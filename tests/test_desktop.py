from __future__ import annotations

import asyncio
import base64
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from tiaaa.apply import desktop, runner
from tiaaa.apply.prompt import desktop_prompt
from tiaaa.codex import codex_command
from tiaaa.config import save_settings


@pytest.mark.parametrize("value", [-1, 1.1, float("nan"), float("inf"), True, "0.5"])
def test_invalid_coordinates_never_click(value):
    driver = object.__new__(desktop.NativeChrome)
    driver.gui = Mock()
    with pytest.raises(ValueError):
        driver.click(value, 0.5)
    driver.gui.click.assert_not_called()


def driver_fixture(tmp_path):
    driver = object.__new__(desktop.NativeChrome)
    driver.gui = Mock()
    driver.lock = threading.RLock()
    driver.window_id = 41
    driver.worker_dir = tmp_path.resolve()
    driver._chrome_foreground = Mock(return_value=True)
    driver._windows = Mock(return_value=[{
        "kCGWindowNumber": 41,
        "kCGWindowBounds": {"X": 100, "Y": 50, "Width": 1001, "Height": 801},
    }])
    return driver


def test_native_coordinates_use_window_points_and_refuse_other_windows(tmp_path):
    driver = driver_fixture(tmp_path)
    driver.click(0.5, 0.5)
    driver.gui.click.assert_called_once_with(600, 450)
    driver.gui.reset_mock()
    driver._chrome_foreground.return_value = False
    with pytest.raises(RuntimeError, match="paused"):
        driver.click(0.5, 0.5)
    driver.gui.click.assert_not_called()
    driver._chrome_foreground.return_value = True
    driver.window_id = 42
    with pytest.raises(RuntimeError, match="paused"):
        driver.press_key("Enter")
    driver.gui.hotkey.assert_not_called()


@pytest.mark.parametrize("url", ["javascript:alert(1)", "file:///etc/passwd", "chrome://settings",
                                  "https://user:pass@example.com", "https://example.com\n"])
def test_navigation_rejects_non_web_urls_before_input(tmp_path, url):
    driver = driver_fixture(tmp_path)
    with pytest.raises(ValueError):
        driver.navigate(url)
    driver.gui.hotkey.assert_not_called()


def test_native_navigation_unicode_and_keys(tmp_path, monkeypatch):
    driver = driver_fixture(tmp_path)
    driver.quartz = SimpleNamespace(
        CGEventCreateKeyboardEvent=Mock(return_value="event"),
        CGEventKeyboardSetUnicodeString=Mock(), CGEventPost=Mock(), kCGHIDEventTap=0,
    )
    monkeypatch.setattr(desktop.time, "sleep", lambda _: None)
    driver.type_text("Zoë 😀", replace=True)
    assert driver.quartz.CGEventKeyboardSetUnicodeString.call_args.args == ("event", 6, "Zoë 😀")
    driver.gui.hotkey.assert_called_with("command", "a")
    driver.type_text = Mock()
    driver.navigate("https://careers.example.com/jobs/123")
    driver.type_text.assert_called_once_with("https://careers.example.com/jobs/123", replace=True)
    driver.gui.hotkey.assert_called_with("enter")
    with pytest.raises(ValueError):
        driver.press_key("command+q")
    with pytest.raises(ValueError):
        driver.scroll("down", 100)
    driver.scroll("down", 4)
    driver.gui.scroll.assert_called_once_with(-4)


def test_document_upload_blocks_traversal_and_symlinks(tmp_path, monkeypatch):
    worker = tmp_path / "worker"
    worker.mkdir()
    document = worker / "resume.pdf"
    document.write_bytes(b"%PDF test")
    outside = tmp_path / "private.pdf"
    outside.write_bytes(b"private")
    (worker / "link.pdf").symlink_to(outside)
    driver = driver_fixture(worker)
    driver.type_text = Mock()
    monkeypatch.setattr(desktop.time, "sleep", lambda _: None)
    for path in [outside, worker / "link.pdf", worker / "missing.pdf"]:
        with pytest.raises(ValueError):
            driver.upload(str(path))
    driver.gui.hotkey.assert_not_called()
    driver.upload(str(document))
    driver.type_text.assert_called_once_with(str(document), replace=True)
    assert driver.gui.hotkey.call_args_list[-1].args == ("enter",)


def test_restore_owned_window_does_not_navigate(tmp_path, monkeypatch):
    driver = driver_fixture(tmp_path)
    run = Mock()
    monkeypatch.setattr(desktop.subprocess, "run", run)
    driver.activate()
    assert run.call_args.args[0] == ["open", "-a", "Google Chrome"]
    driver.gui.hotkey.assert_not_called()
    driver.window_id = 99
    with pytest.raises(RuntimeError, match="Reopen"):
        driver.activate()


def test_settings_and_platform_checks(tmp_path, settings, monkeypatch):
    from tiaaa.config import AppPaths
    settings["automation"]["browser_backend"] = "invalid"
    with pytest.raises(ValueError, match="browser_backend"):
        save_settings(settings, AppPaths(tmp_path))
    settings["automation"].update(browser_backend="desktop", headless=True)
    with pytest.raises(ValueError, match="headless"):
        save_settings(settings, AppPaths(tmp_path))
    monkeypatch.setattr(desktop.platform, "system", lambda: "Linux")
    with pytest.raises(ValueError, match="outside Docker"):
        desktop.validate_desktop()
    monkeypatch.setattr(desktop.platform, "system", lambda: "Darwin")
    with pytest.raises(ValueError, match="visible"):
        desktop.validate_desktop(headless=True)


def test_prompt_retains_facts_and_submission_rules():
    original = ("Use only the Playwright browser tools.\nFACTS graduation=2028; NO SUBMIT\n"
                "EFFICIENT BROWSER CONTROL\nOLD DOM refs\nWORKFLOW\nAudit before submitting")
    result = desktop_prompt(original)
    assert "FACTS graduation=2028; NO SUBMIT" in result
    assert "Audit before submitting" in result
    assert "OLD DOM" not in result
    assert "fractions" in result
    assert "desktop Chrome browser tools" in result


def test_both_agents_authenticate_and_only_receive_native_tools(tmp_path):
    names = desktop.DESKTOP_TOOLS
    command = codex_command(schema_path=tmp_path / "schema.json", port=1234,
                            browser_tools=names, browser_token="local-secret")
    assert 'mcp_servers.tiaaa_browser.http_headers.Authorization="Bearer local-secret"' in command
    assert 'mcp_servers.tiaaa_browser.enabled_tools=' + json.dumps(list(names)) in command
    assert runner._mcp_config(1234, "local-secret")["mcpServers"]["tiaaa_browser"]["headers"] == {
        "Authorization": "Bearer local-secret",
    }
    allowed = tuple("mcp__tiaaa_browser__" + n for n in names)
    claude = runner._claude_command(model="sonnet", config_path=tmp_path / "mcp.json", browser_tools=allowed)
    assert "browser_scroll" in claude[claude.index("--allowedTools") + 1]
    assert "browser_fill_form" not in claude[claude.index("--allowedTools") + 1]


def test_mcp_authentication_tool_contract_and_screenshot_delivery():
    pytest.importorskip("mcp")
    driver = Mock()
    frame = b"test-jpeg"
    driver.snapshot.return_value = frame
    published = []
    app = desktop.desktop_app(driver, "private-token", published.append)

    async def check():
        async with app.app.router.lifespan_context(app.app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:9430") as client:
                headers = {"Authorization": "Bearer private-token",
                           "Accept": "application/json, text/event-stream"}
                request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
                assert (await client.post("/mcp", json=request)).status_code == 403
                assert (await client.post("/mcp", json=request, headers={
                    **headers, "Origin": "https://employer.example",
                })).status_code == 403
                result = await client.post("/mcp", json=request, headers=headers)
                assert result.status_code == 200, result.text
                assert {t["name"] for t in result.json()["result"]["tools"]} == set(desktop.DESKTOP_TOOLS)
                for name, arguments in [
                    ("browser_snapshot", {}), ("browser_click", {"x": 0.25, "y": 0.75}),
                    ("browser_type", {"text": "Avery", "replace": True}),
                    ("browser_press_key", {"key": "Tab"}),
                    ("browser_navigate", {"url": "https://careers.example.com"}),
                    ("browser_scroll", {"direction": "down", "amount": 3}),
                    ("browser_file_upload", {"path": "/worker/resume.pdf"}),
                    ("browser_wait_for", {"time_seconds": 0}),
                ]:
                    result = await client.post("/mcp", headers=headers, json={
                        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                        "params": {"name": name, "arguments": arguments},
                    })
                    content = result.json()["result"]
                    assert not content.get("isError"), content
                    assert content["content"][0]["type"] == "image"
                    assert base64.b64decode(content["content"][0]["data"]) == frame
                driver.click.assert_called_once_with(0.25, 0.75)
                driver.type_text.assert_called_once_with("Avery", True)
                assert len(published) == len(desktop.DESKTOP_TOOLS)
    asyncio.run(check())


def test_bridge_preview_files_are_private_and_stop_preserves_browser(tmp_path):
    bridge = desktop.DesktopBridge(port=9430, worker_dir=tmp_path, worker_id="worker-test",
                                   output_path=tmp_path / "preview.jpg")
    bridge._publish(b"jpeg")
    assert (tmp_path / "preview.jpg").read_bytes() == b"jpeg"
    assert (tmp_path / "preview.jpg").stat().st_mode & 0o777 == 0o600
    bridge.driver = Mock()
    desktop._ACTIVE_WORKERS.add("worker-test")
    assert desktop.desktop_worker_active("worker-test")
    bridge.resume()
    bridge.driver.activate.assert_called_once()
    bridge.stop()
    bridge.driver.close.assert_called_once()
    assert not desktop.desktop_worker_active("worker-test")


def test_desktop_session_builds_native_prompt_and_preserves_checkpoint_on_focus_failure(tmp_path, profile):
    from tiaaa.config import AppPaths, ensure_dirs
    paths = ensure_dirs(AppPaths(tmp_path))
    paths.resume_pdf.write_bytes(b"%PDF test")
    paths.resume_text.write_text("Avery Student, Computer Science.")
    prepare = Mock(side_effect=RuntimeError("Return to the application Chrome window"))
    session = runner._ApplicationAgentSession(
        job={"id": 1, "company": "Acme", "role": "Software Intern",
             "application_url": "https://example.com/apply", "resume_path": str(paths.resume_pdf)},
        profile=profile, paths=paths, worker_id=0, port=9430, model="sonnet", timeout=30,
        submit=False, unattended=False, browser_backend="desktop", browser_token="test-token",
        prepare_browser=prepare,
    )
    assert "DESKTOP BROWSER CONTROL" in session.initial_prompt
    assert "EFFICIENT BROWSER CONTROL" not in session.initial_prompt
    assert "Bearer test-token" in (paths.workers / "worker-0" / "playwright-mcp.json").read_text()
    session.process = Mock()
    result, _ = session._turn("Inspect the current form")
    assert result.result == "needs_review" and result.reason_code == "access_blocked"
    session.process.turn.assert_not_called()


def test_missing_os_permissions_fail_before_chrome_launch(tmp_path, monkeypatch):
    import sys
    monkeypatch.setattr(desktop, "validate_desktop", lambda: None)
    monkeypatch.setattr(desktop, "_accessibility_trusted", lambda: False)
    monkeypatch.setattr(desktop.tempfile, "gettempdir", lambda: str(tmp_path))
    for module in ("AppKit", "Quartz", "pyautogui"):
        monkeypatch.setitem(sys.modules, module, Mock())
    launch = Mock()
    monkeypatch.setattr(desktop.subprocess, "run", launch)
    with pytest.raises(PermissionError, match="Accessibility and Screen Recording"):
        desktop.NativeChrome(tmp_path)
    launch.assert_not_called()


def test_native_startup_uses_existing_chrome_without_debug_flags(tmp_path, monkeypatch):
    import sys
    monkeypatch.setattr(desktop, "validate_desktop", lambda: None)
    monkeypatch.setattr(desktop, "_accessibility_trusted", lambda: True)
    monkeypatch.setattr(desktop.tempfile, "gettempdir", lambda: str(tmp_path))
    gui, quartz, appkit = Mock(), Mock(), Mock()
    appkit.NSWorkspace.sharedWorkspace.return_value.frontmostApplication.return_value.bundleIdentifier\
        .return_value = "com.google.Chrome"
    quartz.CGPreflightScreenCaptureAccess.return_value = True
    def window(number):
        return {"kCGWindowOwnerName": "Google Chrome", "kCGWindowLayer": 0,
                "kCGWindowBounds": {"Width": 1000}, "kCGWindowNumber": number}
    quartz.CGWindowListCopyWindowInfo.side_effect = [[window(1)], [window(2), window(1)]]
    for name, module in [("AppKit", appkit), ("Quartz", quartz), ("pyautogui", gui)]:
        monkeypatch.setitem(sys.modules, name, module)
    launch = Mock()
    monkeypatch.setattr(desktop.subprocess, "run", launch)
    driver = desktop.NativeChrome(tmp_path)
    assert driver.window_id == 2
    assert launch.call_args.args[0] == ["open", "-a", "Google Chrome"]
    gui.hotkey.assert_called_once_with("command", "n")
    driver.close()
    assert driver.lease == -1
    driver.close()
