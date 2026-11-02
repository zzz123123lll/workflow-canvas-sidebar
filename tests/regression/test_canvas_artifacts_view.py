"""Artifact viewing contract: legacy runs, evidence verification, raw ranges."""
import hashlib
import importlib.util
import json
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

ROOT = (Path(__file__).resolve().parents[2] / "server")
PLUGIN = ROOT.parent


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_api(home):
    spec = importlib.util.spec_from_file_location(
        "canvas_artifact_api_%s" % abs(hash(str(home))), PLUGIN / "server" / "canvas_editor_server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def api(tmp_path, monkeypatch):
    home = tmp_path
    monkeypatch.setenv("CANVAS_HOME", str(home))
    (home / "kb").mkdir()
    (home / "episodes").mkdir()
    (home / "kb" / "modules.json").write_text(json.dumps({"modules": [
        {"id": "report", "artifacts": ["episodes/{stem}-report.md"]},
        {"id": "plain", "artifacts": []},
    ]}), encoding="utf-8")
    report = home / "episodes" / "demo-report.md"
    report.write_text("hello report", encoding="utf-8")
    ok = home / "episodes" / "demo-ok.md"
    ok.write_text("verified body", encoding="utf-8")
    stale = home / "episodes" / "demo-stale.md"
    stale.write_text("changed body", encoding="utf-8")
    gone = home / "episodes" / "demo-gone.md"
    run_dir = home / ".runs" / "run-1"
    run_dir.mkdir(parents=True)
    state = {
        "run_id": "run-1", "episode": "demo", "task_id": "demo", "status": "completed",
        "nodes": [
            {"id": "legacy", "module": "report", "status": "passed"},
            {"id": "verified", "module": "plain", "status": "passed", "artifact_evidence": [
                {"path": "episodes/demo-ok.md", "bytes": ok.stat().st_size, "sha256": sha("verified body")}]},
            {"id": "changed", "module": "plain", "status": "passed", "artifact_evidence": [
                {"path": "episodes/demo-stale.md", "bytes": 1, "sha256": sha("original body")}]},
            {"id": "missing", "module": "plain", "status": "passed", "artifact_evidence": [
                {"path": "episodes/demo-gone.md", "bytes": 12, "sha256": sha("gone")}]},
        ],
    }
    (run_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
    (run_dir / "modules.json").write_text(json.dumps({"modules": [
        {"id": "report", "artifacts": ["episodes/{stem}-report.md"]},
    ]}), encoding="utf-8")
    module = load_api(home)
    module.Handler.log_message = lambda *args: None
    server = module.ThreadingHTTPServer(("127.0.0.1", 0), module.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = "http://127.0.0.1:%d" % server.server_address[1]

    def get(path, params=None, headers=None):
        url = base + path + ("?" + urllib.parse.urlencode(params) if params else "")
        request = urllib.request.Request(url, headers={"X-Canvas-Api-Token": module.API_TOKEN, **(headers or {})})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, response.read(), dict(response.headers)
        except urllib.error.HTTPError as error:
            return error.code, error.read(), dict(error.headers)

    yield get
    server.shutdown()


def rows_for(get, node):
    status, body, _ = get("/api/wf/artifacts", {"run_id": "run-1", "node": node})
    assert status == 200, body
    return json.loads(body)


def test_legacy_artifact_is_listed_but_not_verified(api):
    payload = rows_for(api, "legacy")
    assert payload["counts"]["legacy"] == 1
    assert payload["declaration"] == "snapshot"
    row = payload["artifacts"][0]
    assert row["path"] == "episodes/demo-report.md"
    assert row["status"] == "legacy"
    assert row["kind"] == "text"


def test_old_run_scope_is_frozen_against_module_edits(api, tmp_path):
    # Edit the live module library after the run: an old run must not follow it.
    (tmp_path / "kb" / "modules.json").write_text(json.dumps({"modules": [
        {"id": "report", "artifacts": ["episodes/{stem}-new.md"]},
    ]}), encoding="utf-8")
    (tmp_path / "episodes" / "demo-new.md").write_text("added later", encoding="utf-8")

    payload = rows_for(api, "legacy")
    assert [row["path"] for row in payload["artifacts"]] == ["episodes/demo-report.md"]
    status, body, _ = api("/api/wf/raw", {"run_id": "run-1", "path": "episodes/demo-report.md"})
    assert status == 200 and body == b"hello report"
    status, _, _ = api("/api/wf/raw", {"run_id": "run-1", "path": "episodes/demo-new.md"})
    assert status == 403


def test_unknown_declaration_is_not_guessed(api, tmp_path):
    run_dir = tmp_path / ".runs" / "run-2"
    run_dir.mkdir()
    (run_dir / "state.json").write_text(json.dumps({
        "run_id": "run-2", "episode": "demo", "task_id": "demo",
        "nodes": [{"id": "x", "module": "report", "status": "passed"}],
    }), encoding="utf-8")
    status, body, _ = api("/api/wf/artifacts", {"run_id": "run-2", "node": "x"})
    assert status == 200
    payload = json.loads(body)
    assert payload["artifacts"] == []
    assert payload["declaration"] == "unknown"
    assert "无法确认" in payload["warning"]
    status, _, _ = api("/api/wf/raw", {"run_id": "run-2", "path": "episodes/demo-report.md"})
    assert status == 403


def test_verified_changed_missing_states(api):
    assert rows_for(api, "verified")["artifacts"][0]["status"] == "verified"
    assert rows_for(api, "changed")["artifacts"][0]["status"] == "changed"
    missing = rows_for(api, "missing")
    assert missing["artifacts"][0]["status"] == "missing"
    assert missing["evidence_missing"] == ["episodes/demo-gone.md"]


def test_legacy_artifact_is_readable_and_raw_streams(api):
    status, body, _ = api("/api/wf/artifact", {"run_id": "run-1", "path": "episodes/demo-report.md"})
    assert status == 200
    payload = json.loads(body)
    assert payload["kind"] == "text" and payload["text"] == "hello report"
    assert payload["url"].startswith("/api/wf/raw?")

    status, body, headers = api("/api/wf/raw", {"run_id": "run-1", "path": "episodes/demo-report.md"})
    assert status == 200 and body == b"hello report"
    assert headers.get("Accept-Ranges") == "bytes"

    status, body, headers = api("/api/wf/raw", {"run_id": "run-1", "path": "episodes/demo-report.md"},
                                {"Range": "bytes=6-11"})
    assert status == 206 and body == b"report"
    assert headers.get("Content-Range") == "bytes 6-11/12"


def test_paths_outside_declared_scope_are_rejected(api):
    status, body, _ = api("/api/wf/raw", {"run_id": "run-1", "path": "../secret.txt"})
    assert status in (400, 403)
    status, body, _ = api("/api/wf/raw", {"run_id": "run-1", "path": "kb/modules.json"})
    assert status == 403
    # A declared path must not be usable as a springboard to another file.
    status, body, _ = api("/api/wf/raw", {"run_id": "run-1",
                                          "path": "episodes/demo-report.md/../demo-ok.md"})
    assert status in (400, 403)
