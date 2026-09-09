"""Opt-in macOS Chrome control through screen images and OS input, without CDP."""

from __future__ import annotations

import io
import math
import os
import platform
import secrets
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from tiaaa.apply.preview import preview_frame_hub

_ACTIVE_WORKERS: set[str] = set()


def desktop_worker_active(worker_id: str) -> bool:
    return worker_id in _ACTIVE_WORKERS


def _accessibility_trusted() -> bool:
    import ctypes
    services = ctypes.CDLL("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
    services.AXIsProcessTrusted.restype = ctypes.c_bool
    return bool(services.AXIsProcessTrusted())


DESKTOP_TOOLS = (
    "browser_snapshot", "browser_navigate", "browser_click", "browser_type",
    "browser_press_key", "browser_scroll", "browser_file_upload", "browser_wait_for",
)
_KEYS = {
    "Enter": ("enter",), "Tab": ("tab",), "Shift+Tab": ("shift", "tab"),
    "Escape": ("esc",), "Backspace": ("backspace",), "Delete": ("delete",),
    "ArrowDown": ("down",), "ArrowUp": ("up",), "ArrowLeft": ("left",),
    "ArrowRight": ("right",), "Space": ("space",), "PageDown": ("pagedown",),
    "PageUp": ("pageup",), "Home": ("home",), "End": ("end",),
    "SelectAll": ("command", "a"),
}


def validate_desktop(*, headless: bool = False) -> None:
    if platform.system() != "Darwin":
        raise ValueError("Desktop Chrome requires TI-AAA running directly on macOS, outside Docker")
    if headless or os.environ.get("TIAAA_FORCE_HEADLESS") == "1":
        raise ValueError("Desktop Chrome requires visible browser windows; turn off headless mode")
    try:
        import importlib.util
        if any(importlib.util.find_spec(name) is None for name in ("mcp", "pyautogui", "Quartz")):
            raise ImportError
    except ImportError as exc:
        raise RuntimeError('Install desktop support with: pip install "ti-aaa[desktop]"') from exc


def _coordinate(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise ValueError("Coordinates must be numbers between 0 and 1")
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError("Coordinates must be numbers between 0 and 1")
    return float(value)


def _url(value: str) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username or parsed.password or any(ord(c) < 32 for c in value)):
        raise ValueError("Only ordinary HTTP(S) application URLs are allowed")
    return value


class NativeChrome:
    """Own one new ordinary Chrome window; refuse input when it loses foreground focus."""

    def __init__(self, worker_dir: Path) -> None:
        validate_desktop()
        import fcntl

        import AppKit
        import pyautogui
        import Quartz

        self.gui, self.quartz, self.apps = pyautogui, Quartz, AppKit
        self.worker_dir = worker_dir.resolve()
        self.lock = threading.RLock()
        self.window_id: int | None = None
        self.closed = False
        # OS-wide, per-user lock also covers different TI-AAA data directories.
        lock_path = Path(tempfile.gettempdir()) / f"tiaaa-desktop-{os.getuid()}.lock"
        self.lease = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self.lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if not _accessibility_trusted() or not Quartz.CGPreflightScreenCaptureAccess():
                raise PermissionError(
                    "Desktop Chrome needs Accessibility and Screen Recording permission for the app "
                    "running TI-AAA in System Settings > Privacy & Security. Grant them, then restart TI-AAA."
                )
            # Keep PyAutoGUI's corner fail-safe enabled. Never attach a debugger or change Chrome flags.
            subprocess.run(["open", "-a", "Google Chrome"], check=True, timeout=10, capture_output=True)
            deadline = time.monotonic() + 5
            while not self._chrome_foreground() and time.monotonic() < deadline:
                time.sleep(0.1)
            if not self._chrome_foreground():
                raise RuntimeError("Bring Google Chrome to the foreground, then retry")
            previous = {int(w["kCGWindowNumber"]) for w in self._windows()}
            pyautogui.hotkey("command", "n")
            while time.monotonic() < deadline:
                windows = self._windows()
                if windows and int(windows[0]["kCGWindowNumber"]) not in previous:
                    self.window_id = int(windows[0]["kCGWindowNumber"])
                    break
                time.sleep(0.1)
            if self.window_id is None:
                raise RuntimeError("Could not open a dedicated Chrome window; retry with Chrome visible")
        except BaseException:
            os.close(self.lease)
            self.lease = -1
            raise

    def _chrome_foreground(self) -> bool:
        app = self.apps.NSWorkspace.sharedWorkspace().frontmostApplication()
        return bool(app and app.bundleIdentifier() == "com.google.Chrome")

    def _windows(self) -> list[dict[str, Any]]:
        q = self.quartz
        windows = q.CGWindowListCopyWindowInfo(q.kCGWindowListOptionOnScreenOnly, q.kCGNullWindowID)
        return [w for w in windows if w.get("kCGWindowOwnerName") == "Google Chrome"
                and w.get("kCGWindowLayer") == 0 and w.get("kCGWindowBounds", {}).get("Width", 0) > 200]

    def _guard(self) -> dict[str, Any]:
        if getattr(self, "closed", False):
            raise RuntimeError("Desktop browser session is closed")
        windows = self._windows()
        if (not self._chrome_foreground() or not windows
                or int(windows[0]["kCGWindowNumber"]) != self.window_id):
            raise RuntimeError(
                "Desktop control paused: return to the dedicated application Chrome window. "
                "Do not retry actions in another window; request a human checkpoint."
            )
        self.gui.failSafeCheck()
        return windows[0]["kCGWindowBounds"]

    def activate(self) -> None:
        """Restore only our owned window when the candidate explicitly continues a turn."""
        with self.lock:
            windows = self._windows()
            if not any(int(w["kCGWindowNumber"]) == self.window_id for w in windows):
                raise RuntimeError("Reopen the dedicated application Chrome window before continuing")
            subprocess.run(["open", "-a", "Google Chrome"], check=True, timeout=10, capture_output=True)
            deadline = time.monotonic() + 5
            while not self._chrome_foreground() and time.monotonic() < deadline:
                time.sleep(0.1)
            if not self._chrome_foreground():
                raise RuntimeError("Bring Chrome to the foreground before continuing")
            for _ in range(len(windows) + 1):
                current = self._windows()
                if current and int(current[0]["kCGWindowNumber"]) == self.window_id:
                    self._guard()
                    return
                self.gui.hotkey("command", "`")
                time.sleep(0.2)
            raise RuntimeError("Select the dedicated application Chrome window before continuing")

    def snapshot(self) -> bytes:
        with self.lock:
            self._guard()
            from PIL import Image
            with tempfile.TemporaryDirectory(prefix="tiaaa-screen-") as folder:
                target = Path(folder) / "chrome.jpg"
                subprocess.run(
                    ["screencapture", "-x", "-o", "-l", str(self.window_id), "-t", "jpg", str(target)],
                    check=True, timeout=10, capture_output=True,
                )
                self._guard()
                with Image.open(target) as picture:
                    picture.thumbnail((1440, 1100))
                    output = io.BytesIO()
                    picture.convert("RGB").save(output, format="JPEG", quality=85)
                    return output.getvalue()

    def click(self, x: float, y: float) -> None:
        x, y = _coordinate(x), _coordinate(y)
        with self.lock:
            bounds = self._guard()
            self.gui.click(bounds["X"] + x * (bounds["Width"] - 1),
                           bounds["Y"] + y * (bounds["Height"] - 1))

    def type_text(self, text: str, replace: bool = False) -> None:
        if not text or len(text) > 10000 or "\x00" in text:
            raise ValueError("Text must contain 1–10000 characters without NUL")
        with self.lock:
            self._guard()
            if replace:
                self.gui.hotkey("command", "a")
            # Unicode key events avoid overwriting or reading the user's clipboard.
            q = self.quartz
            for offset in range(0, len(text), 20):
                self._guard()
                chunk = text[offset:offset + 20]
                length = len(chunk.encode("utf-16-le")) // 2
                for down in (True, False):
                    event = q.CGEventCreateKeyboardEvent(None, 0, down)
                    q.CGEventKeyboardSetUnicodeString(event, length, chunk)
                    q.CGEventPost(q.kCGHIDEventTap, event)
                time.sleep(0.01)

    def press_key(self, key: str) -> None:
        if key not in _KEYS:
            raise ValueError(f"Unsupported key; choose one of {', '.join(_KEYS)}")
        with self.lock:
            self._guard()
            self.gui.hotkey(*_KEYS[key])

    def navigate(self, url: str) -> None:
        url = _url(url)
        with self.lock:
            self._guard()
            self.gui.hotkey("command", "l")
            self.type_text(url, replace=True)
            self.press_key("Enter")

    def scroll(self, direction: str, amount: int = 4) -> None:
        if direction not in {"up", "down"} or isinstance(amount, bool) or not 1 <= amount <= 10:
            raise ValueError("Scroll direction must be up/down and amount between 1 and 10")
        with self.lock:
            bounds = self._guard()
            self.gui.moveTo(bounds["X"] + bounds["Width"] / 2, bounds["Y"] + bounds["Height"] / 2)
            self.gui.scroll(amount if direction == "up" else -amount)

    def upload(self, path: str) -> None:
        target = Path(path).resolve()
        if (not target.is_relative_to(self.worker_dir) or not target.is_file()
                or target.suffix.lower() not in {".pdf", ".doc", ".docx", ".txt"}):
            raise ValueError("Upload only the supplied application documents in the worker directory")
        with self.lock:
            self._guard()
            self.gui.hotkey("command", "shift", "g")
            time.sleep(0.3)
            self.type_text(str(target), replace=True)
            self.press_key("Enter")
            time.sleep(0.3)
            self.press_key("Enter")

    def close(self) -> None:
        # Leave Chrome and the application intact; never kill the user's browser.
        with self.lock:
            self.closed = True
            if self.lease >= 0:
                os.close(self.lease)
                self.lease = -1


class _PrivateBridge:
    def __init__(self, app: Any, token: str) -> None:
        self.app, self.token = app, token

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            headers = dict(scope.get("headers", []))
            supplied = headers.get(b"authorization", b"").decode("latin1")
            if b"origin" in headers or not secrets.compare_digest(supplied, f"Bearer {self.token}"):
                from starlette.responses import Response
                await Response(status_code=403)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def desktop_app(driver: Any, token: str, publish: Any) -> Any:
    """Build the authenticated loopback MCP app; injectable driver permits non-UI tests."""
    from mcp.server.fastmcp import FastMCP
    from mcp.server.fastmcp.utilities.types import Image

    mcp = FastMCP("TI-AAA Desktop Chrome", stateless_http=True, json_response=True)

    def snapshot() -> Any:
        frame = driver.snapshot()
        publish(frame)
        return Image(data=frame, format="jpeg")

    @mcp.tool()
    def browser_snapshot() -> Any:
        """See the dedicated Chrome window. Coordinates are fractions (0..1) of this image."""
        return snapshot()

    @mcp.tool()
    def browser_navigate(url: str) -> Any:
        """Navigate using Chrome's address bar. Use only at the beginning of the application."""
        driver.navigate(url)
        time.sleep(0.5)
        return snapshot()

    @mcp.tool()
    def browser_click(x: float, y: float) -> Any:
        """Click a visible target at normalized image coordinates; inspect a fresh image first."""
        driver.click(x, y)
        return snapshot()

    @mcp.tool()
    def browser_type(text: str, replace: bool = False) -> Any:
        """Type Unicode into the focused field; replace selects its existing text first."""
        driver.type_text(text, replace)
        return snapshot()

    @mcp.tool()
    def browser_press_key(key: str) -> Any:
        """Press Enter, Tab, Shift+Tab, Escape, arrows, Space, PageDown/Up or SelectAll."""
        driver.press_key(key)
        return snapshot()

    @mcp.tool()
    def browser_scroll(direction: str, amount: int = 4) -> Any:
        """Scroll up/down 1–10 wheel ticks in the application window."""
        driver.scroll(direction, amount)
        return snapshot()

    @mcp.tool()
    def browser_file_upload(path: str) -> Any:
        """After opening the file picker, select the supplied document using its absolute path."""
        driver.upload(path)
        return snapshot()

    @mcp.tool()
    def browser_wait_for(time_seconds: float = 1) -> Any:
        """Wait at most five seconds, then inspect Chrome."""
        if not math.isfinite(time_seconds) or not 0 <= time_seconds <= 5:
            raise ValueError("Wait must be between 0 and 5 seconds")
        time.sleep(time_seconds)
        return snapshot()

    return _PrivateBridge(mcp.streamable_http_app(), token)


class DesktopBridge:
    """Own the native driver, authenticated MCP listener, and private preview stream."""

    def __init__(self, *, port: int, worker_dir: Path, worker_id: str, output_path: Path) -> None:
        self.port, self.worker_dir = port, worker_dir
        self.worker_id, self.output_path = worker_id, output_path
        self.publish_lock = threading.Lock()
        self.token = secrets.token_urlsafe(32)
        self.driver: NativeChrome | None = None
        self.server: Any = None
        self.thread: threading.Thread | None = None
        self.monitor: threading.Thread | None = None
        self.stopping = threading.Event()

    def resume(self) -> None:
        if self.driver:
            self.driver.activate()

    def _publish(self, frame: bytes) -> None:
        with self.publish_lock:
            self._write_frame(frame)

    def _write_frame(self, frame: bytes) -> None:
        preview_frame_hub.publish(self.worker_id, frame)
        self.output_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.output_path.with_suffix(".tmp")
        fd = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as target:
            target.write(frame)
        temporary.replace(self.output_path)

    def start(self) -> None:
        import socket

        import uvicorn

        # Bind before opening Chrome, so port conflicts cannot create orphan windows.
        listener = socket.socket()
        try:
            listener.bind(("127.0.0.1", self.port))
            self.driver = NativeChrome(self.worker_dir)
            app = desktop_app(self.driver, self.token, self._publish)
            self.server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
            self.thread = threading.Thread(
                target=self.server.run, kwargs={"sockets": [listener]}, daemon=True,
            )
            self.thread.start()
            deadline = time.monotonic() + 10
            while not self.server.started and self.thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.05)
            if not self.server.started:
                raise RuntimeError("Desktop Chrome bridge could not start")
            preview_frame_hub.set_active(self.worker_id, True)
            _ACTIVE_WORKERS.add(self.worker_id)
            self.monitor = threading.Thread(target=self._monitor, daemon=True)
            self.monitor.start()
        except BaseException:
            listener.close()
            self.stop()
            raise

    def _monitor(self) -> None:
        while not self.stopping.wait(1):
            try:
                if self.driver:
                    # Do not overwrite a newer tool frame or share the temp file between writers.
                    with self.driver.lock:
                        self._publish(self.driver.snapshot())
            except Exception:
                pass  # No snapshots of other applications when the user takes the foreground.

    def stop(self) -> None:
        self.stopping.set()
        _ACTIVE_WORKERS.discard(self.worker_id)
        if self.server:
            self.server.should_exit = True
        for thread in (self.monitor, self.thread):
            if thread:
                thread.join(timeout=12)
        if self.driver:
            self.driver.close()
        preview_frame_hub.set_active(self.worker_id, False)
