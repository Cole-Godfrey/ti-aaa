"""Low-frequency screenshots for the local live worker view."""

from __future__ import annotations

import base64
import json
import logging
import threading
from contextlib import suppress
from pathlib import Path

import httpx
from websockets.sync.client import connect

log = logging.getLogger(__name__)


class PreviewCapture:
    """Capture a browser tab over its loopback-only Chrome DevTools endpoint."""

    def __init__(self, *, port: int, output_path: Path, interval: float = 1.5) -> None:
        self.port = port
        self.output_path = output_path
        self.interval = max(0.75, interval)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(
            target=self._run,
            name=f"tiaaa-preview-{self.port}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)

    def _page_websocket(self) -> str | None:
        response = httpx.get(f"http://127.0.0.1:{self.port}/json/list", timeout=2)
        response.raise_for_status()
        pages = [item for item in response.json() if item.get("type") == "page"]
        preferred = next(
            (item for item in pages if item.get("url") not in {"", "about:blank"}),
            pages[0] if pages else None,
        )
        return str(preferred.get("webSocketDebuggerUrl")) if preferred else None

    def _capture(self) -> None:
        websocket_url = self._page_websocket()
        if not websocket_url:
            return
        with connect(websocket_url, open_timeout=3, close_timeout=1) as websocket:
            websocket.send(
                json.dumps(
                    {
                        "id": 1,
                        "method": "Page.captureScreenshot",
                        "params": {"format": "jpeg", "quality": 68, "fromSurface": True},
                    }
                )
            )
            while not self._stop.is_set():
                response = json.loads(websocket.recv(timeout=4))
                if response.get("id") != 1:
                    continue
                encoded = response.get("result", {}).get("data")
                if not encoded:
                    return
                temporary = self.output_path.with_suffix(".tmp")
                temporary.write_bytes(base64.b64decode(encoded))
                temporary.replace(self.output_path)
                with suppress(OSError):
                    self.output_path.chmod(0o600)
                return

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._capture()
            except Exception as exc:
                log.debug("Live preview capture skipped: %s", exc)
            self._stop.wait(self.interval)
