"""W0 evidence contract: deterministic executors must prove their artifacts.

These tests pin the behaviour that used to be a silent lie:
a deterministic node reported ``passed`` while carrying no artifact evidence and
a checklist of ``False`` values.
"""
import importlib.util
from pathlib import Path

from test_canvas_automation import canvas, state_of


def _single_node(wf, module):
    wf.write_json(wf.MODULES_PATH, {"modules": [module]})
    wf.write_json(wf.CANVAS_PATH, {"nodes": [{"id": "W", "module": module["id"]}], "edges": []})


def _module(executor, artifacts):
    return {"id": "W", "name": "W", "executor": executor, "retry": 0,
            "artifacts": list(artifacts), "checklist": ["free-text item only a human can confirm"]}


def test_deterministic_node_records_evidence_and_no_fake_checks(canvas):
    wf, kb, root, calls, adapter, success = canvas

    def write(node, stem, run_id, state):
        target = root / "episodes" / (stem + "-out.txt")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("payload", encoding="utf-8")
        return wf.STATUS_DONE, "written"

    wf.EXECUTORS["fixture_writer"] = write
    _single_node(wf, _module("fixture_writer", ["episodes/{stem}-out.txt"]))
    assert wf.canvas_run("evidence-one") == 0

    node = state_of(wf)["nodes"][0]
    assert node["status"] == "passed"
    assert node["checks"] == []
    assert node["checks_source"] == "artifacts"
    evidence = node["artifact_evidence"]
    assert len(evidence) == 1
    assert evidence[0]["logical_path"] == "episodes/evidence-one-out.txt"
    assert "/generation-1/" in evidence[0]["path"]
    assert (root / evidence[0]["path"]).read_text()=="payload"
    assert evidence[0]["bytes"] == len("payload")
    assert len(evidence[0]["sha256"]) == 64


def test_deterministic_node_without_artifact_fails(canvas):
    wf, kb, root, *_ = canvas

    def noop(node, stem, run_id, state):
        return wf.STATUS_DONE, "done"

    wf.EXECUTORS["fixture_noop"] = noop
    _single_node(wf, _module("fixture_noop", ["episodes/{stem}-missing.txt"]))
    assert wf.canvas_run("evidence-two") == 6

    node = state_of(wf)["nodes"][0]
    assert node["status"] == "awaiting_input"
    assert "缺少产物" in node["detail"]
    assert node.get("question")
    assert not node.get("artifact_evidence")


def test_empty_directory_artifact_fails(canvas):
    wf, kb, root, *_ = canvas

    def empty_dir(node, stem, run_id, state):
        (root / "artifacts" / run_id / "empty").mkdir(parents=True, exist_ok=True)
        return wf.STATUS_DONE, "dir"

    wf.EXECUTORS["fixture_dir"] = empty_dir
    _single_node(wf, _module("fixture_dir", ["artifacts/{run_id}/empty"]))
    assert wf.canvas_run("evidence-three") == 6

    node = state_of(wf)["nodes"][0]
    assert node["status"] == "awaiting_input"
    assert "产物为空" in node["detail"]


def test_agent_checklist_still_starts_unchecked(canvas):
    wf, kb, root, calls, adapter, success = canvas
    _single_node(wf, _module("agent", ["episodes/{stem}-agent.md"]))
    assert wf.canvas_run("evidence-four") == 0
    node = state_of(wf)["nodes"][0]
    assert node["status"] == "passed"
    assert node["checks"] == [True]


def _server_module():
    source = ((Path(__file__).resolve().parents[2] / "server")
              / "canvas_editor_server.py")
    spec = importlib.util.spec_from_file_location("isolated_canvas_annotation", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_run_state_annotation_flags_missing_evidence(tmp_path, monkeypatch):
    module = _server_module()
    monkeypatch.setattr(module, "BASE", str(tmp_path))
    monkeypatch.setattr(module, "RUNS", str(tmp_path / ".runs"))
    (tmp_path / "episodes").mkdir()
    (tmp_path / "episodes" / "keep.txt").write_text("x", encoding="utf-8")
    (tmp_path / ".runs" / "run-1").mkdir(parents=True)

    state = {"run_id": "run-1", "nodes": [
        {"id": "a", "artifact_evidence": [{"path": "episodes/keep.txt"}, {"path": "episodes/gone.txt"}]},
        {"id": "b", "artifact_evidence": [{"path": "artifacts/x/y"}]},
    ]}
    out = module.annotate_run(state)
    assert out["nodes"][0]["evidence_missing"] == ["episodes/gone.txt"]
    assert out["evidence_missing_count"] == 2
    assert out["audit_trail"] is False

    (tmp_path / ".runs" / "run-1" / "events.jsonl").write_text("", encoding="utf-8")
    assert module.annotate_run(state)["audit_trail"] is True


def test_annotation_ignores_untrusted_evidence_paths(tmp_path, monkeypatch):
    module = _server_module()
    monkeypatch.setattr(module, "BASE", str(tmp_path))
    monkeypatch.setattr(module, "RUNS", str(tmp_path / ".runs"))
    state = {"run_id": "../escape", "nodes": [
        {"id": "a", "artifact_evidence": [{"path": "../../etc/passwd"}, {"path": "/abs/path"}]},
    ]}
    out = module.annotate_run(state)
    assert out["evidence_missing_count"] == 0
    assert out["audit_trail"] is False
