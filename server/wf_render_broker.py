"""Host-side render bridge for an agent running in a restricted Windows token.

The Windows ACL executor cannot create the named pipes Node needs for esbuild
and the Remotion compositor. The trusted host process starts this loopback-only
broker for one node workspace. A random per-run token authorizes the render,
the workspace is fixed by the host, and every project/output path still goes
through wf_video_tools path validation.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
from pathlib import Path
import secrets
import threading
import time

import wf_video_tools


MAX_BODY = 64 * 1024
FIELDS = {"project", "entry", "composition", "output", "scratch", "timeout"}


class RenderBroker:
    """One workspace-scoped, token-authenticated render endpoint."""

    def __init__(self, workspace):
        self.workspace = Path(workspace).resolve()
        if not self.workspace.is_dir():
            raise ValueError("Render broker requires an existing node workspace")
        self.token = secrets.token_urlsafe(32)
        self._stop = threading.Event()
        self._render_lock = threading.Lock()
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._httpd.daemon_threads = True
        self._httpd.broker = self
        self._thread = None

    def record(self, kind, **data):
        path = self.workspace / 'scratch/render-events.jsonl'
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(dict(at=time.time(), kind=kind, **data), ensure_ascii=False) + '\n')

    @property
    def url(self):
        host, port = self._httpd.server_address[:2]
        return "http://" + host + ":" + str(port)

    def start(self):
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        try:
            self._httpd.shutdown()
            self._httpd.server_close()
        except Exception:
            pass
        try:
            self._thread.join(timeout=3)
        except Exception:
            pass

    def render(self, payload):
        if self._stop.is_set():
            raise ValueError("Render broker is stopping")
        if not isinstance(payload, dict) or set(payload) - FIELDS:
            raise ValueError("Invalid render request")
        required = {"project", "entry", "composition", "output"}
        if not required.issubset(payload):
            raise ValueError("Render request is missing required fields")
        values = {name: payload.get(name) for name in ("project", "entry", "composition", "output")}
        if not all(isinstance(value, str) and value for value in values.values()):
            raise ValueError("Render paths and composition must be nonempty strings")
        scratch = payload.get("scratch", "scratch/remotion")
        timeout = payload.get("timeout", 1200)
        if not isinstance(scratch, str) or type(timeout) not in (int, float):
            raise ValueError("Invalid scratch or timeout")
        if not self._render_lock.acquire(blocking=False):
            raise ValueError("A render is already active for this workspace")
        try:
            if self._stop.is_set():
                raise ValueError("Render broker is stopping")
            self.record('render_started', **values)
            result = wf_video_tools.render(
                workspace=str(self.workspace), project=values["project"], entry=values["entry"],
                composition=values["composition"], output=values["output"], scratch=scratch,
                timeout=timeout, cancel_event=self._stop,
            )
            self.record('render_completed', **result)
            return result
        except Exception as exc:
            self.record('render_stopped' if self._stop.is_set() else 'render_failed', error=wf_video_tools._redact(exc))
            raise
        finally:
            self._render_lock.release()

    def tone(self, payload):
        if self._stop.is_set() or not isinstance(payload, dict) or set(payload) - {'output', 'duration', 'frequency', 'volume'}:
            raise ValueError('Invalid tone request or stopped broker')
        if not isinstance(payload.get('output'), str):
            raise ValueError('Tone output is required')
        result = wf_video_tools.tone(workspace=self.workspace, **payload)
        self.record('tone_generated', **result)
        return result


class Handler(BaseHTTPRequestHandler):
    server_version = "WorkflowRenderBroker"
    sys_version = ""

    def log_message(self, *args):
        return

    def _send(self, status, payload):
        body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        broker = self.server.broker
        try:
            length = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            return self._send(400, {'ok': False, 'error': 'Invalid content length'})
        # Drain bounded request bodies before rejection. Closing a Windows
        # socket with unread data can reset it before the client sees 401/413.
        if length < 1 or length > MAX_BODY + 1:
            return self._send(413, {'ok': False, 'error': 'Render request is too large'})
        self.connection.settimeout(5)
        try:
            raw = self.rfile.read(length)
        except OSError:
            return self._send(408, {'ok': False, 'error': 'Request body timed out'})
        if len(raw) != length:
            return self._send(400, {'ok': False, 'error': 'Incomplete request body'})
        if length > MAX_BODY:
            return self._send(413, {'ok': False, 'error': 'Render request is too large'})
        if self.path not in {"/render", '/tone'}:
            return self._send(404, {"ok": False, "error": "Not found"})
        supplied = self.headers.get("Authorization", "")
        expected = "Bearer " + broker.token
        if not hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")):
            return self._send(401, {"ok": False, "error": "Unauthorized"})
        if not self.headers.get("Content-Type", "").lower().startswith("application/json"):
            return self._send(415, {"ok": False, "error": "Expected application/json"})
        try:
            payload = json.loads(raw.decode("utf-8"))
            result = broker.tone(payload) if self.path == '/tone' else broker.render(payload)
        except (UnicodeError, ValueError) as exc:
            return self._send(400, {"ok": False, "error": wf_video_tools._redact(exc)})
        except Exception:
            return self._send(500, {"ok": False, "error": "Render failed without a safe diagnostic"})
        return self._send(200, result)
