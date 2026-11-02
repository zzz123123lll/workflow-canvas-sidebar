"""Canvas automation contract tests. All data is redirected to pytest temp paths."""
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest


SOURCE = (Path(__file__).resolve().parents[2] / "server")


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def canvas(tmp_path, monkeypatch):
    wf = load_module("isolated_wf_automation", SOURCE / "wf.py")
    kb = load_module("isolated_kb_automation", SOURCE / "kb.py")
    for name, value in {
        "BASE": str(tmp_path), "DEF_PATH": str(tmp_path / "kb/workflow-def.json"),
        "MODULES_PATH": str(tmp_path / "kb/modules.json"), "CANVAS_PATH": str(tmp_path / "kb/canvas-workflow.json"),
        "RUNS": str(tmp_path / ".runs"), "LATEST": str(tmp_path / ".runs/LATEST"),
        "EPISODES": str(tmp_path / "episodes"), "LOCKFILE": str(tmp_path / ".runs/.lock"),
        "WATCH_LOCK": str(tmp_path / ".runs/.watch.lock"),
    }.items():
        monkeypatch.setattr(wf, name, value)
    for name, value in {
        "BASE": str(tmp_path), "KB": str(tmp_path / "kb/knowledge"),
        "SHARED": str(tmp_path / "kb/knowledge/shared"), "TASKS": str(tmp_path / "kb/knowledge/tasks"),
        "INDEX": str(tmp_path / "kb/knowledge/index.json"),
    }.items():
        monkeypatch.setattr(kb, name, value)
    monkeypatch.setitem(sys.modules, "kb", kb)
    wf.write_json(wf.DEF_PATH, {"nodes": [], "retention": {"auto": False}})
    modules = [{"id": name, "name": name, "executor": "agent", "retry": 0,
                "agent_prompt": "Execute original " + name,
                "artifacts": ["episodes/{stem}-" + name + ".md"], "checklist": ["correct output"]}
               for name in ("A", "B")]
    wf.write_json(wf.MODULES_PATH, {"modules": modules})
    wf.write_json(wf.CANVAS_PATH, {"nodes": [{"id": name, "module": name} for name in ("A", "B")],
                                  "edges": [{"from": "A", "to": "B"}]})
    calls = []

    def success(task, **kwargs):
        calls.append((task, kwargs))
        for rel in task["expected_artifacts"]:
            path = Path(kwargs["workspace"]) / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fresh " + task["node_id"], encoding="utf-8")
        return {"status": "completed", "summary": "done", "artifacts": task["expected_artifacts"],
                "checks": [True] * len(task["checklist"]), "session_id": kwargs["session_id"], "evidence": {"fixture": True}}

    adapter = types.SimpleNamespace(run_agent=success)
    monkeypatch.setitem(sys.modules, "wf_agent", adapter)
    return wf, kb, tmp_path, calls, adapter, success


def state_of(wf):
    return wf.read_json(Path(wf.RUNS) / wf.latest_run() / "state.json", {})


def test_state_replace_retries_windows_reader_sharing_collision(canvas, monkeypatch):
    wf, _, root, _, _, _ = canvas
    original = wf.os.replace
    attempts = []
    def busy(source, destination):
        attempts.append(1)
        if len(attempts) < 3:
            error = PermissionError("temporary reader lock")
            error.winerror = 5
            raise error
        return original(source, destination)
    monkeypatch.setattr(wf.os, "replace", busy)
    wf.write_json(root / "state.json", {"ok": True})
    assert len(attempts) == 3
    assert wf.read_json(root / "state.json", {}) == {"ok": True}


def test_one_start_executes_arrows_and_passes_upstream_artifacts(canvas):
    wf, kb, root, calls, _, _ = canvas
    task_id = kb.ensure_task("task A")
    (root / "kb/knowledge/tasks" / task_id / "research.md").write_text("# Own\nOWN_TASK_MARKER", encoding="utf-8")
    (root / "kb/knowledge/shared").mkdir()
    (root / "kb/knowledge/shared/private.md").write_text("SHARED_FORBIDDEN_MARKER", encoding="utf-8")
    other = kb.ensure_task("other")
    (root / "kb/knowledge/tasks" / other / "private.md").write_text("OTHER_FORBIDDEN_MARKER", encoding="utf-8")
    assert wf.canvas_run("task A") == 0
    state = state_of(wf)
    assert state["status"] == "completed"
    assert state["task_id"] == task_id
    assert [task["node_id"] for task, _ in calls] == ["A", "B"]
    for task, _ in calls:
        assert "OWN_TASK_MARKER" in task["knowledge_context"]
        assert "SHARED_FORBIDDEN_MARKER" in task["knowledge_context"]
        assert "OTHER_FORBIDDEN_MARKER" not in task["knowledge_context"]
    upstream = calls[1][0]["upstream_inputs"]
    assert upstream and upstream[0]["node_id"] == "A"
    assert (Path(calls[1][1]["workspace"]) / upstream[0]["path"]).read_text(encoding="utf-8") == "fresh A"
    assert all(n["artifact_evidence"] for n in state["nodes"])


def test_old_project_files_cannot_skip_real_execution(canvas):
    wf, _, root, calls, _, _ = canvas
    (root / "episodes").mkdir()
    (root / "episodes/reuse-A.md").write_text("old production file", encoding="utf-8")
    assert wf.canvas_run("reuse") == 0
    assert [task["node_id"] for task, _ in calls] == ["A", "B"]
    assert (root / "episodes/reuse-A.md").read_text(encoding="utf-8") == "fresh A"


def test_bound_skill_instructions_reach_executor_and_are_recorded(canvas, monkeypatch):
    wf, _, _, calls, _, _ = canvas
    import wf_skills
    staged = []
    def stage(ids, workspace, **kwargs):
        staged.append(ids)
        return [{"id": value, "instructions": "PINNED_SKILL_INSTRUCTIONS", "entry": "skills/" + value + "/SKILL.md", "commit": "a" * 40} for value in ids]
    monkeypatch.setattr(wf_skills, "stage_skills", stage)
    modules = wf.read_json(wf.MODULES_PATH, {})
    modules["modules"][0]["skills"] = ["test-skill"]
    wf.write_json(wf.MODULES_PATH, modules)
    assert wf.canvas_run("skill-proof") == 0
    assert staged[0] == ["test-skill"]
    assert calls[0][0]["skills"][0]["instructions"] == "PINNED_SKILL_INSTRUCTIONS"
    assert state_of(wf)["nodes"][0]["loaded_skills"][0]["commit"] == "a" * 40


def test_missing_bound_skill_pauses_without_calling_model(canvas):
    wf, _, _, calls, _, _ = canvas
    modules = wf.read_json(wf.MODULES_PATH, {})
    modules["modules"][0]["skills"] = ["missing-skill"]
    wf.write_json(wf.MODULES_PATH, modules)
    assert wf.canvas_run("missing-skill-proof") == 6
    assert state_of(wf)["status"] == "awaiting_input"
    assert not calls


def test_runtime_progress_is_saved_and_terminal_stops_worker_signal(canvas):
    wf, _, _, _, adapter, success = canvas
    snapshots = []
    def live(task, **kwargs):
        kwargs["on_progress"]({"phase": "tool", "worker_alive": True, "worker_pid": 12345,
                               "heartbeat_at": 1000, "event_count": 7, "tool_calls": 1, "tool_name": "pwsh"})
        snapshots.append(state_of(wf))
        return success(task, **kwargs)
    adapter.run_agent = live
    assert wf.canvas_run("live-proof") == 0
    assert snapshots[0]["nodes"][0]["live"]["phase"] == "tool"
    assert snapshots[0]["nodes"][0]["live"]["event_count"] == 7
    assert snapshots[0]["nodes"][0]["supervisor_heartbeat_at"] > 0
    assert state_of(wf)["nodes"][0]["live"]["worker_alive"] is False


def test_directory_project_passes_to_downstream_with_verified_hash(canvas):
    wf, _, root, calls, adapter, success = canvas
    modules = wf.read_json(wf.MODULES_PATH, {})
    modules["modules"][0]["artifacts"] = ["artifacts/{run_id}/project"]
    wf.write_json(wf.MODULES_PATH, modules)
    def create_project(task, **kwargs):
        if task["node_id"] == "A":
            calls.append((task, kwargs))
            project = Path(kwargs["workspace"]) / task["expected_artifacts"][0]
            project.mkdir(parents=True)
            (project / "index.tsx").write_text("// simulated project, no media", encoding="utf-8")
            return {"status": "completed", "checks": [True], "artifacts": task["expected_artifacts"]}
        return success(task, **kwargs)
    adapter.run_agent = create_project
    assert wf.canvas_run("directory-proof") == 0
    previous = calls[1][0]["upstream_inputs"][0]
    assert (Path(calls[1][1]["workspace"]) / previous["path"] / "index.tsx").is_file()
    assert state_of(wf)["nodes"][0]["artifact_evidence"][0]["kind"] == "directory"


def test_independent_validation_failure_cannot_promote_claimed_success(canvas, monkeypatch):
    wf, _, root, _, _, _ = canvas
    import wf_validation
    def reject(*args):
        raise ValueError("Independent media check failed")
    monkeypatch.setattr(wf_validation, "validate_node", reject)
    modules = wf.read_json(wf.MODULES_PATH, {})
    modules["modules"][0]["validation"] = {"type": "video_render"}
    wf.write_json(wf.MODULES_PATH, modules)
    assert wf.canvas_run("validation-proof") == 6
    assert not (root / "episodes/validation-proof-A.md").exists()
    assert "Independent media" in state_of(wf)["nodes"][0]["detail"]


def test_cancel_during_independent_validation_prevents_promotion(canvas, monkeypatch):
    wf, _, root, _, _, _ = canvas
    import wf_validation
    def cancel(node, task, workspace, tools):
        Path(wf.RUNS, wf.latest_run(), ".cancel").write_text("1", encoding="utf-8")
        return {"checked": True}
    monkeypatch.setattr(wf_validation, "validate_node", cancel)
    modules = wf.read_json(wf.MODULES_PATH, {})
    modules["modules"][0]["validation"] = {"type": "video_render"}
    wf.write_json(wf.MODULES_PATH, modules)
    assert wf.canvas_run("cancel-validation-proof") == 130
    assert not (root / "episodes/cancel-validation-proof-A.md").exists()


def test_verified_outputs_archive_to_current_partition_and_next_step_reads_them(canvas):
    wf, kb, root, calls, _, _ = canvas
    modules = wf.read_json(wf.MODULES_PATH, {})
    modules["modules"][0]["knowledge_aliases"] = {"episodes/{stem}-A.md": "brief.md"}
    wf.write_json(wf.MODULES_PATH, modules)
    assert wf.canvas_run("archive-proof") == 0
    partition = root / "kb/knowledge/tasks/archive-proof"
    assert (partition / "outputs/episodes/archive-proof-A.md").read_text(encoding="utf-8") == "fresh A"
    assert (partition / "brief.md").read_text(encoding="utf-8") == "fresh A"
    assert "fresh A" in calls[1][0]["knowledge_context"]
    first = state_of(wf)["nodes"][0]
    assert len(first["knowledge_evidence"]) == 2
    assert any(e["path"].endswith("brief.md") for e in kb.list_entries(task="archive-proof"))


def test_knowledge_alias_follows_custom_output_filename(canvas):
    wf, _, root, _, _, _ = canvas
    modules = wf.read_json(wf.MODULES_PATH, {})
    modules["modules"][0]["knowledge_aliases"] = {"episodes/{stem}-A.md": "brief.md"}
    wf.write_json(wf.MODULES_PATH, modules)
    graph = wf.read_json(wf.CANVAS_PATH, {})
    graph["nodes"][0]["artifacts"] = ["episodes/{stem}-custom.md"]
    wf.write_json(wf.CANVAS_PATH, graph)
    assert wf.canvas_run("custom-output") == 0
    assert (root / "kb/knowledge/tasks/custom-output/brief.md").read_text(encoding="utf-8") == "fresh A"


@pytest.mark.parametrize("path", [
    "KB/knowledge/tasks/other/private.md", "kb/knowledge/tasks/other/private.md",
    "kb./knowledge/tasks/other/private.md", "kb /knowledge/tasks/other/private.md",
    ".RUNS/control.md",
])
def test_artifact_aliases_cannot_overwrite_other_task_or_control_files(canvas, path):
    wf, kb, _, _, _, _ = canvas
    kb.ensure_task("own")
    kb.ensure_task("other")
    with pytest.raises(ValueError):
        wf.artifact_relative(path, "own", "test-run", "own")


def test_claimed_completion_without_fresh_output_pauses(canvas):
    wf, _, root, calls, adapter, _ = canvas
    (root / "episodes").mkdir()
    (root / "episodes/reuse-A.md").write_text("old", encoding="utf-8")

    def empty(task, **kwargs):
        calls.append((task, kwargs))
        return {"status": "completed", "summary": "not actually written", "checks": [True], "artifacts": task["expected_artifacts"]}

    adapter.run_agent = empty
    assert wf.canvas_run("reuse") != 0
    state = state_of(wf)
    assert state["status"] == "awaiting_input"
    assert state["nodes"][0]["question"]
    assert state["nodes"][1]["status"] == "pending"
    assert (root / "episodes/reuse-A.md").read_text(encoding="utf-8") == "old"


def test_question_reply_resumes_same_node_and_frozen_module(canvas):
    wf, _, _, calls, adapter, success = canvas

    def ask(task, **kwargs):
        if task["node_id"] == "A" and not task["user_answers"]:
            calls.append((task, kwargs))
            return {"status": "needs_input", "summary": "need color", "question": "Which color?", "session_id": kwargs["session_id"], "evidence": {"finish_reason": "completed"}}
        return success(task, **kwargs)

    adapter.run_agent = ask
    assert wf.canvas_run("reply") == 6
    state = state_of(wf)
    assert state["status"] == "awaiting_input"
    assert state["nodes"][1]["status"] == "pending"
    original_session = calls[0][1]["session_id"]
    modules = wf.read_json(wf.MODULES_PATH, {})
    modules["modules"][0]["agent_prompt"] = "CHANGED_PROMPT_NOT_ALLOWED"
    wf.write_json(wf.MODULES_PATH, modules)
    assert wf.canvas_reply(state["run_id"], "A", "blue") == 0
    assert state_of(wf)["status"] == "completed"
    assert [task["node_id"] for task, _ in calls] == ["A", "A", "B"]
    assert calls[1][1]["session_id"] == original_session
    assert calls[1][1]["resume"] is True
    assert calls[1][0]["user_answers"] == ["blue"]
    assert "CHANGED_PROMPT_NOT_ALLOWED" not in calls[1][0]["objective"]
    with pytest.raises(ValueError):
        wf.canvas_reply(state["run_id"], "A", "late answer")


def test_cancelled_agent_does_not_start_downstream(canvas):
    wf, _, _, calls, adapter, _ = canvas

    def cancel(task, **kwargs):
        calls.append((task, kwargs))
        Path(kwargs["cancel_path"]).write_text("1", encoding="utf-8")
        return {"status": "cancelled", "summary": "cancelled"}

    adapter.run_agent = cancel
    assert wf.canvas_run("cancel") == 130
    assert state_of(wf)["status"] == "cancelled"
    assert len(calls) == 1


def test_rerun_cannot_modify_state_before_exclusive_lock(canvas):
    wf, _, _, _, _, _ = canvas
    assert wf.canvas_run("rerun-lock") == 0
    before = state_of(wf)
    assert wf.acquire_lock("another-execution")
    try:
        assert wf.canvas_rerun(before["run_id"], "A") == 5
        assert state_of(wf) == before
    finally:
        wf.release_lock()


def test_rerun_uses_new_generation_without_reusing_old_workspace(canvas):
    wf, _, _, calls, _, _ = canvas
    assert wf.canvas_run("rerun-generation") == 0
    first = state_of(wf)
    old_workspace = calls[0][1]["workspace"]
    assert wf.canvas_rerun(first["run_id"], "A") == 0
    assert len(calls) == 4
    assert calls[2][1]["workspace"] != old_workspace
    assert calls[2][1]["resume"] is False


def test_failed_rerun_does_not_present_previous_archive_as_new_evidence(canvas):
    wf, _, _, _, adapter, _ = canvas
    assert wf.canvas_run("rerun-proof") == 0
    state = state_of(wf)
    assert state["nodes"][0]["knowledge_evidence"]
    adapter.run_agent = lambda *args, **kwargs: {"status": "failed", "summary": "unavailable"}
    assert wf.canvas_rerun(state["run_id"], "A") == 6
    assert not state_of(wf)["nodes"][0].get("knowledge_evidence")


@pytest.mark.parametrize("episode", ["../outside", "a/b", "C:/outside", "", ".", ".."])
def test_task_paths_reject_traversal(canvas, episode):
    wf, _, _, calls, _, _ = canvas
    with pytest.raises(ValueError):
        wf.canvas_run(episode)
    assert not calls



def test_resume_reuses_existing_attempt_artifacts(canvas):
    """A resumed agent session may continue with artifacts it already wrote."""
    wf, _, _, calls, adapter, success = canvas

    def interrupted(task, **kwargs):
        for relative in task["expected_artifacts"]:
            path = Path(kwargs["workspace"]) / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("already generated", encoding="utf-8")
        return {"status": "needs_input", "summary": "waiting", "question": "continue?",
                "artifacts": [], "checks": [True] * len(task["checklist"])}

    adapter.run_agent = interrupted
    assert wf.canvas_run("resume-reuse") == 6
    state = state_of(wf)
    assert state["nodes"][0]["status"] == "awaiting_input"

    def completed(task, **kwargs):
        if task["node_id"] != "A":
            for relative in task["expected_artifacts"]:
                path = Path(kwargs["workspace"]) / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("next", encoding="utf-8")
        return {"status": "completed", "summary": "reused", "artifacts": task["expected_artifacts"],
                "checks": [True] * len(task["checklist"])}

    adapter.run_agent = completed
    assert wf.canvas_reply(state["run_id"], "A", "continue") == 0
    final = state_of(wf)
    assert final["nodes"][0]["artifact_evidence"][0]["reused_from_previous_attempt"] is True


def test_compose_rejects_declared_input_missing_upstream(canvas, monkeypatch):
    wf, _, _, _, _, _ = canvas
    monkeypatch.setattr(wf, "load_modules", lambda: [
        {"id": "m1", "name": "缺输入模块", "executor": "agent", "requires": ["episodes/never-produced.md"],
         "artifacts": ["episodes/after.md"]}
    ])
    graph = {"nodes": [{"id": "needs_input", "module": "m1", "x": 10, "y": 10,
                        "name": "缺输入", "requires": ["episodes/never-produced.md"],
                        "artifacts": ["episodes/after.md"]}], "edges": []}
    with pytest.raises(ValueError, match="缺输入"):
        wf._validate_canvas(graph)


def test_unstructured_max_tokens_text_does_not_trigger_blind_model_replay(canvas):
    wf, _, _, calls, adapter, success = canvas
    attempts = {"A": 0}

    def flaky(task, **kwargs):
        if task["node_id"] == "A" and attempts["A"] == 0:
            attempts["A"] += 1
            calls.append((task, kwargs))
            return {"status": "awaiting_input",
                    "detail": "DeepSeek could not complete this step: max-tokens",
                    "question": "max-tokens"}
        return success(task, **kwargs)

    adapter.run_agent = flaky
    assert wf.canvas_run("auto-continue") == 6
    state = state_of(wf)
    assert state["status"] == "awaiting_input"
    assert attempts["A"] == 1
    events_file = Path(wf.RUNS) / wf.latest_run() / "events.jsonl"
    events = [json.loads(line) for line in events_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert not any(event.get("kind") == "auto_continue" for event in events)


def test_deprecated_and_other_module_raw_files_are_not_staged(canvas):
    from wf_knowledge import decide
    wf, kb, root, calls, _, _ = canvas
    shared = root / 'kb/knowledge/shared'
    shared.mkdir(parents=True)
    for name, status, module in [('retired.md', 'deprecated', None), ('scoped.md', 'confirmed', 'B')]:
        (shared / name).write_text('SHOULD_NOT_REACH_A', encoding='utf-8')
        item = kb.read_entry_content('shared/' + name)
        decide(root, item['path'], item['knowledge_revision'], status, name, module=module)
    assert wf.canvas_run('scope-filter') == 0
    first = Path(calls[0][1]['workspace'])
    assert not (first / 'knowledge/shared/retired.md').exists()
    assert not (first / 'knowledge/shared/scoped.md').exists()
    second = Path(calls[1][1]['workspace'])
    assert not (second / 'knowledge/shared/retired.md').exists()
    assert (second / 'knowledge/shared/scoped.md').exists()


@pytest.mark.parametrize('missing', [False, True])
def test_declared_project_knowledge_input_is_checked_before_model(canvas, missing):
    wf, kb, root, calls, _, _ = canvas
    graph = wf.read_json(wf.CANVAS_PATH, {})
    graph['nodes'][0]['requires'] = ['knowledge/files/' + ('absent.md' if missing else 'brief.md')]
    wf.write_json(wf.CANVAS_PATH, graph)
    result = wf.canvas_run('knowledge-input')
    assert result == (6 if missing else 0)
    if missing:
        assert calls == []
        assert '必读资料' in state_of(wf)['nodes'][0]['detail']
    else:
        assert len(calls) == 2
        assert any(row['reason'] == 'required' and row['path'].endswith('/brief.md') for row in state_of(wf)['nodes'][0]['knowledge_sources'])


@pytest.mark.parametrize('configured', [False, True])
def test_render_capability_is_explicit_without_a_third_party_skill(canvas, monkeypatch, configured):
    import wf_environment
    wf, _, root, calls, _, _ = canvas
    modules = wf.read_json(wf.MODULES_PATH, {})
    modules['modules'][0]['capabilities'] = ['video.remotion']
    wf.write_json(wf.MODULES_PATH, modules)
    monkeypatch.setattr(wf_environment, 'inspect', lambda base: {
        'video_ready':configured, 'paths':{'node':'configured-node.exe'},
        'available':{'python':True,'node':configured,'remotion_dependencies':configured}})
    code = wf.canvas_run('explicit-capability')
    assert code == (0 if configured else 6)
    if configured:
        assert not calls[0][0]['skills']
        assert calls[0][0]['execution_tools']['remotion_helper'].endswith('wf_video_tools.py')
        assert state_of(wf)['nodes'][0]['tool_config_sha256']
    else:
        assert calls == []
        assert '环境与视频工具' in state_of(wf)['nodes'][0]['detail']
