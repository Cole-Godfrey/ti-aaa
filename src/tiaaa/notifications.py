"""Private browser-event feed and optional SMTP delivery."""

from __future__ import annotations

import logging
import os
import smtplib
import ssl
import threading
from contextlib import suppress
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from tiaaa.config import AppPaths, load_environment, load_settings
from tiaaa.database import (
    get_connection,
    mark_notification_delivery,
    pending_notifications,
)

log = logging.getLogger(__name__)


class NotificationDispatcher:
    """Deliver queued event notices without ever blocking the automation daemon."""

    def __init__(self, paths: AppPaths, db_path: str | Path | None = None) -> None:
        self.paths = paths
        self.db_path = Path(db_path or paths.database).expanduser().resolve()
        self._lock = threading.Lock()

    def _email_configuration(self) -> dict[str, Any]:
        load_environment(self.paths)
        settings = load_settings(self.paths).get("notifications", {})
        return {
            "enabled": bool(settings.get("email_enabled", False)),
            "to": str(settings.get("email_to") or "").strip(),
            "from": str(settings.get("email_from") or settings.get("smtp_username") or "").strip(),
            "host": str(settings.get("smtp_host") or "").strip(),
            "port": int(settings.get("smtp_port") or 587),
            "security": str(settings.get("smtp_security") or "starttls").casefold(),
            "username": str(settings.get("smtp_username") or "").strip(),
            "password": os.environ.get("TIAAA_SMTP_PASSWORD", ""),
            "events": settings.get("events") if isinstance(settings.get("events"), dict) else {},
        }

    @staticmethod
    def _validate(config: dict[str, Any]) -> None:
        for field in ("to", "from", "host"):
            value = str(config[field])
            if not value or "\n" in value or "\r" in value:
                raise ValueError(f"Notification email {field} is not configured")
        if "@" not in str(config["to"]) or "@" not in str(config["from"]):
            raise ValueError("Notification email addresses are invalid")
        if config["security"] not in {"starttls", "ssl", "none"}:
            raise ValueError("SMTP security must be STARTTLS, SSL, or none")
        if config["username"] and not config["password"]:
            raise ValueError("SMTP password is not configured")

    @staticmethod
    def _message(
        config: dict[str, Any],
        *,
        title: str,
        body: str,
    ) -> EmailMessage:
        message = EmailMessage()
        message["Subject"] = f"[TI-AAA] {title.replace(chr(10), ' ').replace(chr(13), ' ')}"
        message["From"] = config["from"]
        message["To"] = config["to"]
        message.set_content(
            f"{body}\n\nOpen your local TI-AAA dashboard for details."
        )
        return message

    @staticmethod
    def _send(config: dict[str, Any], message: EmailMessage) -> None:
        context = ssl.create_default_context()
        if config["security"] == "ssl":
            client: smtplib.SMTP = smtplib.SMTP_SSL(
                config["host"],
                config["port"],
                timeout=10,
                context=context,
            )
        else:
            client = smtplib.SMTP(config["host"], config["port"], timeout=10)
        try:
            client.ehlo()
            if config["security"] == "starttls":
                client.starttls(context=context)
                client.ehlo()
            if config["username"]:
                client.login(config["username"], config["password"])
            client.send_message(message)
        finally:
            with suppress(Exception):
                client.quit()

    def send_test(self) -> None:
        config = self._email_configuration()
        if not config["enabled"]:
            raise ValueError("Enable email notifications and save Settings first")
        self._validate(config)
        self._send(
            config,
            self._message(
                config,
                title="Notification test",
                body="Email delivery is configured correctly.",
            ),
        )

    def flush(self) -> dict[str, int]:
        if not self._lock.acquire(blocking=False):
            return {"sent": 0, "failed": 0, "skipped": 0}
        totals = {"sent": 0, "failed": 0, "skipped": 0}
        try:
            connection = get_connection(self.db_path)
            config = self._email_configuration()
            for notification in pending_notifications(connection):
                category_enabled = bool(
                    config["events"].get(notification["category"], True)
                )
                if not config["enabled"] or not category_enabled:
                    mark_notification_delivery(
                        connection,
                        int(notification["id"]),
                        status="skipped",
                    )
                    totals["skipped"] += 1
                    continue
                try:
                    self._validate(config)
                    self._send(
                        config,
                        self._message(
                            config,
                            title=str(notification["title"]),
                            body=str(notification["body"]),
                        ),
                    )
                except Exception as exc:
                    error = str(exc)[:500] or type(exc).__name__
                    log.warning(
                        "Could not deliver notification %s: %s",
                        notification["id"],
                        error,
                    )
                    mark_notification_delivery(
                        connection,
                        int(notification["id"]),
                        status="failed",
                        error=error,
                    )
                    totals["failed"] += 1
                else:
                    mark_notification_delivery(
                        connection,
                        int(notification["id"]),
                        status="sent",
                    )
                    totals["sent"] += 1
            return totals
        finally:
            self._lock.release()
