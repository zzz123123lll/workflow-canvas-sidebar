# -*- coding: utf-8 -*-
"""Unit tests for the workflow engine's conditional branch (no network/renders)."""
import os
from pathlib import Path
import sys

BASE = str(Path(__file__).resolve().parents[2] / "server")
sys.path.insert(0, BASE)
import wf  # noqa: E402


def _cleanup(*names):
    for name in names:
        try:
            os.remove(os.path.join(wf.BASE, "episodes", name))
        except FileNotFoundError:
            pass


def test_feedback_low_completion_triggers_rewrite(tmp_path, monkeypatch):
    monkeypatch.setattr(wf, "BASE", str(tmp_path))
    stem = "_test_low"
    stats = {"episode": stem, "data": {"stat": {"view": 30, "like": 0, "reply": 0},
                                      "completion_percent": 21.0}}
    wf.write_json(os.path.join(wf.BASE, "episodes", f"{stem}-stats.json"), stats)
    node_fb = {"id": "feedback", "executor": "feedback",
               "artifacts": [f"episodes/{{stem}}-feedback.json"]}
    status, _ = wf.exec_feedback(node_fb, stem, "test", {})
    assert status == wf.STATUS_DONE
    fb = wf.read_json(os.path.join(wf.BASE, "episodes", f"{stem}-feedback.json"), {})
    assert fb["low_completion"] is True
    assert fb["suggestions"]
    node_rw = {"id": "rewrite", "executor": "rewrite",
               "condition": {"artifact": f"episodes/{{stem}}-feedback.json",
                             "path": "low_completion", "op": "==", "value": True}}
    ok, detail = wf.eval_condition(node_rw, stem, "test")
    assert ok, detail
    status, _ = wf.exec_rewrite(node_rw, stem, "test", {})
    assert status == wf.STATUS_DONE
    assert os.path.exists(os.path.join(wf.BASE, "episodes", f"{stem}-rewrite.md"))
    _cleanup(f"{stem}-stats.json", f"{stem}-feedback.json", f"{stem}-rewrite.md")


def test_feedback_high_completion_skips_rewrite(tmp_path, monkeypatch):
    monkeypatch.setattr(wf, "BASE", str(tmp_path))
    stem = "_test_high"
    stats = {"episode": stem, "data": {"stat": {"view": 500, "like": 8, "reply": 2},
                                      "completion_percent": 58.0}}
    wf.write_json(os.path.join(wf.BASE, "episodes", f"{stem}-stats.json"), stats)
    node_fb = {"id": "feedback", "executor": "feedback"}
    wf.exec_feedback(node_fb, stem, "test", {})
    fb = wf.read_json(os.path.join(wf.BASE, "episodes", f"{stem}-feedback.json"), {})
    assert fb["low_completion"] is False
    node_rw = {"id": "rewrite", "executor": "rewrite",
               "condition": {"artifact": f"episodes/{{stem}}-feedback.json",
                             "path": "low_completion", "op": "==", "value": True}}
    ok, _ = wf.eval_condition(node_rw, stem, "test")
    assert ok is False
    _cleanup(f"{stem}-stats.json", f"{stem}-feedback.json")
