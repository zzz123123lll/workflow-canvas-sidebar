"""Security regressions for the workflow canvas."""
import importlib.util
import json
import os
import time
from pathlib import Path

import pytest


SOURCE = (Path(__file__).resolve().parents[2] / "server")


def load_wf():
    spec = importlib.util.spec_from_file_location("security_wf", SOURCE / "wf.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_artifacts_are_confined_to_output_roots():
    wf = load_wf()
    for value in ("wf.py", "canvas_editor_server.py", "kb/modules.json", "../outside.md", ".runs/x"):
        with pytest.raises(ValueError):
            wf.artifact_relative(value, "episode", "run", "task")
    assert wf.artifact_relative("episodes/{stem}-brief.md", "day1", "run", "task") == "episodes/day1-brief.md"
    assert wf.artifact_relative("artifacts/run/file.json", "day1", "run", "task") == "artifacts/run/file.json"


def test_lock_recovers_dead_owner_and_rejects_live_owner(tmp_path, monkeypatch):
    wf = load_wf()
    monkeypatch.setattr(wf, "RUNS", str(tmp_path))
    monkeypatch.setattr(wf, "LOCKFILE", str(tmp_path / ".lock"))
    assert wf.acquire_lock("live") is True
    assert wf.acquire_lock("second") is False
    wf.release_lock()
    with open(wf.LOCKFILE, "w", encoding="utf-8") as handle:
        json.dump({"run_id": "dead", "pid": 999999, "created_at": time.time() - 100}, handle)
    os.utime(wf.LOCKFILE, (time.time() - 100, time.time() - 100))
    assert wf.acquire_lock("recovered") is True
    wf.release_lock()
