"""Render broker keeps node scope, authentication and cancellation explicit."""
import json
from pathlib import Path
import sys
import threading
from urllib.error import HTTPError
import urllib.request

import pytest

PLUGIN = (Path(__file__).resolve().parents[2] / "server").parent
sys.path.insert(0, str(PLUGIN / "server"))

import wf_render_broker  # noqa: E402
import wf_video_tools  # noqa: E402


def post(broker, payload, token=None):
    request = urllib.request.Request(
        broker.url + "/render",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": "Bearer " + (broker.token if token is None else token),
                 "Content-Type": "application/json"},
        method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except HTTPError as exc:
        return exc.code, json.loads(exc.read())


@pytest.fixture
def broker(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    calls = []

    def fake_render(**kwargs):
        calls.append(kwargs)
        return {"ok": True, "output": kwargs["output"], "workspace": kwargs["workspace"]}

    monkeypatch.setattr(wf_render_broker.wf_video_tools, "render", fake_render)
    instance = wf_render_broker.RenderBroker(workspace).start()
    try:
        yield instance, workspace, calls
    finally:
        instance.stop()


def render_request():
    return {"project": "work-products/project", "entry": "src/index.tsx",
            "composition": "Explain", "output": "episodes/final.mp4",
            "scratch": "scratch/remotion", "timeout": 120}


def test_missing_or_wrong_token_never_starts_render(broker):
    instance, _, calls = broker
    assert post(instance, render_request(), token="")[0] == 401
    assert post(instance, render_request(), token="wrong-token")[0] == 401
    assert calls == []


def test_broker_fixes_workspace_and_rejects_extra_paths(broker):
    instance, workspace, calls = broker
    escaped = dict(render_request(), workspace=str(workspace.parent))
    status, payload = post(instance, escaped)
    assert status == 400 and payload["ok"] is False
    assert calls == []

    status, payload = post(instance, render_request())
    assert status == 200 and payload["ok"] is True
    assert payload["workspace"] == str(workspace)
    assert calls[0]["workspace"] == str(workspace)
    assert "workspace" not in {key for key in calls[0] if key != "workspace"}


def test_broker_accepts_only_json_and_bounded_bodies(broker):
    instance, _, calls = broker
    request = urllib.request.Request(
        instance.url + "/render", data=json.dumps(render_request()).encode("utf-8"),
        headers={"Authorization": "Bearer " + instance.token,
                 "Content-Type": "text/plain"}, method="POST")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with pytest.raises(HTTPError) as error:
        opener.open(request, timeout=5)
    assert error.value.code == 415
    request = urllib.request.Request(
        instance.url + "/render", data=b"x" * (wf_render_broker.MAX_BODY + 1),
        headers={"Authorization": "Bearer " + instance.token,
                 "Content-Type": "application/json"}, method="POST")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with pytest.raises(HTTPError) as error:
        opener.open(request, timeout=5)
    assert error.value.code == 413
    assert calls == []


def test_stop_cancels_active_render(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    started = threading.Event()
    seen = []

    def waiting_render(**kwargs):
        seen.append(kwargs)
        started.set()
        kwargs["cancel_event"].wait(3)
        raise ValueError("cancelled")

    monkeypatch.setattr(wf_render_broker.wf_video_tools, "render", waiting_render)
    instance = wf_render_broker.RenderBroker(workspace).start()
    result = []

    def invoke():
        try:
            result.append(post(instance, render_request()))
        except Exception as exc:
            result.append(type(exc).__name__)

    client = threading.Thread(target=invoke)
    client.start()
    assert started.wait(2)
    instance.stop()
    client.join(timeout=3)
    assert client.is_alive() is False
    assert seen and seen[0]["cancel_event"].is_set()


def test_broker_environment_rejects_non_loopback(monkeypatch):
    monkeypatch.setenv("CANVAS_RENDER_BROKER_URL", "http://example.com:8080")
    monkeypatch.setenv("CANVAS_RENDER_BROKER_TICKET", "token")
    with pytest.raises(ValueError, match="loopback"):
        wf_video_tools._broker_config()
