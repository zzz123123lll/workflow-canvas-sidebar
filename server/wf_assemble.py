#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Deterministic fast video assembly, screen-first edition.

Design rules:
- Text cards are solid brand backgrounds, never a complex UI behind text.
- Screenshots and operation clips are shown full-screen, zoomed into readable
  regions, with only the bottom subtitle layer.
- No lower-third marketing copy over screenshots: one text layer at a time.
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime

try:
    from PIL import Image, ImageDraw, ImageFont
except Exception:
    Image = None

FONT_BOLD = r"C:\Windows\Fonts\msyhbd.ttc"
FONT_REG = r"C:\Windows\Fonts\msyh.ttc"
W, H, FPS = 1920, 1080, 30


def ffmpeg():
    for candidate in (os.environ.get("CANVAS_FFMPEG"), str(Path(os.environ.get('CANVAS_HOME') or Path(__file__).resolve().parent) / 'bin/ffmpeg.exe'), "ffmpeg"):
        if candidate and (os.path.isfile(candidate) or candidate == "ffmpeg"):
            return candidate
    raise SystemExit("找不到 ffmpeg")


def run(args, timeout=600):
    result = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=timeout)
    if result.returncode != 0:
        raise SystemExit((result.stderr or result.stdout or "ffmpeg failed")[-1200:])
    return result


def fnt(size, bold=True):
    return ImageFont.truetype(FONT_BOLD if bold else FONT_REG, size)


def wrap(draw, text, font, max_width):
    lines, line = [], ""
    for ch in text:
        if draw.textlength(line + ch, font=font) <= max_width:
            line += ch
        else:
            if line:
                lines.append(line)
            line = ch
    if line:
        lines.append(line)
    return lines


def title_card(out, kicker, title, subtitle=""):
    if Image is None:
        raise SystemExit("缺少 Pillow")
    image = Image.new("RGB", (W, H), (7, 10, 16))
    draw = ImageDraw.Draw(image)
    for x in range(0, W, 80):
        draw.line([(x, 0), (x, H)], fill=(13, 18, 27), width=1)
    for y in range(0, H, 80):
        draw.line([(0, y), (W, y)], fill=(13, 18, 27), width=1)
    draw.rectangle([110, 190, 124, 620], fill=(240, 180, 41))
    y = 170
    if kicker:
        draw.text((170, y), kicker, font=fnt(30, False), fill=(240, 180, 41))
        y += 58
    for line in wrap(draw, title, fnt(82, True), 1500):
        draw.text((166, y), line, font=fnt(82, True), fill=(245, 248, 252))
        y += 104
    if subtitle:
        y += 22
        for line in wrap(draw, subtitle, fnt(36, False), 1500):
            draw.text((170, y), line, font=fnt(36, False), fill=(166, 178, 196))
            y += 50
    draw.text((170, H - 88), "AI日记创作画布 · 把创作流程变成任务画布", font=fnt(24, False),
              fill=(120, 132, 150))
    image.save(out, "PNG")


def showcase(out, screenshot):
    """Full-screen real UI, no text layer. Subtitles are burned later."""
    if Image is None:
        raise SystemExit("缺少 Pillow")
    base = Image.open(screenshot).convert("RGB")
    ratio = W / H
    sw, sh = base.size
    if sw / sh > ratio:
        nw = int(sh * ratio)
        base = base.crop(((sw - nw) // 2, 0, (sw + nw) // 2, sh))
    else:
        nh = int(sw / ratio)
        base = base.crop((0, (sh - nh) // 2, sw, (sh + nh) // 2))
    base.resize((W, H), Image.LANCZOS).save(out, "PNG")


def operation_segment(operation, out, duration, zoom=1.8):
    run([ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
         "-stream_loop", "-1", "-i", operation,
         "-filter_complex",
         "[0:v]scale=%d:%d:force_original_aspect_ratio=increase,"
         "crop=%d:%d,crop=iw/%.2f:ih/%.2f:(iw-iw/%.2f)/2:(ih-ih/%.2f)/2,"
         "scale=%d:%d,trim=duration=%s,setpts=PTS-STARTPTS[v]"
         % (W, H, W, H, zoom, zoom, zoom, zoom, W, H, duration),
         "-map", "[v]", "-t", str(duration), "-r", str(FPS),
         "-an", "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", out])


def duration_of(path):
    result = run([ffmpeg().replace("ffmpeg", "ffprobe"), "-v", "error",
                  "-show_entries", "format=duration", "-of", "json", path], timeout=60)
    return float(json.loads(result.stdout or "{}").get("format", {}).get("duration") or 0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--screenshot", required=True)
    ap.add_argument("--operation", default="")
    ap.add_argument("--narration", required=True)
    ap.add_argument("--captions", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--workdir", default="")
    ap.add_argument("--screen-first", action="store_true")
    args = ap.parse_args()
    for path in (args.screenshot, args.narration, args.captions):
        if not os.path.isfile(path):
            raise SystemExit("缺少输入：" + path)
    narration_duration = duration_of(args.narration) or 90.0
    work = args.workdir or tempfile.mkdtemp(prefix="canvas-assemble-")
    os.makedirs(work, exist_ok=True)
    segs = []
    try:
        beats = max(6, int(round(narration_duration / 8.5)))
        beat = narration_duration / beats
        plan = []
        if args.screen_first and args.operation and os.path.isfile(args.operation):
            for index in range(beats):
                if index == 0:
                    plan.append(("TITLE", "AI日记创作画布", "把创作流程变成任务画布", "真实界面操作演示"))
                elif index == beats - 1:
                    plan.append(("TITLE", "从任务卡到成片", "一张画布看完整流程", "真实截图 / 真实操作 / 可审可回退"))
                else:
                    plan.append(("OP", "真实操作", "", ""))
        else:
            for index in range(beats):
                if index == 0:
                    plan.append(("TITLE", "一条视频 = 一张任务画布", "AI日记创作画布", "把所有步骤放进同一张画布"))
                elif index == beats - 1:
                    plan.append(("TITLE", "把创作流程变成任务画布", "AI日记创作画布", "从一个人用，到一套可复用的工作流"))
                elif index % 3 == 1 and args.operation and os.path.isfile(args.operation):
                    plan.append(("OP", "真实操作", "", ""))
                elif index % 3 == 2:
                    plan.append(("SHOW", "真实界面", "", ""))
                else:
                    plan.append(("TITLE", "每一步都可执行、可验收", "任务卡 + 知识库", "少一些黑盒，多一些可追溯"))
        for index, (kind, title, kicker, subtitle) in enumerate(plan):
            seg = os.path.join(work, "seg%02d.mp4" % index)
            if kind == "OP":
                op_index = sum(1 for item in plan[:index] if item[0] == "OP")
                zooms = [1.45, 1.75, 2.05]
                operation_segment(args.operation, seg, round(beat, 3), zoom=zooms[op_index % len(zooms)])
            elif kind == "SHOW":
                card = os.path.join(work, "show%02d.png" % index)
                showcase(card, args.screenshot)
                run([ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
                     "-loop", "1", "-i", card, "-t", str(round(beat, 3)),
                     "-vf", "zoompan=z='min(zoom+0.0012,1.18)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=%d:s=%dx%d:fps=%d"
                     % (int(beat * FPS) + 1, W, H, FPS),
                     "-an", "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", seg])
            else:
                card = os.path.join(work, "card%02d.png" % index)
                title_card(card, kicker, title, subtitle)
                run([ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
                     "-loop", "1", "-i", card, "-t", str(round(beat, 3)),
                     "-vf", "zoompan=z='min(zoom+0.0008,1.06)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=%d:s=%dx%d:fps=%d"
                     % (int(beat * FPS) + 1, W, H, FPS),
                     "-an", "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", seg])
            segs.append(seg)
        concat = os.path.join(work, "concat.mp4")
        listfile = os.path.join(work, "list.txt")
        with open(listfile, "w", encoding="utf-8") as f:
            for seg in segs:
                f.write("file '" + seg.replace("\\", "/") + "'\n")
        run([ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-f", "concat",
             "-safe", "0", "-i", listfile, "-c", "copy", concat])
        captions_rel = os.path.relpath(args.captions, os.getcwd()).replace("\\", "/")
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        run([ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
             "-i", concat, "-i", args.narration,
             "-vf", "subtitles='" + captions_rel + "':force_style='FontName=Microsoft YaHei,FontSize=24,PrimaryColour=&H00FFFFFF,Outline=1,Shadow=0,MarginV=50'",
             "-c:v", "libx264", "-crf", "18", "-preset", "medium", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", args.out], timeout=600)
        with open(args.out, "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()
        meta = {
            "schema": "dsh.video.fast_assembly/v2",
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "inputs": {"screenshot": args.screenshot, "operation": args.operation,
                       "narration": args.narration, "captions": args.captions},
            "output": {"path": args.out.replace("\\", "/"), "bytes": os.path.getsize(args.out),
                       "sha256": digest},
            "beats": [{"kind": item[0], "title": item[1], "seconds": round(beat, 3)} for item in plan],
            "narration_seconds": narration_duration,
            "note": "屏幕优先：纯色文字卡 + 全屏真实界面 + 放大操作段；字幕只在底部。",
        }
        os.makedirs(os.path.dirname(os.path.abspath(args.manifest)), exist_ok=True)
        with open(args.manifest, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        print(json.dumps({"ok": True, "out": args.out, "bytes": os.path.getsize(args.out),
                          "sha256": digest, "beats": len(plan)}, ensure_ascii=False))
    finally:
        if not args.workdir:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
