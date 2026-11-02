"""API contract tests; every writable path points at pytest's disposable fixture."""
import importlib.util
import json
from pathlib import Path
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request

import pytest


ROOT = (Path(__file__).resolve().parents[2] / "server")


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def api(tmp_path, monkeypatch):
    kb = load("isolated_api_knowledge", ROOT / "kb.py")
    for key, value in {"BASE": tmp_path, "KB": tmp_path / "kb" / "knowledge",
                       "SHARED": tmp_path / "kb" / "knowledge" / "shared",
                       "TASKS": tmp_path / "kb" / "knowledge" / "tasks",
                       "INDEX": tmp_path / "kb" / "knowledge" / "index.json"}.items():
        monkeypatch.setattr(kb, key, str(value))
    kb.ensure_task("任务甲")
    kb.ensure_task("任务乙")
    (Path(kb.TASKS) / "任务甲" / "资料.md").write_text("# 标题\n正文独有词橘子", encoding="utf-8")
    (Path(kb.TASKS) / "任务乙" / "资料.md").write_text("# Other\nFOREIGN_TASK_MARKER", encoding="utf-8")
    Path(kb.SHARED).mkdir(exist_ok=True)
    (Path(kb.SHARED) / "公共.md").write_text("GLOBAL_SHARED_MARKER", encoding="utf-8")
    module = load("isolated_canvas_api", ROOT / "canvas_editor_server.py")
    monkeypatch.setattr(module, "BASE", str(tmp_path))
    monkeypatch.setattr(module, "RUNS", str(tmp_path / ".runs"))
    monkeypatch.setattr(module, "knowledge_module", lambda: kb, raising=False)
    commands = []
    monkeypatch.setattr(module, "start_wf", lambda args: commands.append(args))
    monkeypatch.setattr(module.Handler, "log_message", lambda *args: None)
    server = module.ThreadingHTTPServer(("127.0.0.1", 0), module.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def request(endpoint, data=None, headers=None, method=None):
        body = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request("http://127.0.0.1:" + str(server.server_address[1]) + endpoint,
                                     data=body, headers={"Content-Type": "application/json", "X-Canvas-Api-Token": module.API_TOKEN, **(headers or {})}, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)

    yield module, kb, request, commands
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def test_unscoped_knowledge_reads_only_shared_partition(api):
    _, _, request, _ = api
    for endpoint in ("/api/kb/list", "/api/kb/search?q=GLOBAL_SHARED_MARKER"):
        status, data = request(endpoint)
        assert status == 200
        serialized=json.dumps(data,ensure_ascii=False)
        assert "FOREIGN_TASK_MARKER" not in serialized and "tasks/任务" not in serialized
    assert request("/api/kb/read?path=index.json")[0] == 400


def test_encoded_chinese_path_and_fulltext_search(api):
    _, _, request, _ = api
    query = urllib.parse.urlencode({"task_id": "任务甲", "path": "tasks/任务甲/资料.md"})
    status, result = request("/api/kb/read?" + query)
    assert status == 200
    assert "橘子" in result["content"]
    query = urllib.parse.urlencode({"task_id": "任务甲", "q": "橘子"})
    status, result = request("/api/kb/search?" + query)
    assert status == 200
    assert any(e["path"].endswith("资料.md") for e in result["entries"])


def test_other_partition_rejected_but_shared_is_allowed(api):
    _, _, request, _ = api
    for path in ("tasks/任务乙/资料.md", "../knowledge-neighbor/probe.md"):
        status, result = request("/api/kb/read?" + urllib.parse.urlencode({"task_id": "任务甲", "path": path}))
        assert status in {400, 403}
        assert "FOREIGN_TASK_MARKER" not in json.dumps(result)
    status, result = request("/api/kb/read?" + urllib.parse.urlencode({"task_id": "任务甲", "path": "shared/公共.md"}))
    assert status == 200
    assert "GLOBAL_SHARED_MARKER" in json.dumps(result, ensure_ascii=False)
    status, result = request("/api/kb/list?" + urllib.parse.urlencode({"task_id": "任务甲"}))
    assert status == 200
    scopes = {e["scope"] for e in result["index"]["entries"]}
    assert "shared" in scopes and "task" in scopes


def test_new_partition_returns_canonical_id(api):
    _, _, request, _ = api
    status, result = request("/api/kb/task", {"name": "task A"})
    assert status == 200
    assert result["task"] == "task-A"


def test_skill_catalog_reports_provenance_without_instruction_payload(api):
    module, _, request, _ = api
    file = Path(module.BASE) / "kb/skill-registry.json"
    file.write_text(json.dumps({"skills": [{"id": "sample", "name": "Sample", "root": "skills/vendor/sample", "entry": "SKILL.md", "commit": "a" * 40,
        "source_url": "https://github.com/example/skills", "license": "MIT", "instructions": "INTERNAL_INSTRUCTIONS"}]}), encoding="utf-8")
    status, result = request("/api/skills")
    assert status == 200
    assert result["skills"][0]["commit"] == "a" * 40
    assert "INTERNAL_INSTRUCTIONS" not in json.dumps(result)


def test_create_module_persists_valid_agent_definition(api):
    module, _, request, _ = api
    status, result = request("/api/modules/create", {"name": "整理采访重点", "instructions": "读取本任务资料并整理重点", "checklist": ["保留来源"]})
    assert status == 200
    created = result["module"]
    assert created["name"] == "整理采访重点"
    assert created["executor"] == "agent"
    assert created["agent_prompt"] == "读取本任务资料并整理重点"
    assert created["checklist"] == ["保留来源"]
    assert created["artifacts"][0].startswith("episodes/{stem}-")
    saved = json.loads((Path(module.BASE) / "kb/modules.json").read_text(encoding="utf-8"))
    assert saved["modules"] == [created]


@pytest.mark.parametrize("body", [{"name": "", "instructions": "x"}, {"name": "x", "instructions": ""}, {"name": "x", "instructions": "x", "checklist": "bad"}])
def test_create_module_rejects_incomplete_form(api, body):
    _, _, request, _ = api
    assert request("/api/modules/create", body)[0] == 400


def test_delete_from_library_preserves_existing_canvas_definition(api):
    module, _, request, _ = api
    _, created = request("/api/modules/create", {"name": "整理重点", "instructions": "整理"})
    mid = created["module"]["id"]
    graph = {"nodes": [{"id": "step", "module": mid}], "edges": []}
    graph_file = Path(module.BASE) / "kb/canvas-workflow.json"
    graph_file.write_text(json.dumps(graph), encoding="utf-8")
    status, result = request("/api/modules/delete", {"id": mid})
    assert status == 200
    assert next(m for m in result["modules"] if m["id"] == mid)["archived"] is True
    assert json.loads(graph_file.read_text(encoding="utf-8")) == graph
    assert request("/api/modules/delete", {"id": "unknown"})[0] == 400


def test_reply_validates_waiting_node_and_starts_resume(api):
    module, _, request, commands = api
    run_dir = Path(module.RUNS) / "test-run"
    run_dir.mkdir(parents=True)
    (run_dir / "state.json").write_text(json.dumps({"status": "awaiting_input", "nodes": [{"id": "A", "status": "awaiting_input"}]}), encoding="utf-8")
    status, _ = request("/api/wf/reply", {"run_id": "test-run", "node": "A", "answer": "Use blue"})
    assert status == 200
    queued=module.plugin_module('wf_commands').pending(module.BASE, lambda pid: True)
    assert len(queued)==1 and queued[0]['action']=='reply' and not commands
    assert json.loads(queued[0]['payload'])['answer']=='Use blue'
    status, _ = request("/api/wf/reply", {"run_id": "../outside", "node": "A", "answer": "x"})
    assert status == 400


@pytest.mark.parametrize("endpoint,query", [
    ("run-state", {"run_id": "../outside"}),
    ("task", {"run_id": "valid", "node": "../../kb/knowledge/tasks/任务乙/资料"}),
    ("logs", {"run_id": "../outside"}),
])
def test_run_read_routes_reject_path_traversal(api, endpoint, query):
    _, _, request, _ = api
    status, result = request("/api/wf/" + endpoint + "?" + urllib.parse.urlencode(query))
    assert status == 400
    assert "FOREIGN_TASK_MARKER" not in json.dumps(result)


@pytest.mark.parametrize("method", ["GET", "POST", "OPTIONS"])
def test_untrusted_web_origin_cannot_access_local_api(api, method):
    _, _, request, commands = api
    status, _ = request("/api/health", {} if method == "POST" else None,
                        headers={"Origin": "https://untrusted.example"}, method=method)
    assert status == 403
    assert not commands


@pytest.mark.parametrize("endpoint,body", [
    ("cancel", {"run_id": "../outside"}),
    ("rerun", {"run_id": "../outside", "from": "A"}),
    ("agent-done", {"run_id": "../outside", "node": "A"}),
    ("agent-skip", {"run_id": "valid", "node": "../../A"}),
    ("check", {"run_id": "../outside", "node": "A"}),
    ("approve", {"run_id": "../outside"}),
    ("reject", {"run_id": "../outside"}),
])
def test_run_write_routes_reject_path_traversal(api, endpoint, body):
    module, _, request, commands = api
    outside = Path(module.BASE) / "outside"
    outside.mkdir()
    status, _ = request("/api/wf/" + endpoint, body)
    assert status == 400
    assert not commands
    assert not (outside / ".cancel").exists()


def test_revision_restore_rejects_absolute_and_traversal(api):
    module, _, request, commands = api
    for revision in ("../../knowledge/tasks/任务乙/task.json", str(Path(module.BASE) / "elsewhere.json")):
        status, _ = request("/api/canvas/restore", {"id": revision})
        assert status == 400
    assert not commands


def test_run_read_refuses_link_outside_run_directory(api):
    module, _, request, _ = api
    run = Path(module.RUNS) / "valid"
    run.mkdir(parents=True)
    outside = Path(module.BASE) / "outside"
    outside.mkdir()
    (outside / "state.json").write_text('{"secret":"FOREIGN_TASK_MARKER"}', encoding="utf-8")
    try:
        (run / "state.json").symlink_to(outside / "state.json")
    except OSError:
        import subprocess
        # Directory junctions work on Windows without developer-mode symlink permission.
        run.rmdir()
        subprocess.run(["cmd", "/c", "mklink", "/J", str(run), str(outside)], capture_output=True, check=True)
    status, result = request("/api/wf/run-state?run_id=valid")
    assert status == 400
    assert "FOREIGN_TASK_MARKER" not in json.dumps(result)


def test_workflow_library_crud_and_switching(api):
    _, _, request, _ = api
    status, initial = request("/api/workflows")
    assert status == 200 and initial["active"] and initial["workflows"]
    original = initial["active"]
    status, created = request("/api/workflows/create", {"name": "写作流程"})
    assert status == 200
    wid = created["active"]
    assert wid != original
    status, canvas = request("/api/canvas")
    assert status == 200 and canvas["workflow"]["id"] == wid
    status, renamed = request("/api/workflows/rename", {"id": wid, "name": "写作流程A"})
    assert status == 200 and renamed["workflow"]["name"] == "写作流程A"
    status, selected = request("/api/workflows/select", {"id": original})
    assert status == 200 and selected["active"] == original
    status, deleted = request("/api/workflows/delete", {"id": wid})
    assert status == 200
    status, final = request("/api/workflows")
    assert status == 200 and wid not in {w["id"] for w in final["workflows"]}
    assert final["active"] == original


def test_custom_module_declares_inputs_outputs_and_timeout(api):
    _, _, request, _ = api
    status, result = request("/api/modules/create", {
        "name": "接口契约模块",
        "instructions": "读取输入并写产物",
        "checklist": ["产物存在"],
        "inputs": ["episodes/{stem}-research.md"],
        "outputs": ["episodes/{stem}-notes.md"],
        "timeout_seconds": 900,
    })
    assert status == 200
    module = result["module"]
    assert module["requires"] == ["episodes/{stem}-research.md"]
    assert module["artifacts"] == ["episodes/{stem}-notes.md"]
    assert module["timeout_seconds"] == 900
    assert module["version"] == 1
    status, bad = request("/api/modules/create", {"name": "坏路径", "instructions": "x",
                                                 "outputs": ["../outside.md"]})
    assert status == 400


def test_active_workflow_converges_to_canvas_mirror(api):
    module, _, _, _ = api
    wdir = Path(module.workflows_dir())
    wdir.mkdir(parents=True, exist_ok=True)
    wid = "video"
    (wdir / (wid + ".json")).write_text(json.dumps({
        "id": wid, "name": "视频制作", "graph": {"nodes": [{"id": "old"}], "edges": []}
    }), encoding="utf-8")
    (wdir / "index.json").write_text(json.dumps({"active": wid, "workflows": [
        {"id": wid, "name": "视频制作"}
    ]}), encoding="utf-8")
    (Path(module.BASE) / "kb" / "canvas-workflow.json").write_text(json.dumps({
        "nodes": [{"id": "a"}, {"id": "b"}], "edges": [{"from": "a", "to": "b"}]
    }), encoding="utf-8")
    module.sync_active_workflow_from_mirror()
    saved = json.loads((wdir / (wid + ".json")).read_text(encoding="utf-8"))
    assert len(saved["graph"]["nodes"]) == 2
    assert len(saved["graph"]["edges"]) == 1


def test_run_artifact_preview_and_scope(api):
    module, _, request, _ = api
    rid = "20261030-000000-test"
    run = Path(module.RUNS) / rid
    run.mkdir(parents=True, exist_ok=True)
    episodes = Path(module.BASE) / "episodes"
    episodes.mkdir(parents=True, exist_ok=True)
    (episodes / "preview.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    (run / "state.json").write_text(json.dumps({
        "run_id": rid, "status": "completed", "nodes": [{
            "id": "n1", "status": "passed",
            "artifact_evidence": [{"path": "episodes/preview.png", "sha256": "x", "bytes": 40}]
        }]
    }), encoding="utf-8")
    status, rows = request("/api/wf/artifacts?run_id=%s&node=n1" % rid)
    assert status == 200 and rows["artifacts"][0]["kind"] == "image"
    status, image = request("/api/wf/artifact?run_id=%s&path=%s" % (rid, urllib.parse.quote("episodes/preview.png")))
    assert status == 200 and image["kind"] == "image" and image["data"]
    status, forbidden = request("/api/wf/artifact?run_id=%s&path=episodes/other.png" % rid)
    assert status == 403


def test_user_visual_review_writes_attestation(api):
    module, _, request, _ = api
    rid = "20261030-000001-visual"
    run = Path(module.RUNS) / rid
    ws = run / "n" / "abc" / "g1" / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    episodes = Path(module.BASE) / "episodes"
    episodes.mkdir(parents=True, exist_ok=True)
    (episodes / "frame.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 16)
    (run / "state.json").write_text(json.dumps({
        "run_id": rid, "episode": "demo", "status": "awaiting_input",
        "nodes": [{"id": "qc", "status": "awaiting_input", "workspace": str(ws),
                   "artifact_evidence": [{"path": "episodes/frame.png", "sha256": "x", "bytes": 24}]}]
    }), encoding="utf-8")
    status, result = request("/api/wf/visual-review", {"run_id": rid, "node": "qc"})
    assert status == 200 and result["review"]["status"] == "PASS"
    saved = json.loads((ws / "visual-review.json").read_text(encoding="utf-8"))
    assert saved["status"] == "PASS" and saved["reviewer"] == "user"
    assert saved["frames"] == ["episodes/frame.png"]
    project = json.loads((Path(module.BASE) / "episodes" / "demo-visual-review.json").read_text(encoding="utf-8"))
    assert project["status"] == "PASS"


def test_workflow_export_and_invalid_import(api):
    _, _, request, _ = api
    status, exported = request("/api/workflows/export")
    assert status == 200
    workflow = exported["workflow"]
    assert workflow["schema"] == "dsh.workflow/v1"
    assert workflow["tier"] in ("experimental", "stable")
    assert isinstance(workflow["graph"], dict)
    assert isinstance(workflow.get("dependencies"), list)
    status, invalid = request("/api/workflows/import", {"workflow": {"name": "坏导入", "graph": {"nodes": []}}})
    assert status == 400


def test_custom_module_can_be_updated(api):
    _, _, request, _ = api
    status, made = request("/api/modules/create", {
        "name": "可编辑模块", "instructions": "旧说明", "checklist": ["旧验收"],
        "outputs": ["episodes/old.md"],
    })
    assert status == 200
    mid = made["module"]["id"]
    status, updated = request("/api/modules/update", {
        "id": mid, "name": "可编辑模块 v2", "instructions": "新说明", "checklist": ["新验收"],
        "inputs": ["episodes/in.md"], "outputs": ["episodes/new.md"], "timeout_seconds": 600,
    })
    assert status == 200
    module = updated["module"]
    assert module["name"] == "可编辑模块 v2"
    assert module["agent_prompt"] == "新说明"
    assert module["requires"] == ["episodes/in.md"]
    assert module["artifacts"] == ["episodes/new.md"]
    assert module["timeout_seconds"] == 600
    assert module["version"] == 2


def test_module_history_and_rollback(api):
    _, _, request, _ = api
    status, made = request("/api/modules/create", {"name": "历史模块", "instructions": "v1 说明"})
    assert status == 200
    mid = made["module"]["id"]
    status, _ = request("/api/modules/update", {"id": mid, "name": "历史模块 v2", "instructions": "v2 说明"})
    assert status == 200
    status, hist = request("/api/modules/history?id=" + mid)
    assert status == 200
    versions = {item["version"] for item in hist["versions"]}
    assert 1 in versions and 2 in versions
    status, rolled = request("/api/modules/rollback", {"id": mid, "version": 1})
    assert status == 200
    module = rolled["module"]
    assert module["name"] == "历史模块"
    assert module["agent_prompt"] == "v1 说明"
    assert module["version"] == 3
