from __future__ import annotations

import httpx

import tiaaa.discovery.github as github
from tiaaa.config import SOURCE_DOCUMENTS
from tiaaa.discovery.github import GitHubPoller


def test_poller_uses_baseline_then_conditional_request(tmp_path, profile, settings) -> None:
    source = SOURCE_DOCUMENTS[0]
    document = """
| Company | Role | Location | Apply | Added |
| --- | --- | --- | --- | --- |
| Acme | Software Intern | Remote | [apply](https://jobs.test/1) | 2026-07-18 |
"""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.headers.get("if-none-match") == '"v1"':
            return httpx.Response(304, request=request)
        return httpx.Response(
            200,
            text=document,
            headers={"etag": '"v1"'},
            request=request,
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    poller = GitHubPoller(db_path=str(tmp_path / "poller.db"), client=client)
    first = poller.sync_document(source, profile=profile, settings=settings)
    second = poller.sync_document(source, profile=profile, settings=settings)
    client.close()

    assert first.status == "synced"
    assert first.baseline is True
    assert first.queued == 0
    assert second.status == "unchanged"
    assert requests[1].headers["if-none-match"] == '"v1"'


def test_empty_parse_preserves_previously_populated_source(tmp_path, profile, settings) -> None:
    source = SOURCE_DOCUMENTS[0]
    populated = """
| Company | Role | Location | Apply | Added |
| --- | --- | --- | --- | --- |
| Acme | Software Intern | Remote | [apply](https://jobs.test/1) | 2026-07-18 |
"""
    responses = iter(
        [
            httpx.Response(200, text=populated, headers={"etag": '"v1"'}),
            httpx.Response(200, text="# Temporarily malformed", headers={"etag": '"v2"'}),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        response = next(responses)
        response.request = request
        return response

    path = tmp_path / "guard.db"
    client = httpx.Client(transport=httpx.MockTransport(handler))
    poller = GitHubPoller(db_path=str(path), client=client)
    assert poller.sync_document(source, profile=profile, settings=settings).status == "synced"
    guarded = poller.sync_document(source, profile=profile, settings=settings)
    client.close()

    assert guarded.status == "error"
    row = poller.connection.execute("SELECT is_active, pipeline_status FROM jobs").fetchone()
    assert tuple(row) == (1, "discovered")


def test_oversized_source_is_rejected_before_parsing(
    tmp_path, profile, settings, monkeypatch
) -> None:
    source = SOURCE_DOCUMENTS[0]
    monkeypatch.setattr(github, "MAX_DOCUMENT_BYTES", 10)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 11, request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    poller = GitHubPoller(db_path=str(tmp_path / "oversized.db"), client=client)
    result = poller.sync_document(source, profile=profile, settings=settings)
    client.close()

    assert result.status == "error"
    assert "exceeds" in (result.error or "")
