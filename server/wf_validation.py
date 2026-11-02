"""Independent hard acceptance checks for rendered, reviewed and delivered video."""
from fractions import Fraction
import hashlib
import json
import math
import ntpath
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess


def _absolute(path):
    absolute = os.path.abspath(os.fspath(path))
    if os.name == "nt" and not absolute.startswith("\\\\?\\"):
        absolute = "\\\\?\\UNC\\" + absolute[2:] if absolute.startswith("\\\\") else "\\\\?\\" + absolute
    return Path(absolute)


def _no_links(path):
    for current in reversed((path, *path.parents)):
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400):
            raise ValueError("Validation paths cannot contain links or reparse points")


def _path(workspace, relative, *, directory=False):
    if not isinstance(relative, str):
        raise ValueError("Validation requires a workspace-relative path")
    relative = relative.replace("\\", "/")
    parsed = PurePosixPath(relative)
    if (not relative or not parsed.parts or relative != parsed.as_posix() or parsed.is_absolute() or ntpath.splitdrive(relative)[0]
            or ":" in relative or any(part in {"..", "."} or part.endswith((".", " ")) for part in parsed.parts)):
        raise ValueError("Validation path escapes the workspace")
    root = _absolute(workspace)
    path = root.joinpath(*parsed.parts)
    _no_links(path)
    try:
        within = os.path.normcase(os.path.commonpath([root, path])) == os.path.normcase(str(root))
    except ValueError:
        within = False
    if not within:
        raise ValueError("Validation path escapes the workspace")
    if directory:
        if not path.is_dir():
            raise ValueError("Required delivery directory is missing: " + relative)
    elif not path.is_file() or path.stat().st_size == 0:
        raise ValueError("Required output file is missing or empty: " + relative)
    return path


def _json(path, content=None):
    try:
        value = json.loads(content.decode("utf-8") if content is not None else path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError("Validation JSON is unreadable or invalid") from exc
    if not isinstance(value, dict):
        raise ValueError("Validation JSON must be an object")
    return value


def _positive(value, label):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Invalid " + label) from exc
    if isinstance(value, bool) or not math.isfinite(number) or number <= 0:
        raise ValueError("Invalid " + label)
    return number


def _fps(value):
    try:
        return _positive(float(Fraction(str(value))), "frame rate")
    except (ValueError, ZeroDivisionError, OverflowError) as exc:
        raise ValueError("Invalid frame rate") from exc


def _sha(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _verified_upstream(task, relative, digest):
    entries = task.get("upstream_inputs") or []
    record = next((entry for entry in entries if isinstance(entry, dict) and entry.get("path") == relative), None)
    if not record or record.get("sha256") != digest:
        raise ValueError("Validation input is not an unchanged verified upstream artifact")


def _probe(media, tools):
    executable = tools.get("ffprobe") if isinstance(tools, dict) else None
    if not isinstance(executable, str) or not Path(executable).is_absolute() or not Path(executable).is_file():
        raise ValueError("The registered ffprobe dependency is missing")
    try:
        result = subprocess.run([executable, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(media)],
                                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("Registered ffprobe failed or timed out") from exc
    if result.returncode != 0:
        raise ValueError("ffprobe rejected the output media")
    try:
        data = json.loads(result.stdout)
        streams = data["streams"]
        if not isinstance(streams, list):
            raise ValueError("Invalid stream data")
        video = next((item for item in streams if isinstance(item, dict) and item.get("codec_type") == "video"), None)
        audio = next((item for item in streams if isinstance(item, dict) and item.get("codec_type") == "audio"), None)
        if not video or not audio:
            raise ValueError("Rendered video must contain video and audio streams")
        if str(video.get("codec_name")).lower() != "h264":
            raise ValueError("Rendered video must use H.264")
        duration = _positive((data.get("format") or {}).get("duration", video.get("duration")), "media duration")
        try:
            fps = _fps(video.get("avg_frame_rate"))
        except ValueError:
            fps = _fps(video.get("r_frame_rate"))
        return {"width": _positive(video.get("width"), "video width"), "height": _positive(video.get("height"), "video height"),
                "fps": fps, "duration_seconds": duration, "video_codec": "h264", "audio_codec": str(audio.get("codec_name") or "unknown")}
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise ValueError("ffprobe did not establish a valid H.264 video with audio: " + str(exc)) from exc


def validate_node(node, task, workspace, execution_tools):
    """Return measured JSON evidence, raising ValueError before any promotion."""
    config = node.get("validation") or {}
    if not isinstance(config, dict):
        raise ValueError("Node validation must be an object")
    kind = config.get("type")
    if not kind:
        return {}
    if kind == 'json':
        from wf_contracts import validate_json
        path = _path(workspace, config.get('path'))
        validate_json(_json(path), config.get('schema') or {})
        return {'type': kind, 'path': config['path'], 'sha256': _sha(path), 'schema_verified': True}
    if kind == "video_render":
        media = _path(workspace, config.get("media"))
        evidence = _probe(media, execution_tools)
        if config.get("spec"):
            spec_path = _path(workspace, config["spec"])
            content = spec_path.read_bytes()
            spec_sha = hashlib.sha256(content).hexdigest()
            _verified_upstream(task, config["spec"], spec_sha)
            # Parse precisely the bytes whose hash matched, not a second read.
            expected = _json(spec_path, content)
            for dimension in ("width", "height"):
                if evidence[dimension] != _positive(expected.get(dimension), "expected " + dimension):
                    raise ValueError("Rendered " + dimension + " does not match the edit specification")
            expected_fps = _fps(expected.get("fps"))
            if abs(evidence["fps"] - expected_fps) > 0.01:
                raise ValueError("Rendered frame rate does not match the edit specification")
            expected_duration = _positive(expected.get("duration_seconds"), "expected duration")
            tolerance = max(.25, 2 / expected_fps)
            if abs(evidence["duration_seconds"] - expected_duration) > tolerance:
                raise ValueError("Rendered duration does not match the edit specification")
            evidence["expected"] = expected
            evidence["spec_sha256"] = spec_sha
        return dict(evidence, type=kind, media=config["media"], sha256=_sha(media))
    if kind == "video_qc":
        report_path = _path(workspace, config.get("report"))
        data = _json(report_path)
        technical = data.get("report") or {}
        content = data.get("content_review") or {}
        if not isinstance(technical, dict) or not isinstance(content, dict):
            raise ValueError("QC technical and content review objects are required")
        visual_review = None
        if content.get("status") != "PASS":
            attestation = workspace / "visual-review.json"
            if attestation.is_file():
                try:
                    review = _json(attestation)
                except Exception:
                    review = {}
                if (review.get("status") == "PASS" and review.get("reviewer")
                        and review.get("frames") and review.get("checked")):
                    visual_review = review
                    content = dict(content, status="PASS", external_review=review)
        if (data.get("status") != "completed" or technical.get("overall_status") not in ("PASS", "WARN")
                or content.get("status") != "PASS"):
            raise ValueError("QC has not passed both technical and content checks")
        unresolved = data.get("unresolved", technical.get("unresolved"))
        if unresolved != [] or technical.get("unresolved", []) != []:
            raise ValueError("QC must explicitly confirm there are no unresolved issues")
        evidence = {"type": kind, "report": config["report"], "sha256": _sha(report_path),
                    "overall_status": technical["overall_status"], "content_status": "PASS", "unresolved": []}
        if visual_review is not None:
            evidence["visual_review"] = visual_review
        return evidence
    if kind == "video_delivery":
        bundle_relative = config.get("bundle")
        _path(workspace, bundle_relative, directory=True)
        final_relative = bundle_relative.rstrip("/\\") + "/final.mp4"
        final_media = _path(workspace, final_relative)
        _path(workspace, bundle_relative.rstrip("/\\") + "/captions.srt")
        manifest = _json(_path(workspace, bundle_relative.rstrip("/\\") + "/manifest.json"))
        source_relative = config.get("source_media")
        source_media = _path(workspace, source_relative)
        source_sha = _sha(source_media)
        _verified_upstream(task, source_relative, source_sha)
        if _sha(final_media) != source_sha:
            raise ValueError("Delivery video differs from the verified render")
        return dict(_probe(final_media, execution_tools), type=kind, bundle=bundle_relative,
                    source_media=source_relative, source_sha256=source_sha, manifest=manifest)
    raise ValueError("Unknown hard validation type: " + str(kind))
