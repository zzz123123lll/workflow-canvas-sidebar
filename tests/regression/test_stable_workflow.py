"""Stable, deterministic report workflow contract."""
import json
from pathlib import Path

import wf

SOURCE = (Path(__file__).resolve().parents[2] / "server")


def test_knowledge_digest_workflow_is_valid_and_stable(monkeypatch):
    monkeypatch.setattr(wf, "MODULES_PATH", str(SOURCE.parent / "tests/fixtures/legacy-seed/kb/modules.json"))
    data = json.loads((SOURCE.parent / "tests/fixtures/legacy-seed/kb/workflows/knowledge-digest.json").read_text(encoding="utf-8"))
    assert data["tier"] == "stable"
    graph = data["graph"]
    wf._validate_canvas(graph)
    assert len(graph["nodes"]) == 4
    modules = {m["id"]: m for m in json.loads((SOURCE.parent / "tests/fixtures/legacy-seed/kb/modules.json").read_text(encoding="utf-8"))["modules"]}
    for node in graph["nodes"]:
        module = modules[node["module"]]
        assert module["executor"] in {"report_brief", "report_digest", "report_qc", "report_deliver"}
        assert module["executor"] != "agent"
