"""Rendering-tool scope/command tests. Never renders media or downloads tools."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess

import pytest

REAL_RUN = subprocess.run
REAL_POPEN = subprocess.Popen


SOURCE = (Path(__file__).resolve().parents[2] / "server")


@pytest.fixture
def tool(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("isolated_video_tools", SOURCE / "wf_video_tools.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    workspace = tmp_path / "workspace"
    project = workspace / "work-products/project"
    (project / "src").mkdir(parents=True)
    (project / "src/index.tsx").write_text("// test entry", encoding="utf-8")
    node = tmp_path / "node.exe"
    chrome = tmp_path / "chrome.exe"
    node.touch()
    chrome.touch()
    modules = tmp_path / "node_modules"
    modules.mkdir()
    monkeypatch.setattr(module, "NODE", node)
    monkeypatch.setattr(module, "CHROME", chrome)
    monkeypatch.setattr(module, "NODE_MODULES", modules)
    calls = []

    def subprocess_run(command, **kwargs):
        calls.append((command, kwargs))
        if "-e" in command:
            return subprocess.CompletedProcess(command, 0, json.dumps({"packages": {"@remotion/bundler": {"available": True, "version": "4.0.539"}, "@remotion/renderer": {"available": True, "version": "4.0.539"}, "remotion": {"available": True, "version": "4.0.539"}, "react": {"available": True, "version": "18.3.1"}}}), "")
        config = json.loads(command[-1])
        output = Path(config["output"])
        output.write_bytes(b"fixture-mp4-not-real-media")
        return subprocess.CompletedProcess(command, 0, json.dumps({"ok": True}), "")

    monkeypatch.setattr(module.subprocess, "run", subprocess_run)
    class FakePopen:
        def __init__(self, command, **kwargs):
            self.result = module.subprocess.run(command, **kwargs)
            self.returncode = self.result.returncode
            self.pid = 987654
        def communicate(self, timeout=None):
            return self.result.stdout, self.result.stderr
        def poll(self):
            return self.returncode
        def wait(self, timeout=None):
            return self.returncode
        def kill(self):
            self.returncode = -1
    monkeypatch.setattr(module.subprocess, "Popen", FakePopen)
    return module, workspace, project, calls


def invoke(module, workspace, **kwargs):
    return module.render(workspace=workspace, project="work-products/project", entry="src/index.tsx",
                         composition="Explain", output="episodes/final.mp4", **kwargs)


def test_render_uses_fixed_runtime_and_scoped_javascript(tool):
    module, workspace, project, calls = tool
    result = invoke(module, workspace)
    assert result["ok"] is True
    assert result["output"] == "episodes/final.mp4"
    command, options = calls[-1]
    assert command[0] == str(module.NODE)
    script = Path(command[1])
    assert script.is_relative_to(workspace / "scratch")
    text = script.read_text(encoding="utf-8")
    assert "selectComposition" in text and "renderMedia" in text
    assert "webpackOverride" in text and "enableCaching: false" in text
    assert "Browser download is disabled" in text
    config = json.loads(command[-1])
    assert config["entry"] == str(project / "src/index.tsx")
    assert config["browser"] == str(module.CHROME)
    assert config["nodeModules"] == str(module.NODE_MODULES)
    assert config["output"] == str(workspace / "episodes/final.mp4")
    assert Path(options["cwd"]).is_relative_to(workspace)
    assert Path(options["env"]["TEMP"]).is_relative_to(workspace)
    assert "DEEPSEEK_API_KEY" not in options["env"]
    assert "ELEVENLABS_API_KEY" not in options["env"]


@pytest.mark.parametrize("argument,value", [("project", "../other"), ("entry", "../../private.tsx"),
    ("output", "../other/final.mp4"), ("output", "C:/other/final.mp4"), ("scratch", "../scratch"),
    ("scratch", "outputs/temp"), ("project", "work-products./project"), ("entry", "src/index.tsx:ads")])
def test_rejects_scope_escapes_before_starting_process(tool, argument, value):
    module, workspace, _, calls = tool
    params = {"workspace": workspace, "project": "work-products/project", "entry": "src/index.tsx",
              "composition": "Explain", "output": "episodes/final.mp4"}
    params[argument] = value
    with pytest.raises(ValueError):
        module.render(**params)
    assert not calls


def test_missing_browser_is_reported_without_render_or_install(tool):
    module, workspace, _, calls = tool
    module.CHROME.unlink()
    with pytest.raises(ValueError, match="(?i)browser|chrome|浏览器"):
        invoke(module, workspace)
    assert not (workspace / "episodes/final.mp4").exists()


def test_doctor_only_resolves_packages_without_creating_workspace(tool, monkeypatch):
    module, workspace, _, calls = tool
    monkeypatch.setenv("DEEPSEEK_API_KEY", "TEST_SECRET_DO_NOT_LEAK")
    result = module.doctor()
    assert result["available"] is True
    assert "-e" in calls[-1][0]
    assert "TEST_SECRET_DO_NOT_LEAK" not in json.dumps(result)
    assert not (workspace / "scratch").exists()


def test_chrome_can_resolve_windows_profile_locations_without_inheriting_secrets(tool, monkeypatch):
    module,workspace,_,_=tool
    monkeypatch.setenv('USERPROFILE','C:/Users/renderer-test')
    monkeypatch.setenv('LOCALAPPDATA','C:/Users/renderer-test/AppData/Local')
    monkeypatch.setenv('DEEPSEEK_API_KEY','TEST_DO_NOT_INHERIT')
    environment=module._environment(workspace/'scratch')
    assert environment['USERPROFILE']=='C:/Users/renderer-test'
    assert environment['LOCALAPPDATA']=='C:/Users/renderer-test/AppData/Local'
    assert Path(environment['TEMP']).is_relative_to(workspace)
    assert 'DEEPSEEK_API_KEY' not in environment


def test_nonzero_runtime_exit_does_not_claim_success(tool, monkeypatch):
    module, workspace, _, calls = tool
    original = module.subprocess.run

    def fail(command, **kwargs):
        if "-e" in command:
            return original(command, **kwargs)
        return subprocess.CompletedProcess(command, 1, "", "render failed")

    monkeypatch.setattr(module.subprocess, "run", fail)
    with pytest.raises(ValueError, match="render failed"):
        invoke(module, workspace)


def test_no_output_means_failure_even_after_zero_exit(tool, monkeypatch):
    module, workspace, _, _ = tool
    original = module.subprocess.run

    def empty(command, **kwargs):
        return original(command, **kwargs) if "-e" in command else subprocess.CompletedProcess(command, 0, '{"ok":true}', "")

    monkeypatch.setattr(module.subprocess, "run", empty)
    with pytest.raises(ValueError, match="(?i)output|产物"):
        invoke(module, workspace)


def make_link(link, target):
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        with REAL_POPEN(["cmd", "/c", "mklink", "/J", str(link), str(target)], stdout=subprocess.PIPE, stderr=subprocess.PIPE) as child:
            child.communicate()
        if child.returncode:
            pytest.skip("junction unavailable")


def test_public_directory_junction_is_rejected(tool, tmp_path):
    module, workspace, project, calls = tool
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    make_link(project / "public", foreign)
    with pytest.raises(ValueError, match="(?i)link|junction|链接"):
        invoke(module, workspace)
    assert not calls


def test_output_directory_junction_is_rejected(tool, tmp_path):
    module, workspace, _, calls = tool
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    make_link(workspace / "episodes", foreign)
    with pytest.raises(ValueError, match="(?i)link|junction|链接"):
        invoke(module, workspace)
    assert not calls


@pytest.mark.skipif(os.name != "nt", reason="Windows process-tree cleanup contract")
def test_render_timeout_stops_entire_node_process_tree(tool, monkeypatch):
    module, workspace, _, calls = tool
    original_run = module.subprocess.run
    killed = []
    class TimedOut:
        pid = 987654
        returncode = None
        def __init__(self, command, **kwargs):
            self.command = command
        def communicate(self, timeout=None):
            raise subprocess.TimeoutExpired(self.command, timeout)
        def wait(self, timeout=None):
            self.returncode = 1
            return 1
        def poll(self):
            return self.returncode
        def kill(self):
            self.returncode = -1
    def fake_run(command, **kwargs):
        if command[0] == "taskkill":
            killed.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")
        return original_run(command, **kwargs)
    monkeypatch.setattr(module.subprocess, "Popen", TimedOut)
    monkeypatch.setattr(module.subprocess, "run", fake_run)
    with pytest.raises(ValueError, match="(?i)deadline|timeout|超时"):
        invoke(module, workspace, timeout=1)
    assert killed == [["taskkill", "/PID", "987654", "/T", "/F"]]
