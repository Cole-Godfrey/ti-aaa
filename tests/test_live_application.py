"""Opt-in real Codex/Chrome smoke test, exclusively against a local fake employer."""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from reportlab.pdfgen import canvas

from tiaaa.apply.chrome import launch_chrome, stop_chrome, stop_process_tree
from tiaaa.apply.desktop import DesktopBridge
from tiaaa.apply.runner import _ApplicationAgentSession, _launch_mcp_bridge
from tiaaa.config import AppPaths, ensure_dirs

pytestmark = pytest.mark.skipif(
    os.environ.get("TIAAA_LIVE_BROWSER_TEST") != "1",
    reason="Requires Codex login, Chrome, and a local browser bridge",
)

PAGE = b"""<!doctype html><html><head><title>Acme Software Engineer Intern</title></head><body>
<h1>Acme - Software Engineer Intern</h1><p>Remote internship for Computer Science students.</p>
<form id="application"><label>Full name <input name="name" required></label>
<label>Email <input name="email" type="email" required></label>
<label>Resume <input name="resume" type="file" required></label>
<button type="submit">Submit application</button></form>
<script>
application.onsubmit = async e => {
 e.preventDefault();
 const form = new FormData(application);
 await fetch('/submit', {method:'POST',body:JSON.stringify({name:form.get('name'),
 email:form.get('email'),resume:form.get('resume').name})});
 document.body.innerHTML = '<h1>Acme - Software Engineer Intern</h1>' +
 '<p>An email verification code was sent to avery@example.com.</p>' +
 '<form id="verify"><label>Email verification code <input name="code" required></label>' +
 '<button>Verify application</button></form>';
 verify.onsubmit = async event => { event.preventDefault(); const response = await fetch('/verify',
 {method:'POST',body:new FormData(verify).get('code')});
 document.body.innerHTML = await response.text(); };
};
</script></body></html>"""


def test_real_browser_fills_uploads_then_continues_after_email_verification(tmp_path, profile):
    posts = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(PAGE)

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            posts.append((self.path, body))
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"<h1>Application received</h1><p>Thank you for applying to Acme Software Engineer Intern. "
                b"Confirmation ACME-123.</p>"
                if self.path == "/verify" and body == b"A1B2C3D4"
                else b"Verification required"
            )

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    paths = ensure_dirs(AppPaths(tmp_path))
    paths.resume_text.write_text("Avery Student, Computer Science, Example University. Python project.")
    pdf = canvas.Canvas(str(paths.resume_pdf))
    pdf.drawString(50, 750, paths.resume_text.read_text())
    pdf.save()
    chrome = bridge = session = native = None
    backend = os.environ.get("TIAAA_LIVE_BROWSER_BACKEND", "playwright")
    assert backend in {"desktop", "playwright"}
    try:
        worker_dir = paths.workers / "worker-7"
        worker_dir.mkdir()
        if backend == "desktop":
            native = DesktopBridge(port=9437, worker_dir=worker_dir, worker_id="worker-7",
                                   output_path=paths.previews / "worker-7.jpg")
            native.start()
        else:
            chrome, port = launch_chrome(worker_id=7, paths=paths, headless=False)
            bridge = _launch_mcp_bridge(cdp_port=port, mcp_port=9437, cwd=worker_dir)
        session = _ApplicationAgentSession(
            job={
                "id": 1,
                "company": "Acme",
                "role": "Software Engineer Intern",
                "location": "Remote",
                "application_url": f"http://127.0.0.1:{server.server_port}/",
                "resume_path": str(paths.resume_pdf),
                "base_resume_text_path": str(paths.resume_text),
            },
            profile=profile,
            paths=paths,
            worker_id=7,
            port=9437,
            model="sonnet",
            timeout=180,
            submit=True,
            unattended=True,
            provider="codex",
            claude_fallback=False,
            browser_backend=backend,
            browser_token=native.token if native else "",
            prepare_browser=native.resume if native else None,
        )
        result = session.start()
        assert result.reason_code == "verification_required", result
        assert len(posts) == 1, posts
        posted = json.loads(posts[0][1])
        assert posted["name"] == "Avery Student"
        assert posted["email"] == "avery@example.com"
        assert posted["resume"].endswith(".pdf")
        result = session.continue_with(
            {"email_verification_code": {"question": "Email verification code", "answer": "A1B2C3D4"}}
        )
        assert result.result == "applied", result
        assert [path for path, _ in posts] == ["/submit", "/verify"]
    finally:
        if session:
            session.close()
        if native:
            native.stop()
        stop_process_tree(bridge)
        stop_chrome(chrome)
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
