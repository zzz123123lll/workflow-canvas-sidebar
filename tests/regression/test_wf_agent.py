"""Harness adapter contracts and actual isolated worker cancellation (no model calls)."""
import importlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import types

import pytest
import yaml

sys.path.insert(0, str((Path(__file__).resolve().parents[2] / "server")))
import wf_agent as adapter


def task(**overrides):
    return {"objective": "Write result.txt using only this task's material", "task_id": "task-A", "node_id": "A",
            "expected_artifacts": ["result.txt"], "checklist": ["Result exists"],
            "knowledge_context": "OWN_PARTITION_MARKER", "upstream_inputs": [], "user_answers": [], **overrides}


def good_report(**overrides):
    return {"status": "completed", "summary": "Created the requested result", "artifacts": ["result.txt"],
            "checks": [True], **overrides}


@pytest.mark.parametrize("report", [
    good_report(checks=[False]), good_report(checks=[]), good_report(artifacts=["../outside.txt"]),
    good_report(artifacts=[]), good_report(artifacts=["unrequested.txt"]),
    good_report(status="needs_input", question=""), good_report(unexpected="not allowed"),
])
def test_invalid_or_incomplete_reports_cannot_succeed(report):
    with pytest.raises(ValueError):
        adapter.validate_report(report, task())


def test_completed_report_and_needs_input_contract():
    assert adapter.validate_report(good_report(), task())["status"] == "completed"
    report = good_report(status="needs_input", question="Which topic should this step use?", checks=[False], artifacts=[])
    assert adapter.validate_report(report, task())["question"]


def test_prompt_carries_only_explicit_task_fields():
    prompt = adapter.build_prompt(task(untrusted_host_secret="NOT_A_TASK_FIELD"))
    assert "OWN_PARTITION_MARKER" in prompt
    assert "NOT_A_TASK_FIELD" not in prompt
    assert "only" in prompt.lower() and "needs_input" in prompt


def test_windows_sdk_patch_selects_builtin_non_pty_shell():
    patch = adapter.runtime_patch("nt")
    assert "persistent-pwsh\n  disabled: true" in patch
    assert "terminal-pwsh\n  disabled: true" in patch
    assert "@deepseek-ai/dsh-shell-env" in patch
    assert "@deepseek-ai/dsh-pwsh-sandbox" in patch
    assert "dsh-workflow-worker-guard" in patch
    assert "@deepseek-ai/dsh-tool-pwsh" in patch
    assert "dsh-pwsh-local" not in adapter.runtime_patch("posix")


def server_agent_module():
    """The deployed worker module lives under the plugin; the suite imports the
    development copy at the repo root. Load the plugin copy explicitly so the
    sandbox switch is asserted against the code that actually runs."""
    spec = importlib.util.spec_from_file_location(
        "server_wf_agent_patch_test",
        (Path(__file__).resolve().parents[2] / "server") / "wf_agent.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_windows_sdk_patch_can_switch_worker_to_sandbox(monkeypatch):
    module = server_agent_module()
    monkeypatch.setenv("WF_WORKER_SANDBOX", "1")
    patch = module.runtime_patch("nt")
    assert "@deepseek-ai/dsh-pwsh-sandbox" in patch
    assert "dsh-pwsh-local" not in patch
    assert "sandbox-policy" in patch and "workspace-write" in patch
    assert "@deepseek-ai/dsh-tool-pwsh" in patch
    monkeypatch.delenv("WF_WORKER_SANDBOX")
    default_patch = module.runtime_patch("nt")
    assert "dsh-workflow-worker-guard" in default_patch
    assert "workspace-write" in default_patch
    assert "@deepseek-ai/dsh-pwsh-local" not in default_patch


def test_windows_workspace_root_preserves_native_path_for_acl_sid(tmp_path):
    module = server_agent_module()
    workspace = tmp_path / "n" / "工作区"
    patch = yaml.safe_load(module.runtime_patch("nt", workspace=workspace))
    policy = next(row for row in patch if row.get("id") == "sandbox-policy")
    assert policy["config"]["workspaceRoot"] == str(workspace.resolve())


def test_runtime_boot_directory_keeps_windows_native_modules_on_system_drive(tmp_path, monkeypatch):
    monkeypatch.setenv("SYSTEMROOT", "C:\\Windows")
    monkeypatch.setattr(adapter.tempfile, "gettempdir", lambda: str(tmp_path))
    boot = adapter.sdk_runtime_cwd(Path("Z:/task-specific-home"))
    assert boot.is_dir()
    assert boot.is_relative_to(tmp_path)
    assert adapter.sdk_runtime_cwd(tmp_path) == tmp_path


def test_environment_key_precedes_saved_credential(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ephemeral-test-secret")
    monkeypatch.setattr(adapter, "_credential_read", lambda: "saved-test-secret")
    assert adapter.resolve_api_key() == "ephemeral-test-secret"
    monkeypatch.delenv("DEEPSEEK_API_KEY")
    assert adapter.resolve_api_key() == "saved-test-secret"


def test_missing_key_pauses_without_starting_worker(tmp_path, monkeypatch):
    monkeypatch.setattr(adapter, "resolve_api_key", lambda: None)
    result = adapter.run_agent(task(), workspace=tmp_path / "ws", runtime_home=tmp_path / "home", session_id="s1")
    assert result["status"] == "needs_input"
    assert result["question"]


def fake_worker(tmp_path, monkeypatch, source):
    script = tmp_path / "fake_worker.py"
    script.write_text(source, encoding="utf-8")
    monkeypatch.setattr(adapter, "_runtime_python", lambda: Path(sys.executable))
    monkeypatch.setattr(adapter, "_worker_command", lambda python: [str(python), str(script)])
    monkeypatch.setattr(adapter, "resolve_api_key", lambda: "fixture-api-secret-not-real")


def test_actual_worker_json_result_and_credential_not_saved(tmp_path, monkeypatch):
    fake_worker(tmp_path, monkeypatch,
                "import json,sys; payload=json.load(sys.stdin); print(json.dumps(" + repr(good_report()) + "))")
    result = adapter.run_agent(task(), workspace=tmp_path / "ws", runtime_home=tmp_path / "home", session_id="s1", timeout_seconds=5)
    assert result["status"] == "completed"
    assert result["session_id"] == "s1"
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert b"fixture-api-secret-not-real" not in path.read_bytes()


def test_actual_worker_wall_clock_timeout(tmp_path, monkeypatch):
    fake_worker(tmp_path, monkeypatch, "import json,sys,time; json.load(sys.stdin); time.sleep(30)")
    started = time.monotonic()
    result = adapter.run_agent(task(), workspace=tmp_path / "ws", runtime_home=tmp_path / "home", session_id="s1", timeout_seconds=0.3)
    assert result["status"] == "failed"
    assert "deadline" in result["summary"].lower()
    assert time.monotonic() - started < 8


def test_actual_running_worker_cancel_flag(tmp_path, monkeypatch):
    fake_worker(tmp_path, monkeypatch, "import json,sys,time; json.load(sys.stdin); time.sleep(30)")
    cancel = tmp_path / "cancel"
    timer = threading.Timer(0.3, lambda: cancel.write_text("1", encoding="utf-8"))
    timer.start()
    try:
        result = adapter.run_agent(task(), workspace=tmp_path / "ws", runtime_home=tmp_path / "home", session_id="s1",
                                   timeout_seconds=10, cancel_path=cancel)
        assert result["status"] == "cancelled"
    finally:
        timer.cancel()


def test_worker_errors_redact_credentials(tmp_path, monkeypatch):
    fake_worker(tmp_path, monkeypatch,
                "import os,sys,json; json.load(sys.stdin); sys.stderr.write(os.environ['DEEPSEEK_API_KEY']); sys.exit(1)")
    result = adapter.run_agent(task(), workspace=tmp_path / "ws", runtime_home=tmp_path / "home", session_id="s1", timeout_seconds=5)
    assert result["status"] == "failed"
    assert "fixture-api-secret-not-real" not in json.dumps(result)


def test_resume_failure_does_not_create_new_session(tmp_path, monkeypatch):
    calls = []

    class FakeHarness:
        def __init__(self, **kwargs):
            calls.append("init")
            env=kwargs['env']
            Path(env['WF_WORKER_GUARD_READY']).write_text(json.dumps({'nonce':env['WF_WORKER_GUARD_NONCE'],'guard':True,'mode':'workspace-write'}))
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def resume_session(self, session):
            calls.append("resume")
            raise RuntimeError("persisted session missing")
        def run(self, *args, **kwargs):
            calls.append("new-run")
            raise AssertionError("must not replace persisted session")

    monkeypatch.setitem(sys.modules, "deepseek_harness", types.SimpleNamespace(DeepSeekHarness=FakeHarness))
    payload = {"task": task(), "workspace": str(tmp_path), "runtime_home": str(tmp_path / "home"), "session_id": "s1", "resume": True}
    result = adapter.invoke_sdk(payload)
    assert result["status"] == "failed"
    assert calls == ["init", "resume"]


def test_ipc_error_stops_launched_worker(tmp_path, monkeypatch):
    stopped = []
    monkeypatch.setattr(adapter, "prepare_workspace_owner_access", lambda workspace: None)
    monkeypatch.setattr(adapter, "resolve_api_key", lambda: "fixture-api-secret-not-real")
    monkeypatch.setattr(adapter, "_runtime_python", lambda: Path(sys.executable))

    class BrokenIPC:
        pid = 12345
        stdout = None
        stderr = None
        stdin = None
        def communicate(self, **kwargs): raise OSError("simulated broken pipe")
        def poll(self): return None

    process = BrokenIPC()
    monkeypatch.setattr(adapter.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(adapter, "_stop_worker", lambda child: stopped.append(child) or True)
    result = adapter.run_agent(task(), workspace=tmp_path / "ws", runtime_home=tmp_path / "home", session_id="s1")
    assert result["status"] == "failed"
    assert stopped == [process]
    assert result["evidence"]["cleanup_confirmed"] is True


def test_deadline_covers_worker_that_never_reads_large_stdin(tmp_path, monkeypatch):
    # This contract times blocked worker IPC; native ACL setup is tested separately.
    monkeypatch.setattr(adapter, 'prepare_workspace_owner_access', lambda workspace: None)
    fake_worker(tmp_path, monkeypatch, "import time; time.sleep(2)")
    started = time.monotonic()
    result = adapter.run_agent(task(knowledge_context="x" * 200000), workspace=tmp_path / "ws", runtime_home=tmp_path / "home",
                               session_id="s1", timeout_seconds=0.2)
    assert result["status"] == "failed"
    assert "deadline" in result["summary"].lower()
    assert time.monotonic() - started < 1.5


def test_existing_project_interpreter_also_bounds_blocked_stdin(tmp_path):
    project_python = adapter.BASE / ".venv" / "Scripts" / "python.exe"
    if not project_python.is_file():
        pytest.skip("Existing Windows project interpreter is not installed")
    probe = '''
import sys,pathlib,time,json
sys.path.insert(0,sys.argv[2])
import wf_agent as a
root=pathlib.Path(sys.argv[1])
stub=root/'legacy-no-stdin.py'
stub.write_text('import time; time.sleep(2)',encoding='utf-8')
a._runtime_python=lambda:pathlib.Path(sys.executable)
a._worker_command=lambda p:[str(p),str(stub)]
a.resolve_api_key=lambda:'fixture-not-an-api-key'
task={'task_id':'t','node_id':'n','objective':'test','expected_artifacts':[],'checklist':[],'knowledge_context':'x'*1000000}
started=time.monotonic()
result=a.run_agent(task,workspace=root/'ws',runtime_home=root/'home',session_id='test',timeout_seconds=.2)
print(json.dumps({'status':result['status'],'elapsed':time.monotonic()-started,'summary':result['summary']}))
'''
    result = subprocess.run([str(project_python), "-c", probe, str(tmp_path), str(adapter.BASE)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    evidence = json.loads(result.stdout)
    assert evidence["status"] == "failed" and "deadline" in evidence["summary"].lower()
    assert evidence["elapsed"] < 1.5

