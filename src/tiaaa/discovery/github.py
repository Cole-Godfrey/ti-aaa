"""Conditional polling of the configured GitHub repository documents."""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from typing import Any

import httpx

from tiaaa import __version__
from tiaaa.config import SOURCE_DOCUMENTS
from tiaaa.database import (
    ingest_listings,
    init_db,
    mark_source_polled,
    source_headers,
)
from tiaaa.discovery.parser import parse_document
from tiaaa.models import SourceDocument

log = logging.getLogger(__name__)
MAX_DOCUMENT_BYTES = 8 * 1024 * 1024


@dataclass(slots=True)
class SyncResult:
    document_key: str
    label: str
    status: str
    parsed: int = 0
    new: int = 0
    existing: int = 0
    queued: int = 0
    skipped: int = 0
    expired: int = 0
    baseline: bool = False
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "document_key": self.document_key,
            "label": self.label,
            "status": self.status,
            "parsed": self.parsed,
            "new": self.new,
            "existing": self.existing,
            "queued": self.queued,
            "skipped": self.skipped,
            "expired": self.expired,
            "baseline": self.baseline,
            "error": self.error,
        }


class GitHubPoller:
    """Fetch only raw files from the three configured GitHub repositories."""

    def __init__(
        self,
        *,
        db_path: str | None = None,
        client: httpx.Client | None = None,
        timeout: float = 30,
    ) -> None:
        self.connection = init_db(db_path)
        headers = {
            "Accept": "text/plain, text/markdown;q=0.9, */*;q=0.1",
            "User-Agent": f"TI-AAA/{__version__}",
        }
        if token := os.environ.get("GITHUB_TOKEN"):
            headers["Authorization"] = f"Bearer {token}"
        self.client = client or httpx.Client(headers=headers, timeout=timeout, follow_redirects=True)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> GitHubPoller:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def sync_document(
        self,
        source: SourceDocument,
        *,
        profile: dict[str, Any],
        settings: dict[str, Any],
        force: bool = False,
    ) -> SyncResult:
        headers = {} if force else source_headers(self.connection, source)
        try:
            response = self.client.get(source.raw_url, headers=headers)
            if response.status_code == 304:
                mark_source_polled(self.connection, source, success=True)
                return SyncResult(source.document_key, source.label, "unchanged")
            response.raise_for_status()
            if len(response.content) > MAX_DOCUMENT_BYTES:
                raise ValueError(
                    f"source document exceeds the {MAX_DOCUMENT_BYTES // (1024 * 1024)} MiB limit"
                )
            content = response.text
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            state = self.connection.execute(
                "SELECT initialized, content_sha256 FROM sources WHERE document_key = ?",
                (source.document_key,),
            ).fetchone()
            if not force and state and state["initialized"] and state["content_sha256"] == digest:
                mark_source_polled(
                    self.connection,
                    source,
                    etag=response.headers.get("etag"),
                    last_modified=response.headers.get("last-modified"),
                    content_sha256=digest,
                    success=True,
                )
                return SyncResult(source.document_key, source.label, "unchanged")

            listings = parse_document(content, source)
            previous_active = self.connection.execute(
                "SELECT COUNT(*) FROM job_sources WHERE document_key = ? AND active = 1",
                (source.document_key,),
            ).fetchone()[0]
            if previous_active and not listings:
                raise ValueError(
                    "parser returned zero rows for a previously populated source; "
                    "preserving existing listings"
                )
            ingestion = ingest_listings(
                self.connection,
                source,
                listings,
                profile=profile,
                settings=settings,
                include_existing=False,
            )
            mark_source_polled(
                self.connection,
                source,
                etag=response.headers.get("etag"),
                last_modified=response.headers.get("last-modified"),
                content_sha256=digest,
                success=True,
            )
            return SyncResult(
                source.document_key,
                source.label,
                "synced",
                parsed=int(ingestion["parsed"]),
                new=int(ingestion["new"]),
                existing=int(ingestion["existing"]),
                queued=int(ingestion["queued"]),
                skipped=int(ingestion["skipped"]),
                expired=int(ingestion["expired"]),
                baseline=bool(ingestion["baseline"]),
            )
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            log.warning("Could not sync %s: %s", source.document_key, message)
            mark_source_polled(self.connection, source, success=False, error=message)
            return SyncResult(source.document_key, source.label, "error", error=message)

    def sync_all(
        self,
        *,
        profile: dict[str, Any],
        settings: dict[str, Any],
        force: bool = False,
        source_key: str | None = None,
    ) -> list[SyncResult]:
        documents = SOURCE_DOCUMENTS
        if source_key:
            documents = tuple(source for source in documents if source.key == source_key)
        return [
            self.sync_document(
                source,
                profile=profile,
                settings=settings,
                force=force,
            )
            for source in documents
        ]


def sync_repositories(
    *,
    profile: dict[str, Any],
    settings: dict[str, Any],
    force: bool = False,
    source_key: str | None = None,
    db_path: str | None = None,
) -> list[SyncResult]:
    with GitHubPoller(db_path=db_path) as poller:
        return poller.sync_all(
            profile=profile,
            settings=settings,
            force=force,
            source_key=source_key,
        )
