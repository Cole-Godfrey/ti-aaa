"""Persistent local browser streams for the live worker view."""

from __future__ import annotations

import base64
import json
import logging
import math
import platform
import threading
import time
from contextlib import suppress
from pathlib import Path
from queue import Empty, Full, Queue
from typing import Any

import httpx
from websockets.sync.client import connect

log = logging.getLogger(__name__)


class PreviewFrameHub:
    """Keep only the newest private frame for each local browser worker."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._frames: dict[str, tuple[int, bytes]] = {}
        self._sequences: dict[str, int] = {}
        self._active: set[str] = set()

    def publish(self, worker_id: str, frame: bytes) -> int:
        with self._condition:
            sequence = self._sequences.get(worker_id, 0) + 1
            self._sequences[worker_id] = sequence
            self._frames[worker_id] = (sequence, frame)
            self._condition.notify_all()
            return sequence

    def set_active(self, worker_id: str, active: bool) -> None:
        with self._condition:
            if active:
                self._active.add(worker_id)
            else:
                self._active.discard(worker_id)
            self._condition.notify_all()

    def is_active(self, worker_id: str) -> bool:
        with self._condition:
            return worker_id in self._active

    def wait_for_frame(
        self,
        worker_id: str,
        after_sequence: int,
        timeout: float = 10,
    ) -> tuple[int, bytes] | None:
        with self._condition:
            self._condition.wait_for(
                lambda: (
                    worker_id in self._frames
                    and self._frames[worker_id][0] > after_sequence
                ),
                timeout=timeout,
            )
            frame = self._frames.get(worker_id)
            return frame if frame and frame[0] > after_sequence else None

    def clear(self) -> None:
        """Reset process-local frames (used by tests and process shutdown)."""

        with self._condition:
            self._frames.clear()
            self._sequences.clear()
            self._active.clear()
            self._condition.notify_all()


preview_frame_hub = PreviewFrameHub()

_SPECIAL_KEYS: dict[str, tuple[str, int]] = {
    "Backspace": ("Backspace", 8),
    "Tab": ("Tab", 9),
    "Enter": ("Enter", 13),
    "Escape": ("Escape", 27),
    "PageUp": ("PageUp", 33),
    "PageDown": ("PageDown", 34),
    "End": ("End", 35),
    "Home": ("Home", 36),
    "ArrowLeft": ("ArrowLeft", 37),
    "ArrowUp": ("ArrowUp", 38),
    "ArrowRight": ("ArrowRight", 39),
    "ArrowDown": ("ArrowDown", 40),
    "Delete": ("Delete", 46),
}


def _finite_number(value: Any, *, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Browser-control coordinate must be numeric")
    number = float(value)
    if not math.isfinite(number) or number < minimum or number > maximum:
        raise ValueError("Browser-control coordinate is outside the allowed range")
    return number


def _normalize_control_action(action: dict[str, Any]) -> dict[str, Any]:
    """Validate the small input vocabulary accepted by the live browser relay."""

    if not isinstance(action, dict):
        raise ValueError("Browser-control action must be an object")
    action_type = str(action.get("type") or "")
    if action_type == "click":
        return {
            "type": action_type,
            "x": _finite_number(action.get("x"), minimum=0, maximum=1),
            "y": _finite_number(action.get("y"), minimum=0, maximum=1),
        }
    if action_type == "scroll":
        return {
            "type": action_type,
            "x": _finite_number(action.get("x"), minimum=0, maximum=1),
            "y": _finite_number(action.get("y"), minimum=0, maximum=1),
            "delta_x": _finite_number(
                action.get("delta_x", 0), minimum=-1200, maximum=1200
            ),
            "delta_y": _finite_number(
                action.get("delta_y", 0), minimum=-1200, maximum=1200
            ),
        }
    if action_type == "text":
        value = action.get("text")
        if not isinstance(value, str) or not value or len(value) > 2000 or "\x00" in value:
            raise ValueError("Browser-control text is invalid or too long")
        return {"type": action_type, "text": value}
    if action_type == "key":
        key = str(action.get("key") or "")
        if key not in _SPECIAL_KEYS and key != "SelectAll":
            raise ValueError("Browser-control key is not allowed")
        return {"type": action_type, "key": key}
    raise ValueError("Unknown browser-control action")


class BrowserControlHub:
    """Route validated dashboard input only to a process-local live browser worker."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._controllers: dict[str, Any] = {}

    def register(self, worker_id: str, controller: Any) -> None:
        with self._lock:
            self._controllers[worker_id] = controller

    def unregister(self, worker_id: str, controller: Any) -> None:
        with self._lock:
            if self._controllers.get(worker_id) is controller:
                self._controllers.pop(worker_id, None)

    def is_available(self, worker_id: str) -> bool:
        with self._lock:
            return worker_id in self._controllers

    def dispatch(self, worker_id: str, action: dict[str, Any]) -> None:
        with self._lock:
            controller = self._controllers.get(worker_id)
        if controller is None:
            raise ValueError("The live browser is no longer available")
        controller.enqueue_control(_normalize_control_action(action))

    def clear(self) -> None:
        with self._lock:
            self._controllers.clear()


browser_control_hub = BrowserControlHub()


class PreviewCapture:
    """Stream a browser tab over its loopback-only Chrome DevTools endpoint."""

    def __init__(
        self,
        *,
        port: int,
        output_path: Path,
        worker_id: str | None = None,
        reconnect_delay: float = 0.5,
        max_frames_per_second: int = 10,
    ) -> None:
        self.port = port
        self.output_path = output_path
        self.worker_id = worker_id or output_path.stem
        self.reconnect_delay = max(0.25, reconnect_delay)
        self.frame_interval = 1 / max(1, min(max_frames_per_second, 30))
        self._last_frame_at = 0.0
        self._last_persisted_at = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._control_actions: Queue[dict[str, Any]] = Queue(maxsize=256)
        self._viewport_width = 1280.0
        self._viewport_height = 900.0

    def start(self) -> None:
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        preview_frame_hub.set_active(self.worker_id, True)
        browser_control_hub.register(self.worker_id, self)
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
        browser_control_hub.unregister(self.worker_id, self)
        preview_frame_hub.set_active(self.worker_id, False)

    def enqueue_control(self, action: dict[str, Any]) -> None:
        normalized = _normalize_control_action(action)
        try:
            self._control_actions.put_nowait(normalized)
        except Full as exc:
            raise ValueError("The live browser input queue is full") from exc

    def _send_control_action(
        self,
        websocket: Any,
        command_id: int,
        action: dict[str, Any],
    ) -> int:
        def send(method: str, params: dict[str, Any]) -> None:
            nonlocal command_id
            command_id += 1
            websocket.send(
                json.dumps({"id": command_id, "method": method, "params": params})
            )

        action_type = action["type"]
        if action_type in {"click", "scroll"}:
            x = action["x"] * self._viewport_width
            y = action["y"] * self._viewport_height
            if action_type == "click":
                send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
                send(
                    "Input.dispatchMouseEvent",
                    {
                        "type": "mousePressed",
                        "x": x,
                        "y": y,
                        "button": "left",
                        "buttons": 1,
                        "clickCount": 1,
                    },
                )
                send(
                    "Input.dispatchMouseEvent",
                    {
                        "type": "mouseReleased",
                        "x": x,
                        "y": y,
                        "button": "left",
                        "buttons": 0,
                        "clickCount": 1,
                    },
                )
            else:
                send(
                    "Input.dispatchMouseEvent",
                    {
                        "type": "mouseWheel",
                        "x": x,
                        "y": y,
                        "deltaX": action["delta_x"],
                        "deltaY": action["delta_y"],
                    },
                )
        elif action_type == "text":
            send("Input.insertText", {"text": action["text"]})
        elif action["key"] == "SelectAll":
            modifier = 4 if platform.system() == "Darwin" else 2
            for event_type in ("rawKeyDown", "keyUp"):
                send(
                    "Input.dispatchKeyEvent",
                    {
                        "type": event_type,
                        "key": "a",
                        "code": "KeyA",
                        "windowsVirtualKeyCode": 65,
                        "nativeVirtualKeyCode": 65,
                        "modifiers": modifier,
                    },
                )
        else:
            key = action["key"]
            code, virtual_key = _SPECIAL_KEYS[key]
            for event_type in ("rawKeyDown", "keyUp"):
                send(
                    "Input.dispatchKeyEvent",
                    {
                        "type": event_type,
                        "key": key,
                        "code": code,
                        "windowsVirtualKeyCode": virtual_key,
                        "nativeVirtualKeyCode": virtual_key,
                    },
                )
        return command_id

    def _drain_control_actions(self, websocket: Any, command_id: int) -> int:
        for _ in range(64):
            try:
                action = self._control_actions.get_nowait()
            except Empty:
                break
            command_id = self._send_control_action(websocket, command_id, action)
        return command_id

    def _page_websocket(self) -> str | None:
        response = httpx.get(f"http://127.0.0.1:{self.port}/json/list", timeout=2)
        response.raise_for_status()
        pages = [item for item in response.json() if item.get("type") == "page"]
        preferred = next(
            (item for item in pages if item.get("url") not in {"", "about:blank"}),
            pages[0] if pages else None,
        )
        return str(preferred.get("webSocketDebuggerUrl")) if preferred else None

    def _publish_encoded_frame(self, encoded: str, *, throttle_disk: bool = False) -> None:
        if len(encoded) > 24 * 1024 * 1024:
            raise ValueError("Browser preview frame exceeded the local size limit")
        frame = base64.b64decode(encoded, validate=True)
        preview_frame_hub.publish(self.worker_id, frame)
        now = time.monotonic()
        if throttle_disk and self.output_path.exists() and now - self._last_persisted_at < 2:
            return
        temporary = self.output_path.with_suffix(".tmp")
        temporary.write_bytes(frame)
        temporary.replace(self.output_path)
        self._last_persisted_at = now
        with suppress(OSError):
            self.output_path.chmod(0o600)

    def _stream(self) -> None:
        websocket_url = self._page_websocket()
        if not websocket_url:
            return
        with connect(
            websocket_url,
            open_timeout=3,
            close_timeout=1,
            max_size=32 * 1024 * 1024,
        ) as websocket:
            websocket.send(
                json.dumps(
                    {
                        "id": 1,
                        "method": "Page.startScreencast",
                        "params": {
                            "format": "jpeg",
                            "quality": 68,
                            "maxWidth": 1280,
                            "maxHeight": 900,
                            "everyNthFrame": 1,
                        },
                    }
                )
            )
            started = False
            command_id = 1
            while not self._stop.is_set():
                command_id = self._drain_control_actions(websocket, command_id)
                try:
                    message = json.loads(websocket.recv(timeout=0.25))
                except TimeoutError:
                    continue
                if message.get("id") == 1:
                    if message.get("error"):
                        raise RuntimeError(
                            str(message["error"].get("message") or "Chrome screencast unavailable")
                        )
                    started = True
                    continue
                if message.get("method") != "Page.screencastFrame":
                    continue
                params = message.get("params") or {}
                encoded = params.get("data")
                session_id = params.get("sessionId")
                metadata = params.get("metadata") or {}
                width = metadata.get("deviceWidth")
                height = metadata.get("deviceHeight")
                if isinstance(width, (int, float)) and 1 <= width <= 10000:
                    self._viewport_width = float(width)
                if isinstance(height, (int, float)) and 1 <= height <= 10000:
                    self._viewport_height = float(height)
                now = time.monotonic()
                if encoded and now - self._last_frame_at >= self.frame_interval:
                    self._publish_encoded_frame(str(encoded), throttle_disk=True)
                    self._last_frame_at = now
                if session_id is not None:
                    command_id += 1
                    websocket.send(
                        json.dumps(
                            {
                                "id": command_id,
                                "method": "Page.screencastFrameAck",
                                "params": {"sessionId": session_id},
                            }
                        )
                    )
            if started:
                with suppress(Exception):
                    websocket.send(
                        json.dumps(
                            {
                                "id": command_id + 1,
                                "method": "Page.stopScreencast",
                            }
                        )
                    )

    def _capture_once(self) -> None:
        """Retain a JPEG fallback when a Chrome build lacks screencast support."""

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
                if encoded:
                    self._publish_encoded_frame(str(encoded))
                return

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._stream()
            except Exception as exc:
                log.debug("Live browser stream reconnecting: %s", exc)
                try:
                    self._capture_once()
                except Exception as fallback_exc:
                    log.debug("Live preview fallback skipped: %s", fallback_exc)
            self._stop.wait(self.reconnect_delay)
