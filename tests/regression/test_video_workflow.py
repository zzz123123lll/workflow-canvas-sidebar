"""Legacy graph wiring with original synthetic skill fixtures; no audio/video is generated."""
import json
from pathlib import Path

from test_canvas_automation import canvas, state_of, SOURCE


def test_entire_video_graph_loads_bound_skills_and_transfers_all_dependencies(canvas, monkeypatch):
    wf, _, root, calls, adapter, _ = canvas
    import wf_skills
    original_stage = wf_skills.stage_skills
    monkeypatch.setattr(wf_skills, "stage_skills", lambda ids, workspace, **kwargs: original_stage(ids, workspace, base=SOURCE.parent / "tests/fixtures/legacy-seed"))
    modules = json.loads((SOURCE.parent / "tests/fixtures/legacy-seed/kb/modules.json").read_text(encoding="utf-8"))
    # Deterministic capture is tested separately; this test covers agent modules.
    selected = [dict(m) for m in modules["modules"]
                if m["id"].startswith("video_") and m.get("executor") == "agent"]
    assert len(selected) == 11
    for module in selected:
        # Simulation fixtures are explicit JSON, never fake .mp4/.mp3 media files.
        module["artifacts"] = ["artifacts/" + module["id"] + "/project" if module["id"] in {"video_visuals", "video_edit"}
                               else "episodes/{stem}-" + module["id"] + ".json"]
        module["knowledge_aliases"] = {}
        module["validation"] = {}
        module["requires"] = [r for r in (module.get("requires") or []) if r != "artifacts/{run_id}/capture"]
    graph = json.loads((SOURCE.parent / "tests/fixtures/legacy-seed/kb/workflows/video.json").read_text(encoding="utf-8"))["graph"]
    graph["nodes"] = [n for n in graph["nodes"] if n.get("module") != "video_capture"]
    graph["edges"] = [e for e in graph["edges"] if e.get("from") != "video_capture" and e.get("to") != "video_capture"]
    wf.write_json(wf.MODULES_PATH, {"modules": selected})
    wf.write_json(wf.CANVAS_PATH, graph)
    def simulation(task, **kwargs):
        assert task["skills"] and all(s["instructions"] for s in task["skills"])
        assert all((Path(kwargs["workspace"]) / s["entry"]).is_file() for s in task["skills"])
        calls.append((task, kwargs))
        for relative in task["expected_artifacts"]:
            file = Path(kwargs["workspace"]) / relative
            if file.suffix == "":
                file.mkdir(parents=True)
                file = file / "simulation.json"
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(json.dumps({"simulation": True, "node": task["node_id"]}), encoding="utf-8")
        return {"status": "completed", "checks": [True] * len(task["checklist"]), "artifacts": task["expected_artifacts"]}
    adapter.run_agent = simulation
    assert wf.canvas_run("workflow-wiring-test") == 0
    state = state_of(wf)
    assert state["status"] == "completed"
    assert len(calls) == 11
    assert all(node["loaded_skills"] for node in state["nodes"])
    edit_task = next(task for task, _ in calls if task["node_id"] == "video_edit")
    assert {i["node_id"] for i in edit_task["upstream_inputs"]} == {"video_visuals", "video_voice", "video_captions", "video_storyboard"}
    delivery_task = next(task for task, _ in calls if task["node_id"] == "video_delivery")
    assert {i["node_id"] for i in delivery_task["upstream_inputs"]} == {"video_qc", "video_render", "video_edit", "video_captions", "video_script", "video_research"}
