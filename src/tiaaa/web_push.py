"""Local Web Push subscriptions and Auto-mode queue alerts."""

from __future__ import annotations

import base64
import json
import logging
import sqlite3
from contextlib import suppress
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pywebpush import WebPushException, webpush

from tiaaa.config import AppPaths
from tiaaa.database import utc_now

log = logging.getLogger(__name__)

MAX_BROWSER_SUBSCRIPTIONS = 20


def _load_or_create_private_key(paths: AppPaths) -> ec.EllipticCurvePrivateKey:
    """Return the installation's stable P-256 VAPID key."""

    path = paths.web_push_private_key
    if path.exists():
        loaded = serialization.load_pem_private_key(path.read_bytes(), password=None)
        if not isinstance(loaded, ec.EllipticCurvePrivateKey) or not isinstance(
            loaded.curve, ec.SECP256R1
        ):
            raise ValueError(f"Web Push key is not a P-256 private key: {path}")
        return loaded

    path.parent.mkdir(parents=True, exist_ok=True)
    private_key = ec.generate_private_key(ec.SECP256R1())
    path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    with suppress(OSError):
        path.chmod(0o600)
    return private_key


def vapid_public_key(paths: AppPaths) -> str:
    """Return the URL-safe public key used by PushManager.subscribe()."""

    public_bytes = _load_or_create_private_key(paths).public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    return base64.urlsafe_b64encode(public_bytes).rstrip(b"=").decode("ascii")


def store_push_subscription(
    connection: sqlite3.Connection,
    *,
    endpoint: str,
    p256dh: str,
    auth: str,
    user_agent: str | None = None,
) -> dict[str, Any]:
    """Upsert one browser subscription without exposing it through public APIs."""

    now = utc_now()
    connection.execute(
        """
        INSERT INTO web_push_subscriptions (
            endpoint, p256dh, auth, user_agent, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(endpoint) DO UPDATE SET
            p256dh = excluded.p256dh,
            auth = excluded.auth,
            user_agent = excluded.user_agent,
            updated_at = excluded.updated_at,
            last_error = NULL
        """,
        (endpoint, p256dh, auth, user_agent, now, now),
    )
    connection.execute(
        """
        DELETE FROM web_push_subscriptions
        WHERE id NOT IN (
            SELECT id FROM web_push_subscriptions
            ORDER BY updated_at DESC, id DESC LIMIT ?
        )
        """,
        (MAX_BROWSER_SUBSCRIPTIONS,),
    )
    connection.commit()
    row = connection.execute(
        """
        SELECT id, created_at, updated_at, last_success_at, last_error
        FROM web_push_subscriptions WHERE endpoint = ?
        """,
        (endpoint,),
    ).fetchone()
    if row is None:  # pragma: no cover - protected by the upsert above
        raise RuntimeError("Browser subscription could not be stored")
    return dict(row)


def remove_push_subscription(connection: sqlite3.Connection, endpoint: str) -> bool:
    cursor = connection.execute(
        "DELETE FROM web_push_subscriptions WHERE endpoint = ?", (endpoint,)
    )
    connection.commit()
    return bool(cursor.rowcount)


def push_subscription_count(connection: sqlite3.Connection) -> int:
    return int(connection.execute("SELECT COUNT(*) FROM web_push_subscriptions").fetchone()[0])


def _payload(jobs: list[dict[str, Any]]) -> str:
    count = len(jobs)
    title = "New internship queued" if count == 1 else f"{count} new internships queued"
    labels = [f"{job['company']} · {job['role']}"[:110] for job in jobs[:3]]
    if count > 3:
        labels.append(f"+ {count - 3} more")
    return json.dumps(
        {
            "title": title,
            "body": " • ".join(labels),
            "url": "/?view=live",
            "tag": "tiaaa-new-auto-jobs",
        },
        ensure_ascii=False,
    )


def _record_push_error(
    connection: sqlite3.Connection, subscription_id: int, message: str
) -> None:
    connection.execute(
        """
        UPDATE web_push_subscriptions
        SET last_error = ?, updated_at = ? WHERE id = ?
        """,
        (message[:1000], utc_now(), subscription_id),
    )
    connection.commit()


def send_auto_queue_notifications(
    connection: sqlite3.Connection,
    *,
    paths: AppPaths,
    queue: list[dict[str, Any]],
) -> dict[str, int]:
    """Notify each subscribed browser once for newly queued Auto-mode jobs."""

    subscriptions = [
        dict(row)
        for row in connection.execute(
            """
            SELECT id, endpoint, p256dh, auth, created_at
            FROM web_push_subscriptions ORDER BY id
            """
        ).fetchall()
    ]
    summary = {
        "subscriptions": len(subscriptions),
        "sent": 0,
        "jobs": 0,
        "removed": 0,
        "errors": 0,
    }
    if not subscriptions:
        return summary

    auto_jobs = [
        job
        for job in queue
        if job.get("origin") == "auto" and job.get("first_seen_at")
    ]
    if not auto_jobs:
        return summary

    private_key_path = str(paths.web_push_private_key)
    _load_or_create_private_key(paths)
    for subscription in subscriptions:
        delivered = {
            int(row[0])
            for row in connection.execute(
                """
                SELECT job_id FROM web_push_deliveries WHERE subscription_id = ?
                """,
                (subscription["id"],),
            ).fetchall()
        }
        pending = [
            job
            for job in auto_jobs
            if int(job["id"]) not in delivered
            and str(job["first_seen_at"]) >= str(subscription["created_at"])
        ]
        if not pending:
            continue
        try:
            webpush(
                subscription_info={
                    "endpoint": subscription["endpoint"],
                    "keys": {
                        "p256dh": subscription["p256dh"],
                        "auth": subscription["auth"],
                    },
                },
                data=_payload(pending),
                vapid_private_key=private_key_path,
                vapid_claims={"sub": "https://localhost"},
                ttl=3600,
                timeout=5,
            )
        except WebPushException as exc:
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            if status_code in {404, 410}:
                remove_push_subscription(connection, str(subscription["endpoint"]))
                summary["removed"] += 1
            else:
                _record_push_error(connection, int(subscription["id"]), str(exc))
                summary["errors"] += 1
            continue
        except Exception as exc:  # delivery errors must not stop repository polling
            log.warning("Web Push delivery failed: %s", exc)
            _record_push_error(connection, int(subscription["id"]), str(exc))
            summary["errors"] += 1
            continue

        now = utc_now()
        connection.executemany(
            """
            INSERT OR IGNORE INTO web_push_deliveries (
                subscription_id, job_id, delivered_at
            ) VALUES (?, ?, ?)
            """,
            [(subscription["id"], int(job["id"]), now) for job in pending],
        )
        connection.execute(
            """
            UPDATE web_push_subscriptions
            SET last_success_at = ?, last_error = NULL, updated_at = ? WHERE id = ?
            """,
            (now, now, subscription["id"]),
        )
        connection.commit()
        summary["sent"] += 1
        summary["jobs"] += len(pending)
    return summary
