#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Capture a real UI screenshot for a workflow.

This is a deterministic capture step, not an agent step. It uses the locally
installed Chrome in headless mode and writes a PNG plus a manifest with source,
timestamp, dimensions and hash.
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import shutil
from datetime import datetime

DEFAULT_CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
DEFAULT_URL = os.environ.get("CANVAS_CAPTURE_URL") or "http://127.0.0.1:8792/"


def capture(url, out, width=1600, height=1000, chrome=None):
    chrome = chrome or os.environ.get("CANVAS_CHROME") or DEFAULT_CHROME
    if not os.path.isfile(chrome):
        raise SystemExit("找不到 Chrome：" + chrome)
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    if os.path.exists(out):
        os.remove(out)
    profile = tempfile.mkdtemp(prefix="canvas-capture-")
    args = [chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-sandbox",
            "--no-first-run", "--disable-extensions", "--disable-background-networking",
            "--user-data-dir=" + profile,
            "--window-size=%d,%d" % (width, height), "--virtual-time-budget=2500",
            "--screenshot=" + os.path.abspath(out), url]
    try:
        result = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=90)
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    if not os.path.isfile(out) or os.path.getsize(out) < 1000:
        raise SystemExit("截图失败：" + (result.stderr or result.stdout or "")[-500:])
    try:
        from PIL import Image
        image = Image.open(out)
        actual = [image.width, image.height]
    except Exception:
        actual = [width, height]
    with open(out, "rb") as f:
        digest = hashlib.sha256(f.read()).hexdigest()
    return {
        "source": url,
        "path": out.replace("\\", "/"),
        "width": actual[0],
        "height": actual[1],
        "bytes": os.path.getsize(out),
        "sha256": digest,
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "engine": "chrome-headless",
        "note": "真实运行界面的自动截图；不是生成的示意几何。",
    }


def _find_ffmpeg():
    candidates = [os.environ.get("CANVAS_FFMPEG"),
                  str(Path(os.environ.get('CANVAS_HOME') or Path(__file__).resolve().parent) / 'bin/ffmpeg.exe'),
                  "ffmpeg"]
    for candidate in candidates:
        if not candidate:
            continue
        if os.path.isfile(candidate) or candidate == "ffmpeg":
            return candidate
    return None


def capture_video(url, out, width=1600, height=1000, duration=3, fps=8, node_script=None, actions=""):
    """Record real UI frames with Puppeteer, then encode them to MP4.

    This is best-effort: if the local Node/Puppeteer profile is unavailable, the
    static screenshot is still a valid capture and the caller can continue.
    """
    node = os.environ.get("CANVAS_NODE") or "node"
    node_path = os.environ.get("CANVAS_NODE_PATH") or os.path.join(
        os.path.expanduser("~"), ".dsh", "profiles", "desktop", "node_modules")
    script = node_script or os.path.join(os.path.dirname(os.path.abspath(__file__)), "capture_ui.cjs")
    ffmpeg = _find_ffmpeg()
    if not os.path.isfile(script) or not ffmpeg:
        return {"ok": False, "error": "缺少 capture_ui.cjs 或 ffmpeg"}
    frames = tempfile.mkdtemp(prefix="canvas-capture-frames-")
    main = out + ".main.png"
    try:
        env = os.environ.copy()
        env["NODE_PATH"] = node_path
        env.setdefault("CANVAS_CHROME", os.environ.get("CANVAS_CHROME") or DEFAULT_CHROME)
        result = subprocess.run([node, script, "--url", url, "--frames-dir", frames,
                                 "--main", main, "--duration", str(duration), "--fps", str(fps),
                                 "--actions", actions],
                                capture_output=True, text=True, encoding="utf-8", errors="replace",
                                timeout=90, env=env)
        if result.returncode != 0:
            return {"ok": False, "error": (result.stderr or result.stdout)[-500:]}
        pattern = os.path.join(frames, "frame-%04d.jpg")
        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
        encode = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                                 "-framerate", str(fps), "-i", pattern,
                                 "-c:v", "libx264", "-pix_fmt", "yuv420p",
                                 "-movflags", "+faststart", os.path.abspath(out)],
                                capture_output=True, text=True, encoding="utf-8", errors="replace",
                                timeout=90)
        if encode.returncode != 0 or not os.path.isfile(out):
            return {"ok": False, "error": (encode.stderr or "ffmpeg failed")[-500:]}
        with open(out, "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()
        return {"ok": True, "path": out.replace("\\", "/"), "bytes": os.path.getsize(out),
                "sha256": digest, "duration_sec": duration, "fps": fps,
                "frames": len([name for name in os.listdir(frames) if name.startswith("frame-")]),
                "source": url, "engine": "puppeteer+ffmpeg"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[-500:]}
    finally:
        shutil.rmtree(frames, ignore_errors=True)
        if os.path.isfile(main):
            os.remove(main)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--width", type=int, default=1600)
    parser.add_argument("--height", type=int, default=1000)
    parser.add_argument("--chrome")
    parser.add_argument("--video", default="")
    parser.add_argument("--video-duration", type=float, default=3)
    parser.add_argument("--video-fps", type=int, default=8)
    parser.add_argument("--actions", default="")
    args = parser.parse_args()
    meta = capture(args.url, args.out, args.width, args.height, args.chrome)
    if args.video:
        video = capture_video(args.url, args.video, args.width, args.height,
                              args.video_duration, args.video_fps, actions=args.actions)
        meta["video"] = video
    os.makedirs(os.path.dirname(os.path.abspath(args.manifest)), exist_ok=True)
    with open(args.manifest, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(json.dumps(meta, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
