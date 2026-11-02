import importlib.util
import json
from pathlib import Path
import sys
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

SERVER = Path(__file__).resolve().parents[1] / 'server'
sys.path.insert(0, str(SERVER))


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv('CANVAS_HOME', str(tmp_path))
    spec = importlib.util.spec_from_file_location('authorized_canvas_api', SERVER / 'canvas_editor_server.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    monkeypatch.setattr(module.Handler, 'log_message', lambda *args: None)
    monkeypatch.setattr(module.Handler, 'get_request', lambda self: self.send_json(200, {'ok': True}))
    server = module.CanvasHTTPServer(('127.0.0.1', 0), module.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    def request(path, token=None, origin=None):
        headers = {}
        if token: headers['X-Canvas-Api-Token'] = token
        if origin: headers['Origin'] = origin
        try:
            with urlopen(Request(f'http://127.0.0.1:{server.server_port}'+path, headers=headers), timeout=3) as response:
                return response.status
        except HTTPError as error:
            error.close(); return error.code
    yield module, request
    server.shutdown(); server.server_close(); thread.join(timeout=3)


def test_localhost_is_not_authorization_and_health_remains_available(api):
    module, request = api
    assert request('/api/health') == 200
    assert request('/api/projects') == 403
    assert request('/api/projects', origin='http://localhost:9999') == 403
    assert request('/api/projects', module.API_TOKEN) == 200
    assert request('/api/projects', module.API_TOKEN, 'https://unrelated.example') == 403


def test_native_media_ticket_is_expiring_and_exact_resource_scoped(api):
    module, request = api
    expires = int(time.time()) + 60
    signature = module.media_signature('run-a', 'episodes/video.mp4', expires)
    q = f'?run_id=run-a&path=episodes%2Fvideo.mp4&expires={expires}&media_token={signature}'
    assert request('/api/wf/raw'+q) == 200
    assert request('/api/wf/artifact'+q) == 403
    assert request('/api/wf/raw'+q.replace('run-a', 'run-b')) == 403
    assert request('/api/wf/raw'+q.replace('video.mp4', 'other.mp4')) == 403
    assert not module.authorized_media('/api/wf/raw'+q.replace(str(expires), '1'))
