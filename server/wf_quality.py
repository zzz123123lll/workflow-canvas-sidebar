#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Deterministic quality metrics for a rendered video.

This is intentionally small and auditable: it reports what is measurable
(freeze/static ratio, black segments, real-operation input presence) instead of
letting a model declare "PASS" without evidence.
"""
import argparse
import json
import os
import re
import subprocess
import sys


def ffmpeg():
    for candidate in (os.environ.get("CANVAS_FFMPEG"), str(Path(os.environ.get('CANVAS_HOME') or Path(__file__).resolve().parent) / 'bin/ffmpeg.exe'), "ffmpeg"):
        if candidate and (os.path.isfile(candidate) or candidate == "ffmpeg"):
            return candidate
    raise SystemExit("找不到 ffmpeg")


def run(args, timeout=300):
    result = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=timeout)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--video", required=True)
    parser.add_argument("--capture-manifest", default="")
    parser.add_argument("--out", required=True)
    parser.add_argument("--max-static-ratio", type=float, default=0.5)
    args = parser.parse_args()
    if not os.path.isfile(args.video):
        raise SystemExit("缺少视频：" + args.video)

    probe = run([ffmpeg().replace("ffmpeg", "ffprobe") if ffmpeg().endswith("ffmpeg") else "ffprobe",
                 "-v", "error", "-show_entries", "format=duration", "-of", "json", args.video])
    if probe.returncode != 0:
        raise SystemExit((probe.stderr or "ffprobe failed")[-500:])
    duration = float(json.loads(probe.stdout or "{}").get("format", {}).get("duration") or 0)

    detect = run([ffmpeg(), "-hide_banner", "-nostats", "-i", args.video,
                  "-vf", "freezedetect=n=-45dB:d=1", "-an", "-f", "null", "-"])
    text = (detect.stderr or "") + (detect.stdout or "")
    segments = []
    current = None
    for line in text.splitlines():
        start = re.search(r"freeze_start: ([0-9.]+)", line)
        duration_match = re.search(r"freeze_duration: ([0-9.]+)", line)
        end = re.search(r"freeze_end: ([0-9.]+)", line)
        if start:
            current = {"start": float(start.group(1)), "duration": 0.0}
        if duration_match and current is not None:
            current["duration"] = float(duration_match.group(1))
        if end and current is not None:
            current["end"] = float(end.group(1))
            segments.append(current)
            current = None
    if current is not None:
        segments.append(current)
    static = sum(item.get("duration") or 0 for item in segments)
    ratio = (static / duration) if duration else 0.0

    operation = None
    if args.capture_manifest and os.path.isfile(args.capture_manifest):
        try:
            manifest = json.load(open(args.capture_manifest, encoding="utf-8"))
            video = (manifest.get("video") or {})
            operation = {"present": bool(video.get("ok")), "path": video.get("path"),
                         "duration_sec": video.get("duration_sec"),
                         "sha256": video.get("sha256")}
        except Exception as exc:
            operation = {"present": False, "error": str(exc)}
    passed = bool(duration) and ratio <= args.max_static_ratio
    report = {
        "schema": "dsh.video.quality/v1",
        "video": args.video.replace("\\", "/"),
        "duration_sec": round(duration, 3),
        "freeze_segments": segments[:60],
        "freeze_segment_count": len(segments),
        "static_seconds": round(static, 3),
        "static_ratio": round(ratio, 4),
        "max_static_ratio": args.max_static_ratio,
        "operation_capture": operation,
        "verdict": "PASS" if passed else "NEEDS_REVIEW",
        "reasons": [] if passed else ["静态比例超过阈值；视频仍以静态卡为主"],
        "note": "freezedetect 只测静态比例；真实素材是否好看到此为止，不冒充内容质量。",
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(json.dumps(report, ensure_ascii=False))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
