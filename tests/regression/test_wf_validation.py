"""Hard video node acceptance uses mocked ffprobe, never generates media."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest

REAL_RUN = subprocess.run

spec = importlib.util.spec_from_file_location("isolated_wf_validation", (Path(__file__).resolve().parents[2] / "server") / "wf_validation.py")
validation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validation)


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    media = tmp_path / "episodes/video.mp4"
    media.parent.mkdir()
    media.write_bytes(b"test media: ffprobe is explicitly mocked")
    tool = tmp_path / "ffprobe.exe"
    tool.write_bytes(b"test placeholder")
    calls = []
    measured = {"streams": [{"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080, "avg_frame_rate": "30/1"},
                            {"codec_type": "audio", "codec_name": "aac"}], "format": {"duration": "2.0"}}

    def probe(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, json.dumps(measured), "")

    monkeypatch.setattr(validation.subprocess, "run", probe)
    return tmp_path, {"ffprobe": str(tool)}, measured, calls


def test_missing_validation_type_is_noop():
    assert validation.validate_node({}, {}, "unused", {}) == {}


def test_render_measures_and_matches_real_specs(fixture):
    root, tools, measured, calls = fixture
    (root / "spec.json").write_text(json.dumps({"width": 1920, "height": 1080, "fps": 30, "duration_seconds": 2.1}), encoding="utf8")
    task = {"upstream_inputs": [{"path": "spec.json", "sha256": hashlib.sha256((root / "spec.json").read_bytes()).hexdigest()}]}
    result = validation.validate_node({"validation": {"type": "video_render", "media": "episodes/video.mp4", "spec": "spec.json"}}, task, root, tools)
    assert result["duration_seconds"] == 2
    assert result["width"] == 1920 and result["height"] == 1080
    assert result["fps"] == 30 and result["video_codec"] == "h264"
    assert calls[0][0] == tools["ffprobe"]
    assert "-show_streams" in calls[0] and "-show_format" in calls[0]


@pytest.mark.parametrize("change", ["no_video", "no_audio", "bad_codec", "zero_duration", "bad_json", "probe_failure"])
def test_invalid_render_cannot_pass(fixture, monkeypatch, change):
    root, tools, measured, _ = fixture
    if change == "no_video": measured["streams"] = measured["streams"][1:]
    if change == "no_audio": measured["streams"] = measured["streams"][:1]
    if change == "bad_codec": measured["streams"][0]["codec_name"] = "png"
    if change == "zero_duration": measured["format"]["duration"] = "0"
    if change == "bad_json": monkeypatch.setattr(validation.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "not media", ""))
    if change == "probe_failure": monkeypatch.setattr(validation.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "Invalid data found"))
    with pytest.raises(ValueError):
        validation.validate_node({"validation": {"type": "video_render", "media": "episodes/video.mp4"}}, {}, root, tools)


@pytest.mark.parametrize("field,value", [("width", 1280), ("height", 720), ("fps", 60), ("duration_seconds", 6)])
def test_render_rejects_spec_mismatch(fixture, field, value):
    root, tools, _, _ = fixture
    expected = {"width": 1920, "height": 1080, "fps": 30, "duration_seconds": 2}
    expected[field] = value
    (root / "spec.json").write_text(json.dumps(expected), encoding="utf8")
    task = {"upstream_inputs": [{"path": "spec.json", "sha256": hashlib.sha256((root / "spec.json").read_bytes()).hexdigest()}]}
    with pytest.raises(ValueError):
        validation.validate_node({"validation": {"type": "video_render", "media": "episodes/video.mp4", "spec": "spec.json"}}, task, root, tools)


@pytest.mark.parametrize("condition", ["changed", "unverified"])
def test_render_cannot_self_validate_against_tampered_or_unverified_spec(fixture, condition):
    root, tools, _, _ = fixture
    spec_path = root / "spec.json"
    spec_path.write_text(json.dumps({"width": 1280, "height": 720, "fps": 60, "duration_seconds": 10}), encoding="utf8")
    task = {"upstream_inputs": [{"path": "spec.json", "sha256": hashlib.sha256(spec_path.read_bytes()).hexdigest()}]}
    # A dishonest renderer changes the spec to the dimensions it actually emitted.
    spec_path.write_text(json.dumps({"width": 1920, "height": 1080, "fps": 30, "duration_seconds": 2}), encoding="utf8")
    if condition == "unverified":
        task = {}
    with pytest.raises(ValueError, match="upstream"):
        validation.validate_node({"validation": {"type": "video_render", "media": "episodes/video.mp4", "spec": "spec.json"}}, task, root, tools)


def test_missing_probe_or_media_is_rejected(fixture):
    root, tools, _, _ = fixture
    for node, deps in [({"type": "video_render", "media": "missing.mp4"}, tools),
                       ({"type": "video_render", "media": "episodes/video.mp4"}, {})]:
        with pytest.raises(ValueError):
            validation.validate_node({"validation": node}, {}, root, deps)


@pytest.mark.parametrize("report", [
    {"status": "completed", "report": {"overall_status": "FAIL"}, "content_review": {"status": "PASS"}, "unresolved": []},
    {"status": "completed", "report": {"overall_status": "PASS"}, "content_review": {"status": "UNKNOWN"}, "unresolved": []},
    {"status": "completed", "report": {"overall_status": "WARN"}, "content_review": {"status": "PASS"}, "unresolved": ["unreviewed subtitle"]},
    {"status": "completed"},
])
def test_qc_does_not_trust_top_level_completed(fixture, report):
    root, tools, _, _ = fixture
    (root / "qc.json").write_text(json.dumps(report), encoding="utf8")
    with pytest.raises(ValueError):
        validation.validate_node({"validation": {"type": "video_qc", "report": "qc.json"}}, {}, root, tools)


def test_qc_accepts_explicit_technical_and_content_pass(fixture):
    root, tools, _, calls = fixture
    (root / "qc.json").write_text(json.dumps({"status": "completed", "report": {"overall_status": "WARN"}, "content_review": {"status": "PASS"}, "unresolved": []}), encoding="utf8")
    evidence = validation.validate_node({"validation": {"type": "video_qc", "report": "qc.json"}}, {}, root, tools)
    assert evidence["overall_status"] == "WARN"
    assert not calls


def make_bundle(root):
    bundle = root / "delivery"
    bundle.mkdir()
    (bundle / "final.mp4").write_bytes((root / "episodes/video.mp4").read_bytes())
    (bundle / "captions.srt").write_text("1\n00:00:00,000 --> 00:00:02,000\n字幕\n", encoding="utf8")
    (bundle / "manifest.json").write_text('{"files":["final.mp4","captions.srt"]}', encoding="utf8")
    task = {"upstream_inputs": [{"path": "episodes/video.mp4", "sha256": hashlib.sha256((root / "episodes/video.mp4").read_bytes()).hexdigest()}]}
    return bundle, task


def test_delivery_verifies_upstream_hash_and_media(fixture):
    root, tools, _, calls = fixture
    _, task = make_bundle(root)
    result = validation.validate_node({"validation": {"type": "video_delivery", "bundle": "delivery", "source_media": "episodes/video.mp4"}}, task, root, tools)
    assert result["source_sha256"] == task["upstream_inputs"][0]["sha256"]
    assert len(calls) == 1


@pytest.mark.parametrize("failure", ["missing_caption", "changed_final", "unverified_source", "changed_source"])
def test_delivery_cannot_ship_missing_or_changed_media(fixture, failure):
    root, tools, _, _ = fixture
    bundle, task = make_bundle(root)
    if failure == "missing_caption": (bundle / "captions.srt").unlink()
    if failure == "changed_final": (bundle / "final.mp4").write_bytes(b"different")
    if failure == "unverified_source": task = {}
    if failure == "changed_source": (root / "episodes/video.mp4").write_bytes(b"modified")
    with pytest.raises(ValueError):
        validation.validate_node({"validation": {"type": "video_delivery", "bundle": "delivery", "source_media": "episodes/video.mp4"}}, task, root, tools)


@pytest.mark.parametrize("relative", ["../outside.mp4", "C:/outside.mp4", "episodes/../../outside.mp4"])
def test_validation_refuses_out_of_workspace_paths(fixture, relative):
    root, tools, _, _ = fixture
    with pytest.raises(ValueError):
        validation.validate_node({"validation": {"type": "video_render", "media": relative}}, {}, root, tools)


def test_validation_rejects_junction_before_running_probe(fixture):
    root, tools, _, calls = fixture
    outside = root.parent / (root.name + "-external")
    outside.mkdir()
    (outside / "video.mp4").write_bytes(b"outside")
    link = root / "foreign"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        REAL_RUN(["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True, check=True)
    with pytest.raises(ValueError):
        validation.validate_node({"validation": {"type": "video_render", "media": "foreign/video.mp4"}}, {}, root, tools)
    assert not calls
