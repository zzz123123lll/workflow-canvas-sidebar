#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Workflow state machine for the six-step pipeline.

Each episode's progress is inferred from evidence (artifacts + gates), never from memory:
  status   — table of episodes and their current stage
  next     — what the next stage requires + exact commands
  check    — run the gates of one stage
  capture-stats — pull published video stats into episodes/<stem>-stats.json (复盘 gate)

Usage:
  python workflow.py status
  python workflow.py next day0
  python workflow.py check day0 --stage 发布
  python workflow.py capture-stats day0
  python workflow.py status --board episodes/WORKFLOW-BOARD.md
"""
import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
EPISODES = os.path.join(BASE, "episodes")
WORKFLOW = os.path.join(BASE, "kb", "workflow.json")
PUBLISH_LOG = os.path.join(BASE, "kb", "publish-log.jsonl")
PY = sys.executable
BILI = os.path.join(BASE, "bin", "bili.exe")


def load_stages():
    with open(WORKFLOW, encoding="utf-8") as f:
        return json.load(f)["stages"]


def episode_files():
    out = []
    for fn in sorted(os.listdir(EPISODES)):
        if not fn.endswith(".json"):
            continue
        stem = os.path.splitext(fn)[0]
        if not re.match(r"day\d+", stem):
            continue
        try:
            d = json.load(open(os.path.join(EPISODES, fn), encoding="utf-8"))
        except Exception:
            continue
        if isinstance(d, dict) and "scenes" in d:
            out.append(stem)
    return out


def plan_path(stem):
    p = os.path.join(EPISODES, stem + ".json")
    return p if os.path.exists(p) else None


def video_path(stem):
    p = os.path.join(EPISODES, stem + ".mp4")
    return p if os.path.exists(p) else None


def qa_path(stem):
    p = os.path.join(EPISODES, stem + "-qa.json")
    return p if os.path.exists(p) else None


def review_path(stem):
    p = os.path.join(EPISODES, stem + "-review.md")
    return p if os.path.exists(p) else None


def stats_path(stem):
    p = os.path.join(EPISODES, stem + "-stats.json")
    return p if os.path.exists(p) else None


def publish_records():
    recs = []
    if os.path.exists(PUBLISH_LOG):
        for line in open(PUBLISH_LOG, encoding="utf-8"):
            line = line.strip()
            if line:
                try:
                    recs.append(json.loads(line))
                except Exception:
                    pass
    return recs


def gate_plan_exists(stem):
    return plan_path(stem) is not None, plan_path(stem) or "缺少 plan 文件"


def spec_path(stem):
    p = os.path.join(EPISODES, stem + "-spec.json")
    return p if os.path.exists(p) else None


def gate_spec_exists(stem):
    p = spec_path(stem)
    if not p:
        return False, "缺少可执行约束 spec"
    try:
        d = json.load(open(p, encoding="utf-8"))
    except Exception as exc:
        return False, f"spec 无法解析：{exc}"
    need = ("type", "engine", "style_key", "structure", "constraints", "decisions", "blocks")
    missing = [k for k in need if not d.get(k)]
    if missing:
        return False, f"spec 缺字段：{missing}"
    if d.get("complete") is False:
        return False, f"spec 未完成：{d.get('problems')}"
    return True, f"{d.get('type')} / {d.get('engine')} / {d.get('duration_seconds')}s / {len(d['blocks'])} 区块"


def gate_video_exists(stem):
    p = video_path(stem)
    return p is not None, p or "缺少成片 mp4"


def gate_template_gate(stem):
    plan = plan_path(stem)
    if not plan:
        return False, "缺少 plan，无法校验"
    r = subprocess.run([PY, os.path.join(BASE, "render.py"), plan, "--check"],
                       cwd=BASE, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode == 0:
        try:
            ok = json.loads(r.stdout).get("ok", False)
            return bool(ok), "render --check ok" if ok else "render --check 校验未通过"
        except Exception:
            return True, "render --check 退出码 0"
    return False, (r.stdout or r.stderr)[:160]


def gate_qa_ok(stem):
    p = qa_path(stem)
    if not p:
        return False, "缺少 QA 报告"
    try:
        d = json.load(open(p, encoding="utf-8"))
    except Exception as exc:
        return False, f"QA 报告无法解析：{exc}"
    checks = d.get("checks", [])
    fails = [c["name"] for c in checks if c.get("status") == "fail"]
    if d.get("ok") and not fails:
        return True, f"{len(checks)} 项通过"
    return False, f"QA 未通过：{fails[:3] or d.get('summary')}"


def gate_review_ok(stem):
    p = review_path(stem)
    if not p:
        return False, "缺少审核包"
    text = open(p, encoding="utf-8").read()
    if "可以审核发布" in text and "⛔" not in text:
        return True, "审核包 ok"
    return False, "审核包存在阻塞项"


def gate_publish_entry(stem):
    for rec in publish_records():
        if rec.get("episode") == stem and rec.get("bvid"):
            return True, rec["bvid"]
    return False, "发布日志中没有该期 BV 记录"


def gate_stats_recorded(stem):
    p = stats_path(stem)
    if not p:
        return False, "缺少数据回填文件"
    try:
        d = json.load(open(p, encoding="utf-8"))
    except Exception as exc:
        return False, f"数据文件无法解析：{exc}"
    stat = (d.get("data") or {}).get("stat") or {}
    if stat.get("view") is not None and (d.get("data") or {}).get("completion_percent") is not None:
        return True, f"播放 {stat.get('view')} / 完播 {d['data'].get('completion_percent')}%"
    return False, "数据文件缺少播放/完播字段"


GATE_LABELS = {
    "spec_exists": "可执行约束 spec 已生成",
    "plan_exists": "plan / 脚本骨架存在",
    "video_exists": "成片 mp4 存在",
    "template_gate": "render --check 模板门禁通过",
    "qa_ok": "QA 报告 ok",
    "review_ok": "审核包无阻塞",
    "publish_entry": "发布日志有 BV 记录",
    "stats_recorded": "复盘数据已回填",
}


def cmd_gates(as_json=True):
    rows = [{"id": g, "label": GATE_LABELS.get(g, g)} for g in GATES]
    print(json.dumps(rows, ensure_ascii=False, indent=2))


GATES = {
    "spec_exists": gate_spec_exists,
    "plan_exists": gate_plan_exists,
    "video_exists": gate_video_exists,
    "template_gate": gate_template_gate,
    "qa_ok": gate_qa_ok,
    "review_ok": gate_review_ok,
    "publish_entry": gate_publish_entry,
    "stats_recorded": gate_stats_recorded,
}


def stage_status(stem, stages):
    """Return (reached_index, details) where reached_index = number of consecutive passed stages."""
    details = []
    reached = 0
    for i, st in enumerate(stages):
        results = []
        ok_all = True
        for req in st["requirements"]:
            fn = GATES.get(req["gate"])
            ok, detail = (False, "未知闸门") if not fn else fn(stem)
            results.append({"label": req["label"], "gate": req["gate"], "ok": bool(ok), "detail": str(detail)})
            ok_all = ok_all and bool(ok)
        details.append({"stage": st["id"], "passed": ok_all, "requirements": results})
        if ok_all:
            reached = i + 1
        else:
            break
    return reached, details


def cmd_status(stems, stages, board=None, as_json=False):
    rows = []
    for stem in stems:
        reached, details = stage_status(stem, stages)
        cur = stages[reached - 1]["id"] if reached else "未开始"
        nxt = stages[reached]["id"] if reached < len(stages) else "完成"
        rows.append((stem, cur, nxt, details))
    if board:
        lines = ["# 工作流状态板", "", f"更新：{datetime.now().isoformat(timespec='seconds')}", "",
                 "| 期 | 当前阶段 | 下一步 | 阻塞项 |", "|---|---|---|---|"]
        for stem, cur, nxt, details in rows:
            blocked = []
            for d in details:
                if not d["passed"]:
                    blocked = [r["label"] + "：" + r["detail"] for r in d["requirements"] if not r["ok"]]
                    break
            lines.append(f"| {stem} | {cur} | {nxt} | {'；'.join(blocked) or '—'} |")
        open(board, "w", encoding="utf-8").write("\n".join(lines) + "\n")
        print("BOARD", board)
    if as_json:
        out = []
        for stem, cur, nxt, details in rows:
            blocked = []
            for d in details:
                if not d["passed"]:
                    blocked = [r["label"] + "：" + r["detail"] for r in d["requirements"] if not r["ok"]]
                    break
            out.append({"episode": stem, "stage": cur, "next": nxt, "blockers": blocked})
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return rows
    print(f"{'期':<8}{'当前阶段':<10}{'下一步':<10}阻塞项")
    for stem, cur, nxt, details in rows:
        blocked = []
        for d in details:
            if not d["passed"]:
                blocked = [r["label"] + "：" + r["detail"] for r in d["requirements"] if not r["ok"]]
                break
        print(f"{stem:<8}{cur:<10}{nxt:<10}{'；'.join(blocked) or '—'}")
    return rows


def cmd_next(stem, stages):
    reached, details = stage_status(stem, stages)
    if reached >= len(stages):
        print(f"{stem}: 已完成全部 {len(stages)} 个阶段 ✅")
        return
    st = stages[reached]
    print(f"{stem}: 当前 {stages[reached-1]['id'] if reached else '未开始'} → 下一步「{st['id']}」（{st['owner']}）")
    print(f"说明：{st['description']}")
    print("需满足：")
    for req in st["requirements"]:
        fn = GATES.get(req["gate"])
        ok, detail = (False, "未知闸门") if not fn else fn(stem)
        print(f"  [{'✓' if ok else '✗'}] {req['label']} — {detail}")
    print("命令：")
    for c in st["commands"]:
        print("  " + c.format(stem=stem))


def cmd_check(stem, stages, stage_id):
    for i, st in enumerate(stages):
        if st["id"] != stage_id:
            continue
        print(f"{stem} · 阶段「{st['id']}」闸门检查")
        all_ok = True
        for req in st["requirements"]:
            fn = GATES.get(req["gate"])
            ok, detail = (False, "未知闸门") if not fn else fn(stem)
            all_ok = all_ok and bool(ok)
            print(f"  [{'✓' if ok else '✗'}] {req['label']} — {detail}")
        sys.exit(0 if all_ok else 2)
    print("未知阶段", stage_id)
    sys.exit(2)


def cmd_capture_stats(stem, stages):
    records = [r for r in publish_records() if r.get("episode") == stem and r.get("bvid")]
    if not records:
        print(json.dumps({"ok": False, "error": f"publish-log 中没有 {stem} 的 BV 记录"}, ensure_ascii=False))
        sys.exit(2)
    bvid = records[-1]["bvid"]
    bili = BILI if os.path.exists(BILI) else "bili"
    r = subprocess.run([bili, "upload", "stats", bvid], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        print(json.dumps({"ok": False, "error": "bili upload stats 失败", "detail": (r.stdout or r.stderr)[:300]},
                         ensure_ascii=False))
        sys.exit(2)
    try:
        data = json.loads(r.stdout)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"解析失败：{exc}"}, ensure_ascii=False))
        sys.exit(2)
    out = {"episode": stem, "bvid": bvid, "captured_at": datetime.now().isoformat(timespec="seconds"),
           "data": data.get("data")}
    p = os.path.join(EPISODES, stem + "-stats.json")
    json.dump(out, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(json.dumps({"ok": True, "bvid": bvid, "saved": p,
                      "view": data.get("data", {}).get("stat", {}).get("view")}, ensure_ascii=False))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("command", nargs="?", default="status",
                    choices=["status", "next", "check", "capture-stats"])
    ap.add_argument("episode", nargs="?", default=None)
    ap.add_argument("--stage", default=None)
    ap.add_argument("--board", default=None)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--gates", action="store_true")
    args = ap.parse_args()

    if args.gates:
        cmd_gates()
        return

    stages = load_stages()
    stems = episode_files()

    if args.command == "status":
        if args.episode:
            stems = [args.episode]
        cmd_status(stems, stages, args.board, args.json)
    elif args.command == "next":
        if not args.episode:
            ap.error("next 需要 <episode>")
        cmd_next(args.episode, stages)
    elif args.command == "check":
        if not args.episode or not args.stage:
            ap.error("check 需要 <episode> --stage <阶段>")
        cmd_check(args.episode, stages, args.stage)
    elif args.command == "capture-stats":
        if not args.episode:
            ap.error("capture-stats 需要 <episode>")
        cmd_capture_stats(args.episode, stages)


if __name__ == "__main__":
    main()
