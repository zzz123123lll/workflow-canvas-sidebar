#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Deterministic technical QC using the qc_skill CLI.

No model call: qc_skill runs ffprobe/ffmpeg checks and emits a machine-readable
report. Content review remains an explicit attestation (NEEDS_REVIEW by
default), because the execution model has no image input.
"""
import argparse
import json
import os
import subprocess
import sys
from datetime import datetime


def base_dir():
    return os.environ.get("CANVAS_HOME") or os.path.dirname(os.path.abspath(__file__))


def qc_src():
    candidates = [
        os.environ.get("CANVAS_QC_SRC"),
        os.path.join(base_dir(), "skills", "vendor", "qc", "src"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor", "qc", "src"),
    ]
    for candidate in candidates:
        if candidate and os.path.isdir(os.path.join(candidate, "qc_skill")):
            return candidate
    raise SystemExit("找不到 qc_skill 源码目录；请设置 CANVAS_QC_SRC")


def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", required=True)
    ap.add_argument("--captions", required=True)
    ap.add_argument("--brief", default="")
    ap.add_argument("--out", required=True)
    ap.add_argument("--md", required=True)
    ap.add_argument("--frames", default="")
    args = ap.parse_args()
    base = base_dir()
    brief = read_json(args.brief, {}) if args.brief else {}
    width = int(brief.get("width") or 1920)
    height = int(brief.get("height") or 1080)
    fps = int(brief.get("fps") or 30)
    duration = float(brief.get("duration_seconds") or 93)
    video_rel = os.path.relpath(args.video, base).replace("\\", "/")
    captions_rel = os.path.relpath(args.captions, base).replace("\\", "/")
    request = {
        "operation": "check", "kind": "delivery",
        "input": video_rel, "subtitle": captions_rel, "cache_policy": "bypass",
        "rules": {"delivery": {
            "require_video": True, "require_audio": True, "require_subtitle": True,
            "expected_extension": "mp4", "expected_container": "mp4", "min_size_bytes": 1000000,
            "video": {"expected_width": width, "expected_height": height,
                      "expected_aspect_ratio": "16:9", "expected_frame_rate": fps,
                      "frame_rate_tolerance": 0.01, "disallow_vfr": True,
                      "max_duration_sec": duration + 5,
                      "max_single_black_sec": 0.5, "max_total_black_sec": 1.0},
            "audio": {"require_audio_stream": True},
            "subtitle": {"require_subtitle": True, "max_duration_delta_sec": 1.0,
                         "min_coverage_ratio": 0.8},
        }},
    }
    work = os.path.dirname(os.path.abspath(args.out))
    os.makedirs(work, exist_ok=True)
    request_path = os.path.join(work, "qc-request.json")
    with open(request_path, "w", encoding="utf-8") as f:
        json.dump(request, f, ensure_ascii=False, indent=2)
    env = os.environ.copy()
    env["PYTHONPATH"] = qc_src() + (os.pathsep + env.get("PYTHONPATH", "") if env.get("PYTHONPATH") else "")
    env["PYTHONIOENCODING"] = "utf-8"
    result = subprocess.run([sys.executable, "-B", "-m", "qc_skill.cli", "run", request_path,
                             "--json", "--workspace", base],
                            cwd=base, capture_output=True, text=True, encoding="utf-8", errors="replace",
                            timeout=300, env=env)
    if result.returncode != 0:
        raise SystemExit((result.stderr or result.stdout or "qc_skill failed")[-1200:])
    response = json.loads(result.stdout)
    report = response.get("report") or {}
    overall = report.get("overall_status")
    unresolved = []
    if overall not in ("PASS", "WARN"):
        unresolved.append("technical QC: " + str(overall))
    if report.get("findings"):
        for finding in report["findings"]:
            if finding.get("severity") in ("FAIL", "ERROR"):
                unresolved.append(finding.get("message") or finding.get("code"))
    # Content review is a separate human/vision attestation.
    attestation_path = os.path.join(base, "episodes", os.path.basename(args.video).replace(".mp4", "-visual-review.json"))
    attestation = read_json(attestation_path, {})
    if attestation.get("status") == "PASS":
        content = {"status": "PASS", "reviewer": attestation.get("reviewer") or "user",
                   "reviewed_at": attestation.get("reviewed_at"), "notes": attestation.get("notes", "")}
    else:
        content = {"status": "NEEDS_REVIEW", "reason": "执行模型无图像输入；需要用户/视觉复核抽帧",
                   "attestation_path": os.path.relpath(attestation_path, base).replace("\\", "/")}
        unresolved.append("content review: NEEDS_REVIEW")
    qc = {"status": "completed", "report": report, "content_review": content,
          "unresolved": unresolved, "operation": "deterministic-qc",
          "created_at": datetime.now().isoformat(timespec="seconds")}
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(qc, f, ensure_ascii=False, indent=2)
    md = "# QC（确定性技术检查）\n\n- 技术: " + str(overall) + "\n- 内容: " + content["status"] + "\n- 未解决: " + (", ".join(unresolved) or "无") + "\n- 视频: `" + video_rel + "`\n"
    with open(args.md, "w", encoding="utf-8") as f:
        f.write(md)
    if args.frames:
        os.makedirs(args.frames, exist_ok=True)
        subprocess.run([os.environ.get("CANVAS_FFMPEG") or os.path.join(base, "bin", "ffmpeg.EXE"),
                        "-hide_banner", "-loglevel", "error", "-y", "-i", args.video,
                        "-vf", "fps=1/8,scale=480:-1,tile=4x3", "-frames:v", "1",
                        os.path.join(args.frames, "contact.png")], capture_output=True, timeout=120)
    print(json.dumps({"ok": overall in ("PASS", "WARN"), "technical": overall,
                      "content": content["status"], "unresolved": unresolved,
                      "out": args.out}, ensure_ascii=False))
    return 0 if overall in ("PASS", "WARN") else 1


if __name__ == "__main__":
    sys.exit(main())
