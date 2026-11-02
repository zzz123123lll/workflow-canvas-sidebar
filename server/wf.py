#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""A small, complete workflow engine for the video pipeline.

This is the execution layer the canvas edits: definition → runs → artifacts →
gates → human approval → retries → event log → version snapshot.

Usage:
  python wf.py graph
  python wf.py run --episode day1
  python wf.py status [--run RUN_ID] [--json]
  python wf.py approve <RUN_ID>
  python wf.py reject <RUN_ID>
  python wf.py resume <RUN_ID>
  python wf.py logs [--run RUN_ID]
  python wf.py list
"""
import argparse
import ctypes
import hashlib
import json
import os
import ntpath
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import copy
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath
from wf_conversation import append_message, read_messages, stage_context

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE = os.environ.get("CANVAS_HOME") or os.path.dirname(os.path.abspath(__file__))
DEF_PATH = os.path.join(BASE, "kb", "workflow-def.json")
MODULES_PATH = os.path.join(BASE, "kb", "modules.json")
CANVAS_PATH = os.path.join(BASE, "kb", "canvas-workflow.json")
RUNS = os.path.join(BASE, ".runs")
LATEST = os.path.join(RUNS, "LATEST")
EPISODES = os.path.join(BASE, "episodes")
PY = sys.executable
BILI = os.path.join(BASE, 'bin', 'bili.exe')

STATUS_DONE = "passed"
STATUS_FAIL = "failed"
STATUS_SKIP = "skipped"
STATUS_WAIT = "awaiting_approval"
STATUS_BLOCK = "blocked"
STATUS_AGENT = "awaiting_agent"
STATUS_INPUT = "awaiting_input"
STATE_LOCK = threading.RLock()


def now():
    return datetime.now().isoformat(timespec="seconds")


def load_def():
    with open(DEF_PATH, encoding="utf-8") as f:
        return json.load(f)


def def_hash(d):
    raw = json.dumps(d, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:12]


def run_dir(run_id):
    return contained_path(RUNS, safe_component(run_id, "run_id"))


def safe_component(value, label="name"):
    value = str(value or "")
    if (not value.strip() or value in (".", "..") or value != value.strip()
            or value.endswith((".", " ")) or any(ch in value for ch in '/\\:\x00')
            or any(ord(ch) < 32 for ch in value)
            or re.match(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", value, re.I)):
        raise ValueError("Invalid " + label)
    return value


def contained_path(root, relative):
    root = os.path.realpath(root)
    result = os.path.realpath(os.path.join(root, relative))
    try:
        inside = os.path.commonpath([root, result]) == root
    except ValueError:
        inside = False
    if not inside:
        raise ValueError("Path escapes its declared directory")
    return result


ARTIFACT_ROOTS = ("episodes", "artifacts")


def artifact_relative(value, stem, run_id, task_id=None):
    value = str(tmpl(str(value), stem, run_id)).replace("\\", "/")
    path = PurePosixPath(value)
    if (not value or value != path.as_posix() or path.is_absolute() or ntpath.splitdrive(value)[0]
            or any(part.casefold() in ("..", ".runs", ".git") for part in path.parts)
            or ":" in value or "\x00" in value or value == "."):
        raise ValueError("Invalid artifact path: " + value)
    for part in path.parts:
        safe_component(part, "artifact path component")
    if path.parts[0].casefold() not in ARTIFACT_ROOTS:
        raise ValueError("产物只能写入 episodes/ 或 artifacts/ 目录：" + value)
    destination = contained_path(BASE, value)
    import kb
    knowledge_root = os.path.normcase(os.path.realpath(kb.KB))
    resolved = os.path.normcase(destination)
    try:
        in_knowledge = os.path.commonpath([knowledge_root, resolved]) == knowledge_root
    except ValueError:
        in_knowledge = False
    if in_knowledge:
        kb.safe_rel(os.path.relpath(destination, kb.KB), task=task_id)
    return value


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = str(path) + ".tmp-" + uuid.uuid4().hex
    with STATE_LOCK:
        try:
            with open(temporary, "w", encoding="utf-8") as f:
                json.dump(obj, f, ensure_ascii=False, indent=2)
            for attempt in range(6):
                try:
                    os.replace(temporary, path)
                    break
                except PermissionError as exc:
                    if getattr(exc, "winerror", None) not in (5, 32) or attempt == 5:
                        raise
                    time.sleep(0.025 * 2**attempt)
        finally:
            if os.path.exists(temporary):
                os.remove(temporary)


def latest_run():
    rid = None
    if os.path.exists(LATEST):
        try:
            rid = open(LATEST, encoding="utf-8").read().strip()
        except Exception:
            rid = None
    if rid and os.path.isdir(run_dir(rid)) and os.path.exists(os.path.join(run_dir(rid), "state.json")):
        return rid
    # fallback: newest real run directory (ignore _archive and non-run entries)
    try:
        candidates = [d for d in os.listdir(RUNS)
                      if os.path.isdir(os.path.join(RUNS, d)) and not d.startswith("_")]
        candidates.sort()
        return candidates[-1] if candidates else None
    except Exception:
        return None


def set_latest(run_id):
    os.makedirs(RUNS, exist_ok=True)
    with open(LATEST, "w", encoding="utf-8") as f:
        f.write(run_id)


def event(run_id, kind, **fields):
    rec = {"ts": now(), "kind": kind}
    rec.update(fields)
    path = os.path.join(run_dir(run_id), "events.jsonl")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with STATE_LOCK:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def tmpl(value, stem, run_id):
    return value.replace("{stem}", stem).replace("{run_id}", run_id)


def path_for(value, stem, run_id):
    p = tmpl(value, stem, run_id)
    return p if os.path.isabs(p) else os.path.join(BASE, p)


def exists(value, stem, run_id):
    return os.path.exists(path_for(value, stem, run_id))


def run_cmd(args, timeout):
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    r = subprocess.run(args, cwd=BASE, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout, env=env)
    return r.returncode, (r.stdout or ""), (r.stderr or "")


# ---------------------------------------------------------------------------
# executors: each returns ("passed"|"awaiting_approval"|"blocked"|"failed", detail)
# ---------------------------------------------------------------------------
def exec_spec(node, stem, run_id, state):
    spec = path_for("episodes/{stem}-spec.json", stem, run_id)
    if node.get("reuse", True) and os.path.exists(spec):
        return STATUS_DONE, f"复用已有 spec: {os.path.relpath(spec, BASE)}"
    brief = path_for("episodes/{stem}-brief.json", stem, run_id)
    if not os.path.exists(brief):
        return STATUS_FAIL, "缺少 brief：" + os.path.relpath(brief, BASE)
    topic = read_json(brief, {}).get("topic") or stem
    code, out, err = run_cmd([PY, "spec_compose.py", "--topic", topic, "--brief", brief,
                              "--out", spec, "--md", spec.replace(".json", ".md")], 120)
    if code != 0:
        return STATUS_FAIL, (err or out)[:300]
    return STATUS_DONE, out.strip()[:300]


def exec_plan(node, stem, run_id, state):
    plan = path_for("episodes/{stem}.json", stem, run_id)
    if node.get("reuse", True) and os.path.exists(plan):
        return STATUS_DONE, f"复用已有 plan: {os.path.relpath(plan, BASE)}"
    brief = path_for("episodes/{stem}-brief.json", stem, run_id)
    if not os.path.exists(brief):
        return STATUS_FAIL, "缺少 brief：" + os.path.relpath(brief, BASE)
    code, out, err = run_cmd([PY, "selector.py", "--brief", brief, "--out", plan], 180)
    if code != 0:
        return STATUS_FAIL, (err or out)[:300]
    return STATUS_DONE, "场景骨架已生成"


def exec_script(node, stem, run_id, state):
    plan = path_for("episodes/{stem}.json", stem, run_id)
    if not os.path.exists(plan):
        return STATUS_FAIL, "缺少 plan：" + os.path.relpath(plan, BASE)
    code, out, err = run_cmd([PY, "render.py", plan, "--check"], 180)
    if code != 0:
        return STATUS_FAIL, (err or out)[:300]
    return STATUS_DONE, "模板门禁通过"


def exec_produce(node, stem, run_id, state):
    mp4 = path_for("episodes/{stem}.mp4", stem, run_id)
    if node.get("reuse", True) and os.path.exists(mp4):
        return STATUS_DONE, f"复用已有成片 {os.path.getsize(mp4)//1024} KB"
    plan = path_for("episodes/{stem}.json", stem, run_id)
    code, out, err = run_cmd([PY, "render.py", plan, "--out", mp4, "--transition", "fade"], 900)
    if code != 0:
        return STATUS_FAIL, (err or out)[-300:]
    return STATUS_DONE, (out or "").strip()[-200:]


def exec_qa(node, stem, run_id, state):
    qa = path_for("episodes/{stem}-qa.json", stem, run_id)
    if node.get("reuse", True) and os.path.exists(qa):
        d = read_json(qa, {})
        if d.get("ok"):
            return STATUS_DONE, f"复用 QA 报告（{len(d.get('checks', []))} 项）"
    plan = path_for("episodes/{stem}.json", stem, run_id)
    code, out, err = run_cmd([PY, "qa.py", plan], 600)
    if code != 0:
        return STATUS_FAIL, (err or out)[:400]
    d = read_json(qa, {})
    return (STATUS_DONE if d.get("ok") else STATUS_FAIL), d.get("summary", "")


def exec_review(node, stem, run_id, state):
    md = path_for("episodes/{stem}-review.md", stem, run_id)
    if node.get("reuse", True) and os.path.exists(md):
        return STATUS_DONE, "复用已有审核包"
    plan = path_for("episodes/{stem}.json", stem, run_id)
    code, out, err = run_cmd([PY, "review.py", plan, "--review",
                              path_for("episodes/{stem}-review.json", stem, run_id)], 300)
    if code != 0:
        return STATUS_FAIL, (err or out)[:300]
    return STATUS_DONE, "审核包已生成"


def exec_approval(node, stem, run_id, state):
    record = read_json(os.path.join(run_dir(run_id), "approval.json"), {})
    if record.get("approved"):
        return STATUS_DONE, "使用者 已批准"
    return STATUS_WAIT, "等待 使用者 审核批准（python wf.py approve " + run_id + "）"


def publish_preflight(stem, run_id):
    """Safe publish rehearsal: verify every artifact/auth/param without uploading."""
    checks, problems = [], []
    review = path_for("episodes/{stem}-review.md", stem, run_id)
    mp4 = path_for("episodes/{stem}.mp4", stem, run_id)
    cover = path_for("episodes/{stem}-cover.png", stem, run_id)
    desc = path_for("episodes/{stem}-desc.txt", stem, run_id)
    for label, path in (("成片", mp4), ("审核包", review), ("封面", cover), ("简介", desc)):
        ok = os.path.exists(path)
        checks.append({"item": label, "ok": ok, "path": path})
        if not ok:
            problems.append("缺少" + label + "：" + os.path.relpath(path, BASE))
    auth = None
    try:
        r = subprocess.run([BILI, "auth", "status"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60)
        auth = json.loads(r.stdout or "{}")
        logged = bool((auth.get("data") or {}).get("logged_in"))
        checks.append({"item": "B站登录", "ok": logged,
                       "detail": (auth.get("data") or {}).get("user", {}).get("name", "")
                                 or (auth.get("error") or {}).get("message", "")})
        if not logged:
            problems.append("B站未登录：bili auth status 显示未登录")
    except Exception as exc:
        checks.append({"item": "B站登录", "ok": False, "detail": str(exc)[:120]})
        problems.append("无法检查 B 站登录：" + str(exc)[:120])
    cmd = (f'bili upload video episodes\\{stem}.mp4 --title "<从审核包选择>" '
           f'--tag 人工智能 --tag AI工具 --desc-file episodes\\{stem}-desc.txt '
           f'--cover episodes\\{stem}-cover.png --zone 1011 --tid 2098 --topic "B站AI无限竞技场" --ai-declare')
    cmd_path = os.path.join(run_dir(run_id), "publish-command.txt")
    os.makedirs(os.path.dirname(cmd_path), exist_ok=True)
    with open(cmd_path, "w", encoding="utf-8") as f:
        f.write(cmd + "\n")
    return checks, problems, cmd, cmd_path


def exec_publish(node, stem, run_id, state):
    approval = read_json(os.path.join(run_dir(run_id), "approval.json"), {})
    if not approval.get("approved"):
        return STATUS_BLOCK, "尚未人工批准；先 python wf.py approve " + run_id
    checks, problems, cmd, cmd_path = publish_preflight(stem, run_id)
    if problems:
        return STATUS_BLOCK, "发布前检查未通过：" + "；".join(problems[:4])
    log = os.path.join(BASE, "kb", "publish-log.jsonl")
    bvid = None
    try:
        with open(log, encoding="utf-8") as f:
            for line in f:
                rec = json.loads(line)
                if rec.get("episode") == stem and rec.get("bvid"):
                    bvid = rec["bvid"]
    except Exception:
        pass
    if not bvid:
        return STATUS_BLOCK, ("发布前检查通过；等待人工发布。命令已写入 " + os.path.relpath(cmd_path, BASE))
    return STATUS_DONE, "已发布：" + bvid


def exec_stats(node, stem, run_id, state):
    stats = path_for("episodes/{stem}-stats.json", stem, run_id)
    if node.get("reuse", True) and os.path.exists(stats):
        d = read_json(stats, {})
        view = ((d.get("data") or {}).get("stat") or {}).get("view")
        return STATUS_DONE, f"复用数据回填（播放 {view}）"
    code, out, err = run_cmd([PY, "workflow.py", "capture-stats", stem], 300)
    if code != 0:
        return STATUS_FAIL, (err or out)[:300]
    return STATUS_DONE, "数据已回填"


def eval_condition(node, stem, run_id):
    cond = node.get("condition")
    if not cond:
        return True, "无条件"
    art = path_for(cond.get("artifact", ""), stem, run_id)
    if not os.path.exists(art):
        return False, "条件产物不存在：" + os.path.relpath(art, BASE)
    data = read_json(art, {})
    cur = data
    for part in str(cond.get("path", "")).split("."):
        if not part:
            continue
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except Exception:
                cur = None
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            cur = None
    op = cond.get("op", "==")
    val = cond.get("value")
    ok = {"==": cur == val, "!=": cur != val, ">": (cur or 0) > val,
          ">=": (cur or 0) >= val, "<": (cur or 0) < val, "<=": (cur or 0) <= val}.get(op, False)
    return bool(ok), f"{cond.get('path')}={cur} {op} {val} → {ok}"


def exec_feedback(node, stem, run_id, state):
    stats = read_json(path_for("episodes/{stem}-stats.json", stem, run_id), {})
    data = stats.get("data") or {}
    stat = data.get("stat") or {}
    completion = data.get("completion_percent")
    view = stat.get("view")
    low = completion is not None and float(completion) < 35.0
    suggestions = []
    if low:
        suggestions.append(f"完播 {completion}% < 35%：下一条时长砍 10–15%，前 3 秒直接给结果")
    if view is not None and view < 100:
        suggestions.append(f"播放 {view} < 100：封面/标题换角度（冲突、数字、损失感）")
    if (stat.get("like", 0) + stat.get("reply", 0)) == 0:
        suggestions.append("互动为 0：结尾 CTA 换成可回答的具体问题")
    if not suggestions:
        suggestions.append("数据未触发改写条件，延续当前结构")
    out = {"episode": stem, "view": view, "completion_percent": completion,
           "low_completion": bool(low), "suggestions": suggestions, "generated_at": now()}
    write_json(path_for("episodes/{stem}-feedback.json", stem, run_id), out)
    return STATUS_DONE, ("低完播，触发改写支路" if low else "数据正常，不触发改写支路")


def exec_rewrite(node, stem, run_id, state):
    fb = read_json(path_for("episodes/{stem}-feedback.json", stem, run_id), {})
    lines = ["# 下一条改写建议（" + stem + "）", ""]
    lines += ["- " + x for x in fb.get("suggestions", [])]
    lines += ["", "> 本支路只在「低完播」条件成立时执行；否则节点跳过。"]
    path = path_for("episodes/{stem}-rewrite.md", stem, run_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return STATUS_DONE, "改写建议已生成"


def exec_metrics(node, stem, run_id, state):
    qa = read_json(path_for("episodes/{stem}-qa.json", stem, run_id), {})
    checks = qa.get("checks", [])
    fails = [c.get("name") for c in checks if c.get("status") == "fail"]
    warns = [c.get("name") for c in checks if c.get("status") == "warn"]
    out = {"episode": stem, "ok": qa.get("ok"), "checks": len(checks),
           "fails": fails, "warns": warns, "quality": qa.get("quality"),
           "generated_at": now()}
    write_json(path_for("episodes/{stem}-metrics.json", stem, run_id), out)
    return STATUS_DONE, f"QA 指标汇总：{len(checks)} 项，{len(fails)} fail / {len(warns)} warn"


def exec_kb_context(node, stem, run_id, state):
    code, out, err = run_cmd([PY, "kb.py", "context", stem], 90)
    if code != 0:
        return STATUS_FAIL, (err or out)[:300]
    return STATUS_DONE, (out or "").strip()[:200]


def exec_kb_archive(node, stem, run_id, state):
    code, out, err = run_cmd([PY, "kb.py", "archive", stem], 120)
    if code != 0:
        return STATUS_FAIL, (err or out)[:300]
    return STATUS_DONE, (out or "").strip()[:200]


def _canvas_save(state):
    with STATE_LOCK:
        saved = read_json(os.path.join(run_dir(state['run_id']), 'state.json'), {})
        state['state_revision'] = max(int(saved.get('state_revision', 0)), int(state.get('state_revision', 0))) + 1
        state["updated_at"] = now()
        write_json(os.path.join(run_dir(state["run_id"]), "state.json"), state)


def _record_progress(state, node, update):
    allowed = {"phase", "heartbeat_at", "worker_pid", "worker_alive", "event_count", "tool_calls",
               "last_event_at", "last_event_type", "tool_name", "worker_started_at"}
    with STATE_LOCK:
        previous = node.get("live") or {}
        live = dict(previous, **{key: value for key, value in update.items() if key in allowed})
        live["updated_at"] = time.time()
        node["live"] = live
        if (previous.get("phase"), previous.get("tool_name")) != (live.get("phase"), live.get("tool_name")):
            history = node.setdefault("activity", [])
            history.append({"at": time.time(), "phase": live.get("phase"), "tool_name": live.get("tool_name")})
            node["activity"] = history[-30:]
        _canvas_save(state)


def _execute_with_heartbeat(node, state):
    ns = node_state(state, node["id"])
    stop = threading.Event()
    def pulse():
        while not stop.wait(2):
            try:
                with STATE_LOCK:
                    ns["supervisor_heartbeat_at"] = time.time()
                    _canvas_save(state)
            except OSError:
                return
    with STATE_LOCK:
        ns["supervisor_heartbeat_at"] = time.time()
    thread = threading.Thread(target=pulse, daemon=True)
    thread.start()
    try:
        return execute_node(node, state, force=False)
    finally:
        stop.set()
        thread.join(timeout=2.5)


def _node_update(node, **fields):
    with STATE_LOCK:
        node.update(fields)


def _message(state, node, role, text, kind='message', key=None, source='workflow'):
    if not str(text or '').strip():
        return None
    return append_message(state['run_id'], node['id'], int(node.get('execution_generation',1)),
                          role, text, kind=kind, session_id=node.get('agent_session_id',''),
                          source=source, key=key, base=BASE)


def _freeze_evidence(state, ns):
    """Store an immutable, generation-scoped copy without overwriting earlier results."""
    generation = int(ns.get('execution_generation',1))
    prefix = 'artifacts/' + safe_component(state['run_id']) + '/nodes/' + safe_component(ns['id']) + '/generation-' + str(generation) + '/'
    result = []
    for original in ns.get('artifact_evidence') or []:
        row = dict(original)
        relative = str(row.get('path') or '').replace('\\','/')
        if relative.startswith(prefix):
            result.append(row); continue
        source = contained_path(BASE, relative)
        current = _file_evidence(source)
        if not current:
            row['historical_status'] = 'missing'; result.append(row); continue
        saved_relative = prefix + relative
        destination = contained_path(BASE, saved_relative)
        saved = _file_evidence(destination)
        if saved and saved['sha256'] != current['sha256']:
            raise ValueError('Refusing to overwrite immutable generation artifact: ' + saved_relative)
        if not saved:
            _copy_scoped_file(source, destination)
            saved = _file_evidence(destination)
        if not saved or saved['sha256'] != current['sha256']:
            raise ValueError('Generation artifact verification failed')
        changed = bool(row.get('sha256') and row['sha256'] != current['sha256'])
        row.update(saved)
        row.update(path=saved_relative, logical_path=relative, generation=generation)
        if changed:
            row['historical_status'] = 'changed'
            row['original_sha256'] = original.get('sha256')
        result.append(row)
    return result


def _filesystem_path(path):
    value = os.path.abspath(path)
    if os.name == "nt" and not value.startswith("\\\\?\\"):
        return "\\\\?\\UNC\\" + value[2:] if value.startswith("\\\\") else "\\\\?\\" + value
    return value


def _file_evidence(path):
    path = _filesystem_path(path)
    if os.path.isdir(path):
        from wf_artifacts import directory_evidence
        return directory_evidence(path)
    if not os.path.isfile(path) or os.path.islink(path):
        return None
    st = os.stat(path)
    with open(path, "rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    return {"sha256": digest, "bytes": st.st_size, "mtime_ns": st.st_mtime_ns,
            "ctime_ns": st.st_ctime_ns}


def _copy_scoped_file(source, destination):
    source, destination = _filesystem_path(source), _filesystem_path(destination)
    if os.path.isdir(source):
        from wf_artifacts import copy_directory
        return copy_directory(source, destination)
    if not os.path.isfile(source) or os.path.islink(source):
        raise ValueError("Input is missing or is a link")
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    shutil.copy2(source, destination)


def _run_module_snapshot(state):
    snapshot = read_json(os.path.join(run_dir(state["run_id"]), "modules.json"), {}) or {}
    return {row.get("id"): row for row in snapshot.get("modules", []) if isinstance(row, dict)}


def _declared_artifacts(node_id, by_id, modules):
    node = by_id.get(node_id) or {}
    module = modules.get(node.get("module")) or {}
    values = node.get("artifacts") if isinstance(node.get("artifacts"), list) else module.get("artifacts", [])
    return [str(value).replace("\\", "/") for value in (values or [])]


def _resolved_pattern(value, stem, run_id):
    return tmpl(str(value), stem, run_id).replace("\\", "/").strip()


def _knowledge_requirement(value, task_id):
    for prefix, destination in (('knowledge/shared/', 'shared/'), ('knowledge/files/', 'tasks/' + task_id + '/'),
                                ('shared/', 'shared/'), ('files/', 'tasks/' + task_id + '/files/')):
        if str(value).startswith(prefix):
            return destination + str(value)[len(prefix):]
    return None


def _required_patterns(node):
    values = node.get("requires") if isinstance(node.get("requires"), list) else []
    return [str(value) for value in values
            if str(value) and not str(value).startswith(("knowledge/", "shared/", "files/", "inputs/"))]


def _resolve_declared_inputs(node, state, by_id, modules, predecessors):
    """Find the nearest ancestor that declares each required artifact.

    Edges only express order. A required file is delivered by whichever upstream
    node declared it, even when that node is not the direct predecessor.
    """
    stem, run_id = state["episode"], state["run_id"]
    needed = {_resolved_pattern(value, stem, run_id): value for value in _required_patterns(node)}
    if not needed:
        return {}
    bindings = node.get('input_bindings') or {}
    if not isinstance(bindings, dict):
        raise ValueError('输入绑定须为路径到生产节点ID的映射')
    explicit = {}
    ancestors = _canvas_ancestors(_canvas_graph_for(run_id), node['id'])
    for raw_path, producer in bindings.items():
        path = _resolved_pattern(raw_path, stem, run_id)
        if path not in needed or producer not in ancestors:
            raise ValueError('输入绑定不属于必需输入或上游节点：' + str(raw_path))
        declared = next((p for p in _declared_artifacts(producer, by_id, modules)
                         if _resolved_pattern(p, stem, run_id) == path or
                         (_resolved_pattern(p, stem, run_id).endswith('/') and path.startswith(_resolved_pattern(p, stem, run_id)))), None)
        if declared is None:
            raise ValueError('绑定的上游未声明该输出：' + str(raw_path))
        explicit[path] = (0, producer, declared)
    frontier = list(predecessors.get(node["id"], []))
    seen, depth, found = set(), 0, {}
    while frontier and len(found) < len(needed):
        following = []
        for producer in frontier:
            if producer in seen:
                continue
            seen.add(producer)
            for declared in _declared_artifacts(producer, by_id, modules):
                concrete = _resolved_pattern(declared, stem, run_id)
                for pattern in needed:
                    matched = concrete == pattern or (concrete.endswith("/") and pattern.startswith(concrete))
                    if matched:
                        found.setdefault(pattern, []).append((depth, producer, declared))
            following.extend(predecessors.get(producer, []))
        frontier, depth = following, depth + 1
    resolved = dict(explicit)
    for pattern, rows in found.items():
        if pattern in explicit:
            continue
        rows.sort(key=lambda row: row[0])
        nearest = [row for row in rows if row[0] == rows[0][0]]
        if len(nearest) > 1:
            names = "、".join(sorted({str((by_id.get(row[1]) or {}).get("name") or row[1]) for row in nearest}))
            raise ValueError("输入「" + needed[pattern] + "」有多个上游同时声明产出（" + names
                             + "）；请只保留一个，或给节点单独改产物路径")
        resolved[pattern] = nearest[0]
    missing = [needed[pattern] for pattern in needed if pattern not in resolved]
    if missing:
        raise ValueError("节点「" + str(node.get("name") or node.get("id")) + "」缺输入：" + "、".join(missing)
                         + "；上游没有任何已完成的节点声明产出这些文件")
    return resolved


def _upstream_inputs(node, state, workspace):
    graph = _canvas_graph_for(state["run_id"])
    predecessors = _canvas_predecessors(graph)
    by_id = {row.get("id"): row for row in graph.get("nodes", [])}
    modules = _run_module_snapshot(state)
    stem, run_id = state["episode"], state["run_id"]
    inputs, staged = [], {}

    def stage(producer, evidence, direct):
        relative = artifact_relative(evidence["path"], stem, run_id, state.get("task_id"))
        logical = artifact_relative(evidence.get("logical_path") or evidence["path"],
                                    stem, run_id, state.get("task_id"))
        bound = node.get('input_bindings') or {}
        selected_producer = next((p for pattern, p in bound.items() if _resolved_pattern(pattern, stem, run_id) == logical), None)
        if selected_producer and selected_producer != producer:
            return
        if logical in staged:
            if staged[logical]['node_id'] != producer and staged[logical]['sha256'] != evidence['sha256']:
                raise ValueError('多个上游输出到相同路径，请设置输入绑定：' + logical)
            if direct:
                staged[logical]["direct"] = True
            return
        source = contained_path(BASE, relative)
        current = _file_evidence(source)
        if not current or current["sha256"] != evidence["sha256"]:
            raise ValueError("Upstream artifact changed after verification: " + relative)
        destination = contained_path(workspace, logical)
        _copy_scoped_file(source, destination)
        producer_state = node_state(state, producer) or {}
        row = {"node_id": producer, "path": logical, "artifact_path": relative,
               "generation": int(producer_state.get('execution_generation', 1)),
               "sha256": current["sha256"], "direct": direct, 'adopted_at': now()}
        if current.get("kind") != "directory" and current["bytes"] <= 65536:
            try:
                row["content"] = Path(source).read_text(encoding="utf-8")
            except UnicodeDecodeError:
                pass
        staged[logical] = row
        inputs.append(row)

    for predecessor in predecessors.get(node["id"], []):
        previous = node_state(state, predecessor)
        if not previous or previous["status"] not in (STATUS_DONE, STATUS_SKIP):
            raise ValueError("Upstream node is not complete")
        for artifact in previous.get("artifact_evidence", []):
            stage(predecessor, artifact, True)

    for pattern, (_, producer, _declared) in sorted(_resolve_declared_inputs(
            node, state, by_id, modules, predecessors).items()):
        previous = node_state(state, producer)
        if not previous or previous["status"] != STATUS_DONE:
            raise ValueError("声明产出的上游节点没有完成：" + producer)
        match = None
        for artifact in previous.get("artifact_evidence", []):
            logical = artifact_relative(artifact.get("logical_path") or artifact["path"],
                                        stem, run_id, state.get("task_id"))
            if logical == pattern or (logical.endswith("/") and pattern.startswith(logical)):
                match = artifact
                break
        if match is None:
            raise ValueError("上游节点「" + producer + "」没有留下「" + pattern + "」的产物证据")
        stage(producer, match, False)
    from wf_contracts import validate_inputs
    schemas = {artifact_relative(p, stem, run_id, state.get('task_id')): rule
               for p, rule in (node.get('input_schemas') or {}).items()}
    validate_inputs(dict(node, input_schemas=schemas), inputs, workspace)
    return inputs


def _kb_read(entry, stem):
    import kb
    try:
        if entry.get("scope") == "task":
            full = kb.safe_rel(entry["path"], task=stem)
        else:
            full = kb.safe_rel(entry["path"])
        with open(full, encoding="utf-8") as source:
            return source.read()
    except (OSError, ValueError, KeyError):
        return ""


def exec_report_brief(node, stem, run_id, state):
    brief = path_for("episodes/{stem}-report-brief.json", stem, run_id)
    topic = state.get("task_name") or stem
    definition = {"schema": "dsh.report-brief/v1", "topic": topic, "episode": stem,
                  "source_policy": "shared+task", "generated_at": now()}
    write_json(brief, definition)
    return STATUS_DONE, "资料卡主题已确定：" + topic


def exec_report_digest(node, stem, run_id, state):
    import kb
    digest = path_for("episodes/{stem}-digest.md", stem, run_id)
    citation_path = path_for("episodes/{stem}-digest.json", stem, run_id)
    try:
        shared = kb.list_entries(scope="shared")
        task_entries = kb.list_entries(task=stem)
    except (OSError, ValueError) as exc:
        return STATUS_FAIL, "读取知识库失败：" + str(exc)
    entries = []
    adopted = []
    for entry in shared + task_entries:
        text = _kb_read(entry, stem)
        if not text.strip():
            continue
        adopted.append({'path':entry.get('path'),'title':entry.get('title') or entry.get('path'),
                        'scope':entry.get('scope'),'source_type':entry.get('source_type'),
                        'content_sha256':hashlib.sha256(text.encode('utf-8')).hexdigest(),
                        'selected_chars':min(len(text),900),'source_chars':len(text),
                        'truncated':len(text)>900,'reason':'report_citation'})
        entries.append({"title": entry.get("title") or entry.get("path"),
                        "path": entry.get("path"), "scope": entry.get("scope"),
                        "task": entry.get("task"), "chars": len(text),
                        "excerpt": " ".join(text.split())[:900]})
    lines = ["# 资料卡：" + (state.get("task_name") or stem), "",
             "生成方式：共享知识库 + 当前任务分区，确定性整理，不调用大模型。", "",
             "## 资料范围", ""]
    if not entries:
        lines.append("当前知识库没有可用文本资料。请先在知识库中加入资料。")
    else:
        for scope_name, label in (("shared", "共享知识"), ("task", "任务资料")):
            group = [entry for entry in entries if entry.get("scope") == scope_name]
            lines.extend(["### " + label, ""])
            if not group:
                lines.append("（无）")
            for entry in group:
                lines.append("#### " + str(entry["title"]))
                lines.append("")
                lines.append(entry["excerpt"] + ("…" if entry["chars"] > 900 else ""))
                lines.append("")
    lines.extend(["## 引用清单", ""])
    for index, entry in enumerate(entries, 1):
        lines.append("%d. `%s`（%s，%d 字）" % (index, entry["path"], entry["scope"], entry["chars"]))
    if not entries:
        lines.append("（无可用资料）")
    lines.extend(["", "## 使用说明", "",
                  "这是一份可审阅的资料卡，不是最终结论；引用原文请打开对应知识库文件。", ""])
    content = "\n".join(lines)
    with open(digest, "w", encoding="utf-8") as target:
        target.write(content)
    write_json(citation_path, {"schema": "dsh.report-digest/v1", "entries": entries,
                               "generated_at": now(), "source_count": len(entries)})
    _node_update(node_state(state,node['id']),knowledge_scope=stem,knowledge_sources=adopted)
    return STATUS_DONE, "资料卡已生成：%d 条资料" % len(entries)


def exec_report_qc(node, stem, run_id, state):
    digest = path_for("episodes/{stem}-digest.md", stem, run_id)
    citations = path_for("episodes/{stem}-digest.json", stem, run_id)
    quality = path_for("episodes/{stem}-report-quality.json", stem, run_id)
    if not os.path.exists(digest) or not os.path.exists(citations):
        return STATUS_FAIL, "缺少资料卡或引用清单"
    data = read_json(citations, {})
    text = open(digest, encoding="utf-8").read()
    checks = {
        "has_title": text.startswith("# 资料卡："),
        "has_scope": "## 资料范围" in text,
        "has_citations": "## 引用清单" in text,
        "has_source": int(data.get("source_count") or 0) > 0,
        "no_placeholder": "TODO" not in text and "待补充" not in text,
        "min_chars": len(text) >= 200,
    }
    ok = all(checks.values())
    write_json(quality, {"schema": "dsh.report-quality/v1", "checks": checks, "ok": ok,
                         "source_count": data.get("source_count"), "chars": len(text),
                         "checked_at": now()})
    if not ok:
        failed = [key for key, value in checks.items() if not value]
        return STATUS_FAIL, "资料卡 QC 未通过：" + ", ".join(failed)
    return STATUS_DONE, "资料卡 QC PASS：%d 条引用，%d 字" % (data.get("source_count"), len(text))


def exec_report_deliver(node, stem, run_id, state):
    digest = path_for("episodes/{stem}-digest.md", stem, run_id)
    report = path_for("episodes/{stem}-report.md", stem, run_id)
    if not os.path.exists(digest):
        return STATUS_FAIL, "缺少资料卡"
    shutil.copyfile(digest, report)
    write_json(path_for("episodes/{stem}-report-manifest.json", stem, run_id),
               {"schema": "dsh.report-delivery/v1", "report": os.path.relpath(report, BASE),
                "bytes": os.path.getsize(report), "delivered_at": now()})
    return STATUS_DONE, "报告已交付：" + os.path.relpath(report, BASE)


def exec_agent(node, stem, run_id, state):
    """Run a real Harness turn in a per-node staging workspace, never from old outputs."""
    import kb
    import wf_agent

    task_id = state.get("task_id") or kb.ensure_task(stem)
    with STATE_LOCK:
        state["task_id"] = task_id
    node_id = safe_component(node["id"], "node_id")
    ns = node_state(state, node_id)
    _record_progress(state, ns, {"phase": "preparing", "worker_alive": False, "event_count": 0, "tool_calls": 0, "tool_name": ""})
    generation = int(ns.get("execution_generation", 1))
    node_root = contained_path(run_dir(run_id), "nodes/" + node_id + "/generation-" + str(generation))
    if node.get("skills"):
        node_root = contained_path(run_dir(run_id), "n/" + hashlib.sha256(node_id.encode()).hexdigest()[:8] + "/g" + str(generation))
    workspace = contained_path(node_root, "workspace")
    os.makedirs(workspace, exist_ok=True)
    import wf_skills
    registry_snapshot = os.path.join(run_dir(run_id), "skill-registry.json")
    skills = wf_skills.stage_skills(node.get("skills") or [], workspace, base=BASE,
                                   registry_path=registry_snapshot if os.path.isfile(registry_snapshot) else None)
    tools_snapshot = os.path.join(run_dir(run_id), "execution-tools.json")
    tools_source = os.path.join(BASE, 'kb', 'execution-tools.json') if node.get('capabilities') else tools_snapshot if os.path.isfile(tools_snapshot) else os.path.join(BASE, 'kb', 'execution-tools.json')
    execution_tools = read_json(tools_source, {}) if node.get('skills') or node.get('capabilities') else {}
    execution_tools = dict(execution_tools, python=execution_tools.get('python') or sys.executable,
                           python_usage='需要Python时使用此绝对路径，不假设系统PATH存在python。也可用宿主写文件工具保存成果。')
    if node.get('capabilities'):
        import wf_environment
        environment = wf_environment.inspect(BASE)
        if 'video.remotion' in node['capabilities'] and not environment['video_ready']:
            missing = [key for key, available in environment['available'].items() if key != 'python' and not available]
            return STATUS_INPUT, '请在项目总览的环境与视频工具中配置：' + '、'.join(missing) + '。保存后回复并继续，会采用该配置，保留已有成果。'
        execution_tools = dict(execution_tools, **environment['paths'], remotion_helper=str(Path(__file__).resolve().parent / 'wf_video_tools.py'))
        ns['tool_config_sha256'] = hashlib.sha256(json.dumps(execution_tools, sort_keys=True).encode()).hexdigest()
        event(run_id, 'tool_config_adopted', node=node_id, generation=generation, sha256=ns['tool_config_sha256'])
    _node_update(ns, loaded_skills=[{key: value for key, value in skill.items() if key not in {"instructions", "files_sha256"}} for skill in skills])
    artifacts = [artifact_relative(item, stem, run_id, task_id) for item in node.get("artifacts", [])]
    if len(artifacts) != len(set(artifacts)):
        raise ValueError("Duplicate artifact path")
    # Archive verified text outputs centrally; named aliases serve the existing brief/research/lessons modules.
    archive_targets = []
    for relative in artifacts:
        if Path(relative).suffix.lower() in {".md", ".txt", ".json", ".csv"}:
            archive_targets.append((relative, "tasks/" + task_id + "/outputs/" + relative))
    aliases = node.get("knowledge_aliases") or {}
    if not isinstance(aliases, dict):
        raise ValueError("knowledge_aliases must be a path mapping")
    for source, target in aliases.items():
        source = artifact_relative(source, stem, run_id, task_id)
        if source not in artifacts:
            raise ValueError("Knowledge alias must reference a declared artifact")
        archive_targets.append((source, "tasks/" + task_id + "/" + str(target)))
    for _, target in archive_targets:
        kb.safe_rel(target, task=task_id)
    checklist = list(node.get("checklist") or [])
    required_knowledge = list(node.get('required_knowledge') or [])
    required_knowledge.extend(reference for reference in (_knowledge_requirement(req, task_id) for req in node.get('requires', [])) if reference)
    context_path = kb.context_pack(task_id, query=str(node.get('agent_prompt') or node.get('name') or ''),
                                   module=str(node.get('module') or node.get('name') or node_id), required_paths=required_knowledge)
    knowledge_context = Path(context_path).read_text(encoding="utf-8")
    knowledge_path = contained_path(workspace, "knowledge/current-task.md")
    os.makedirs(os.path.dirname(knowledge_path), exist_ok=True)
    Path(knowledge_path).write_text(knowledge_context, encoding="utf-8")
    knowledge_manifest_path = str(context_path).removesuffix('.md') + '.sources.json'
    knowledge_manifest = read_json(knowledge_manifest_path,{})
    if knowledge_manifest:
        write_json(contained_path(node_root,'knowledge-sources.json'),knowledge_manifest)
        _node_update(ns, knowledge_sources=knowledge_manifest.get('selected') or [],
                     knowledge_omitted=knowledge_manifest.get('omitted') or [],
                     knowledge_duplicates=knowledge_manifest.get('duplicates') or [])
    # Shared knowledge is global reference material; the selected task partition
    # is still isolated from every other task partition.
    shared_root = contained_path(kb.KB, "shared")
    if os.path.isdir(shared_root):
        for directory, dirs, names in os.walk(shared_root, followlinks=False):
            for name in names:
                source = os.path.join(directory, name)
                relative = os.path.relpath(source, shared_root).replace("\\", "/")
                item = kb.read_entry_content('shared/' + relative)
                if item.get('knowledge_status') == 'deprecated' or (item.get('module_scope') and item['module_scope'] != str(node.get('module') or node.get('name') or node_id)):
                    Path(contained_path(workspace, 'knowledge/shared/' + relative)).unlink(missing_ok=True)
                    continue
                _copy_scoped_file(source, contained_path(workspace, "knowledge/shared/" + relative))
    partition = contained_path(kb.TASKS, task_id)
    if os.path.isdir(partition):
        for directory, dirs, names in os.walk(partition, followlinks=False):
            for name in list(dirs):
                candidate = Path(directory) / name
                if candidate.is_symlink() or getattr(candidate.lstat(), "st_reparse_tag", 0) == 0xA0000003:
                    raise ValueError("Links are not permitted in task knowledge")
            for name in names:
                source = os.path.join(directory, name)
                relative = os.path.relpath(source, partition).replace("\\", "/")
                item = kb.read_entry_content('tasks/' + task_id + '/' + relative, task=task_id)
                if item.get('knowledge_status') == 'deprecated' or (item.get('module_scope') and item['module_scope'] != str(node.get('module') or node.get('name') or node_id)):
                    Path(contained_path(workspace, 'knowledge/files/' + relative)).unlink(missing_ok=True)
                    continue
                _copy_scoped_file(source, contained_path(workspace, "knowledge/files/" + relative))
    upstream = _upstream_inputs(node, state, workspace)
    attachments = []
    for directory, dirs, names in os.walk(workspace, followlinks=False):
        for name in names:
            full = os.path.join(directory, name)
            rel = os.path.relpath(full, workspace).replace("\\", "/")
            if rel.startswith("knowledge/files/") or rel.startswith("knowledge/shared/"):
                try:
                    info = os.stat(full)
                except OSError:
                    continue
                attachments.append({"path": rel, "bytes": info.st_size, "kind": "workspace-file"})
    before = {relative: _file_evidence(contained_path(workspace, relative)) for relative in artifacts}
    # A resumed agent session may legitimately leave already-correct artifacts in
    # its workspace. Fresh runs must still produce a changed/new file; resumed
    # attempts may reuse the previous attempt's verified output.
    allow_reused_artifacts = bool(ns.get("agent_has_session")) or int(ns.get("attempts", 0)) > 0
    tasks_dir = contained_path(run_dir(run_id), "tasks")
    os.makedirs(tasks_dir, exist_ok=True)
    task = {
        "objective": str(node.get("agent_prompt") or "Complete this step using only this task's knowledge and upstream results.")
        + ("\nShared reference knowledge is staged under knowledge/shared/ and is included in knowledge_context. "
           "Task-specific files are under knowledge/files/. "
           "Shared references are authoritative: if the requested information exists there, use it directly and do not ask for it to be duplicated in the task partition. "
           "The workflow engine will archive your validated outputs into the current task knowledge partition after successful validation. "
           "Do not write knowledge storage directly. These archives will be created by the engine: "
           + json.dumps([target for _, target in archive_targets], ensure_ascii=False) if archive_targets else ""),
        "task_id": task_id, "node_id": node_id, "run_id": run_id,
        "request_id": hashlib.sha256((run_id + ":" + node_id + ":" + str(generation) + ":" + str(ns.get("attempts", 0)) + ":" + str(ns.get("agent_turns", 0))).encode()).hexdigest(),
        "expected_artifacts": artifacts,
        "checklist": checklist, "knowledge_context": knowledge_context,
        "upstream_inputs": upstream, "user_answers": list(ns.get("user_answers") or []),
        "skills": skills, "execution_tools": execution_tools,
        "skill_focus": node.get("skill_focus") or {}, "attachments": attachments,
    }
    if state.get('project_id'):
        task['objective'] = ('Project goal: ' + state.get('project_goal', '') + '\nDelivery requirements: '
                             + state.get('delivery_requirements', '') + '\nModule task: ' + task['objective'])
    context = stage_context(run_id, node_id, workspace, base=BASE)
    task['prior_messages'] = context['messages']
    task['conversation_history'] = {k: v for k, v in context.items() if k != 'messages'}
    Path(os.path.join(tasks_dir, node_id + ".md")).write_text(
        "# " + str(node.get("name") or node_id) + "\n\n" + task["objective"]
        + "\n\n## Current task knowledge\n" + knowledge_context
        + "\n\n## Upstream inputs\n" + json.dumps(upstream, ensure_ascii=False, indent=2)
        + "\n\n## Expected artifacts\n" + "\n".join(artifacts)
        + "\n\n## Available attachments\n"
        + ("\n".join("- " + a["path"] + " (" + str(a["bytes"]) + " bytes)" for a in attachments) or "（无）")
        + "\n\n## Acceptance checklist\n" + "\n".join(checklist) + "\n", encoding="utf-8")
    if skills:
        with open(os.path.join(tasks_dir, node_id + ".md"), "a", encoding="utf-8") as card:
            card.write("\n## Bound skills\n" + json.dumps(task["skills"], ensure_ascii=False, indent=2) + "\n")
    session_id = ns.get("agent_session_id") or "canvas-" + hashlib.sha256((run_id + ":" + node_id + ":" + str(generation)).encode()).hexdigest()[:28]
    _node_update(ns, agent_session_id=session_id, workspace=workspace,
                 knowledge_scope=task_id,
                 knowledge_context_sha256=hashlib.sha256(knowledge_context.encode("utf-8")).hexdigest())
    _canvas_save(state)
    _message(state, ns, 'user', task['objective'], kind='task', key='initial-task')
    try:
        node_timeout = int(node.get("timeout_seconds") or node.get('timeout_sec') or 180)
    except (TypeError, ValueError):
        node_timeout = 180
    result = wf_agent.run_agent(
        task, workspace=workspace, runtime_home=contained_path(node_root, "harness-home"),
        session_id=session_id, timeout_seconds=max(5, min(node_timeout, 7200)),
        cancel_path=os.path.join(run_dir(run_id), ".cancel"), resume=bool(ns.get("agent_has_session")),
        progress_path=os.path.join(node_root, "live.json"), on_progress=lambda update: _record_progress(state, ns, update),
    )
    if not isinstance(result, dict):
        raise ValueError("Harness adapter returned an invalid report")
    evidence = result.get("evidence") or {}
    host_session = str(result.get("session_id") or "").strip()
    if host_session and host_session != ns.get("agent_session_id"):
        _node_update(ns, agent_session_id=host_session)
    if evidence.get("backend") == "host-subagent":
        _node_update(ns, agent_has_session=True)
        if evidence.get("parent_session_id"):
            _node_update(ns, host_parent_session=evidence["parent_session_id"])
    _node_update(ns, agent_report=result, agent_turns=int(ns.get("agent_turns", 0)) + 1)
    turn = int(ns.get('agent_turns',1))
    for index, message in enumerate(result.get('public_messages') or []):
        _message(state, ns, 'assistant', message.get('text'), kind='message',
                 key='turn:' + str(turn) + ':public:' + str(index), source='sdk_public_assistant')
    _message(state, ns, 'assistant', result.get('summary'), kind='summary', key='turn:' + str(turn) + ':summary', source='agent_report')
    if result.get('question'):
        _message(state, ns, 'assistant', result['question'], kind='question', key='turn:' + str(turn) + ':question', source='agent_report')
    if evidence.get("finish_reason") is not None:
        _node_update(ns, agent_has_session=True)
    elif result.get("status") == "failed" and ns.get("agent_session_id"):
        # The worker was stopped by a deadline; its session is durable and the
        # next reply should continue it instead of starting from scratch.
        _node_update(ns, agent_has_session=True)
    if evidence.get("finish_reason") == "max-tokens" and result.get("status") != "completed":
        # A second max-tokens failure means this session is saturated; the next
        # reply must start a fresh compact session or it will truncate forever.
        fresh_session = "canvas-" + hashlib.sha256(
            (run_id + ":" + node["id"] + ":" + str(ns.get("execution_generation", 1)) + ":" + str(time.time())).encode()
        ).hexdigest()[:28]
        _node_update(ns, agent_has_session=False, agent_session_id=fresh_session)
    if evidence.get("cleanup_confirmed") is False:
        with STATE_LOCK:
            state["cleanup_pending"] = True
        _node_update(ns, question="执行进程清理尚未确认，请先检查并停止残余进程。")
        return STATUS_INPUT, ns["question"]
    if result.get("status") == "cancelled":
        return "cancelled", result.get("summary") or "Cancelled"
    if result.get("status") != "completed":
        _node_update(ns, question=str(result.get("question") or result.get("summary") or "这一步遇到问题，请补充说明后继续。"))
        return STATUS_INPUT, str(result.get("summary") or ns["question"])
    _record_progress(state, ns, {"phase": "validating", "worker_alive": False, "tool_name": ""})
    checks = result.get("checks")
    if not isinstance(checks, list) or len(checks) != len(checklist) or any(value is not True for value in checks):
        _node_update(ns, question="执行结果没有通过全部验收项，请说明如何修正。")
        return STATUS_INPUT, ns["question"]
    reported = result.get("artifacts") or []
    reported_paths = {item.get("path") if isinstance(item, dict) else item for item in reported}
    if set(artifacts) - reported_paths or reported_paths - set(artifacts):
        _node_update(ns, question="执行报告的产物列表与本步骤声明不一致，请检查后继续。")
        return STATUS_INPUT, ns["question"]
    observed = []
    for relative in artifacts:
        source = contained_path(workspace, relative)
        current = _file_evidence(source)
        reused = bool(current and current == before.get(relative))
        if not current or (reused and not allow_reused_artifacts):
            _node_update(ns, question="未发现本轮新生成的产物：" + relative + "。请说明如何继续。")
            return STATUS_INPUT, ns["question"]
        observed.append({"path": relative, **current, "produced_by": node_id,
                         "run_id": run_id, "reused_from_previous_attempt": reused})
    if os.path.exists(os.path.join(run_dir(run_id), ".cancel")):
        return "cancelled", "Cancelled before artifact promotion"
    validation = dict(node.get("validation") or {})
    if validation:
        from wf_validation import validate_node
        for key in ("media", "spec", "report", "bundle", "source_media", "path"):
            if validation.get(key):
                validation[key] = artifact_relative(validation[key], stem, run_id, task_id)
        validation_evidence = validate_node(dict(node, validation=validation), task, workspace, execution_tools)
    else:
        validation_evidence = {}
    if os.path.exists(os.path.join(run_dir(run_id), ".cancel")):
        return "cancelled", "Cancelled during independent validation"
    # Validate all outputs before touching project files; only declared outputs are promoted.
    _record_progress(state, ns, {"phase": "archiving", "worker_alive": False})
    for artifact in observed:
        source = contained_path(workspace, artifact["path"])
        destination = contained_path(BASE, artifact["path"])
        _copy_scoped_file(source, destination)
    knowledge_evidence = []
    for relative, target in archive_targets:
        destination = kb.safe_rel(target, task=task_id)
        source = contained_path(workspace, relative)
        _copy_scoped_file(source, destination)
        saved = _file_evidence(destination)
        expected = next(item for item in observed if item["path"] == relative)
        if not saved or saved["sha256"] != expected["sha256"]:
            raise ValueError("Knowledge archive verification failed: " + target)
        knowledge_evidence.append({"path": target, **saved, "produced_by": node_id, "run_id": run_id})
    kb.list_entries(task=task_id)
    with STATE_LOCK:
        ns.update(artifact_evidence=observed, knowledge_evidence=knowledge_evidence, checks=checks, upstream_inputs=upstream, validation_evidence=validation_evidence)
        ns.pop("question", None)
    return STATUS_DONE, str(result.get("summary") or "DeepSeek completed this step")


def exec_capture(node, stem, run_id, state):
    """Deterministic UI capture; no model call, no prompt."""
    capture_cfg = node.get("capture") if isinstance(node.get("capture"), dict) else {}
    url = node.get("capture_url") or capture_cfg.get("url") or os.environ.get("CANVAS_CAPTURE_URL") or "http://127.0.0.1:8792/"
    width = int(node.get("capture_width") or capture_cfg.get("width") or 1600)
    height = int(node.get("capture_height") or capture_cfg.get("height") or 1000)
    png = path_for("artifacts/{run_id}/capture/canvas-main.png", stem, run_id)
    video = path_for("artifacts/{run_id}/capture/canvas-operation.mp4", stem, run_id)
    manifest = path_for("episodes/{stem}-capture.json", stem, run_id)
    if node.get("reuse", True) and os.path.exists(png) and os.path.exists(manifest) and os.path.exists(video):
        return STATUS_DONE, "复用真实界面截图 + 操作录屏：" + os.path.relpath(png, BASE)
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wf_capture.py")
    args = [PY, script, "--out", png, "--manifest", manifest, "--url", str(url),
            "--width", str(width), "--height", str(height),
            "--video", video, "--video-duration", "3", "--video-fps", "8"]
    actions = node.get("capture_actions") or (capture_cfg.get("actions") if isinstance(capture_cfg, dict) else "")
    if actions:
        args += ["--actions", str(actions)]
    code, out, err = run_cmd(args, 180)
    if code != 0 or not os.path.exists(png):
        return STATUS_FAIL, (err or out or "截图失败")[-400:]
    has_video = os.path.exists(video)
    return STATUS_DONE, "已采集真实界面截图" + (" + 3 秒操作录屏" if has_video else "（录屏失败，仅静态图）") + "：" + os.path.relpath(png, BASE)


def exec_assemble(node, stem, run_id, state):
    """Deterministic fast assembly from capture + narration + captions."""
    capture = path_for("artifacts/{run_id}/capture", stem, run_id)
    screenshot = os.path.join(capture, "canvas-main.png")
    operation = os.path.join(capture, "canvas-operation.mp4")
    narration = path_for("episodes/{stem}-narration.mp3", stem, run_id)
    captions = path_for("episodes/{stem}-captions.srt", stem, run_id)
    missing = [name for name, path in (("capture/canvas-main.png", screenshot),
                                       ("narration", narration), ("captions", captions)) if not os.path.exists(path)]
    if missing:
        return STATUS_FAIL, "快装缺少输入：" + "、".join(missing)
    out = path_for("episodes/{stem}-fast.mp4", stem, run_id)
    manifest = path_for("episodes/{stem}-fast.json", stem, run_id)
    if node.get("reuse", True) and os.path.exists(out) and os.path.exists(manifest):
        return STATUS_DONE, "复用快装成片：" + os.path.relpath(out, BASE)
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wf_assemble.py")
    args = [PY, script, "--screenshot", screenshot, "--operation", operation if os.path.exists(operation) else "",
            "--narration", narration, "--captions", captions, "--out", out, "--manifest", manifest]
    code, text, err = run_cmd(args, 600)
    if code != 0 or not os.path.exists(out):
        return STATUS_FAIL, (err or text or "快装失败")[-500:]
    size = os.path.getsize(out) // 1024
    return STATUS_DONE, "确定性快装完成：%d KB，真实截图 + %s" % (
        size, "操作录屏" if os.path.exists(operation) else "无录屏")


def exec_fast_qc(node, stem, run_id, state):
    """Deterministic quality gate: static ratio + real capture presence."""
    video = path_for("episodes/{stem}-fast.mp4", stem, run_id)
    capture_manifest = path_for("episodes/{stem}-capture.json", stem, run_id)
    if not os.path.exists(video):
        return STATUS_FAIL, "缺少快装成片：" + os.path.relpath(video, BASE)
    out = path_for("episodes/{stem}-quality.json", stem, run_id)
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wf_quality.py")
    args = [PY, script, "--video", video, "--capture-manifest", capture_manifest if os.path.exists(capture_manifest) else "",
            "--out", out, "--max-static-ratio", "0.5"]
    code, text, err = run_cmd(args, 300)
    if not os.path.exists(out):
        return STATUS_FAIL, (err or text or "质量检查失败")[-500:]
    report = read_json(out, {})
    verdict = report.get("verdict")
    ratio = report.get("static_ratio")
    if verdict != "PASS":
        return STATUS_FAIL, "质量门禁未通过：静态比例 %.1f%%（阈值 50%%），见 %s" % (
            (ratio or 0) * 100, os.path.relpath(out, BASE))
    return STATUS_DONE, "质量门禁 PASS：静态比例 %.1f%%，操作录屏=%s" % (
        (ratio or 0) * 100, (report.get("operation_capture") or {}).get("present"))


def exec_qc_auto(node, stem, run_id, state):
    """Deterministic technical QC; content review remains an explicit attestation."""
    video = path_for("episodes/{stem}.mp4", stem, run_id)
    captions = path_for("episodes/{stem}-captions.srt", stem, run_id)
    brief = path_for("episodes/{stem}-brief.json", stem, run_id)
    if not os.path.exists(video):
        return STATUS_FAIL, "缺少成片：" + os.path.relpath(video, BASE)
    if not os.path.exists(captions):
        return STATUS_FAIL, "缺少字幕：" + os.path.relpath(captions, BASE)
    out = path_for("episodes/{stem}-qc.json", stem, run_id)
    md = path_for("episodes/{stem}-qc.md", stem, run_id)
    frames = path_for("artifacts/{run_id}/qc-frames", stem, run_id)
    if node.get("reuse", True) and os.path.exists(out) and os.path.exists(md):
        return STATUS_DONE, "复用确定性 QC：" + os.path.relpath(out, BASE)
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wf_qc.py")
    args = [PY, script, "--video", video, "--captions", captions,
            "--brief", brief if os.path.exists(brief) else "", "--out", out, "--md", md, "--frames", frames]
    code, text, err = run_cmd(args, 300)
    report = read_json(out, {})
    technical = (report.get("report") or {}).get("overall_status")
    content = (report.get("content_review") or {}).get("status")
    if technical not in ("PASS", "WARN"):
        return STATUS_FAIL, "技术 QC 未通过：" + str(technical) + "，见 " + os.path.relpath(out, BASE)
    if content != "PASS":
        return STATUS_FAIL, "技术 QC %s；内容复核 %s（需用户/视觉签核），见 %s" % (
            technical, content, os.path.relpath(out, BASE))
    return STATUS_DONE, "确定性 QC PASS：技术 %s + 内容复核 PASS" % technical


EXECUTORS = {
    "spec": exec_spec,
    "plan": exec_plan,
    "script": exec_script,
    "produce": exec_produce,
    "qa": exec_qa,
    "review": exec_review,
    "approval": exec_approval,
    "publish": exec_publish,
    "stats": exec_stats,
    "metrics": exec_metrics,
    "kb_context": exec_kb_context,
    "kb_archive": exec_kb_archive,
    "agent": exec_agent,
    "capture": exec_capture,
    "assemble": exec_assemble,
    "quality": exec_fast_qc,
    "qc_auto": exec_qc_auto,
    "report_brief": exec_report_brief,
    "report_digest": exec_report_digest,
    "report_qc": exec_report_qc,
    "report_deliver": exec_report_deliver,
    "feedback": exec_feedback,
    "rewrite": exec_rewrite,
}


# ---------------------------------------------------------------------------
# run / status
# ---------------------------------------------------------------------------
def node_state(state, node_id):
    for n in state["nodes"]:
        if n["id"] == node_id:
            return n
    return None


def initial_checks(node, modules):
    """Checklist placeholders only exist where a human/agent must confirm them.

    Deterministic executors cannot answer free-text checklist items, so they
    start with no checks instead of a misleading list of ``False`` values.
    """
    module = modules.get(node.get("module"), {})
    checklist = node.get("checklist") if isinstance(node.get("checklist"), list) else module.get("checklist", [])
    return [False] * len(checklist) if module.get("executor") == "agent" else []


def _artifact_evidence(node, state):
    """Verify and describe every declared artifact of a non-agent node."""
    stem, run_id = state["episode"], state["run_id"]
    rows, missing = [], []
    for value in node.get("artifacts", []):
        relative = artifact_relative(value, stem, run_id, state.get("task_id"))
        current = _file_evidence(contained_path(BASE, relative))
        if not current:
            missing.append(relative)
            continue
        row = {"path": relative}
        row.update(current)
        row.update(produced_by=node["id"], run_id=run_id, reused_from_previous_attempt=False)
        rows.append(row)
    return rows, missing


def execute_node(node, state, force=False):
    run_id = state["run_id"]
    stem = state["episode"]
    ns = node_state(state, node["id"])
    if ns["status"] == STATUS_DONE and not force:
        return STATUS_DONE, "已完成（跳过）"
    for req in node.get("requires", []):
        if node.get('executor') == 'agent' and _knowledge_requirement(req, state.get('task_id') or stem):
            continue  # Checked against this project's KB before any model call.
        if not exists(req, stem, run_id):
            return STATUS_FAIL, "缺少前置产物：" + req.replace("{stem}", stem)
    executor = EXECUTORS.get(node.get("executor"))
    if not executor:
        return STATUS_FAIL, "未知 executor：" + str(node.get("executor"))
    attempts = int(node.get("retry", 0)) + 1
    last = None
    for attempt in range(1, attempts + 1):
        started = now()
        try:
            status, detail = executor(node, stem, run_id, state)
        except Exception as exc:
            status, detail = STATUS_FAIL, "异常：" + str(exc)[:300]
        last = (status, detail)
        event(run_id, "node_attempt", node=node["id"], attempt=attempt,
              status=status, detail=str(detail)[:500], started=started)
        if status in (STATUS_DONE, STATUS_WAIT, STATUS_BLOCK, STATUS_INPUT, "cancelled"):
            return status, detail
        if attempt < attempts:
            time.sleep(float(node.get("retry_delay_ms", 800)) / 1000.0)
    return last


LOCKFILE = os.path.join(RUNS, ".lock")
WATCH_LOCK = os.path.join(RUNS, ".watch.lock")


LOCK_MAX_AGE_SECONDS = 3600
LOCK_HEARTBEAT_SECONDS = 15


def _pid_alive(pid):
    if not pid:
        return False
    try:
        if os.name == "nt":
            kernel32 = ctypes.windll.kernel32
            process = kernel32.OpenProcess(0x1000, False, int(pid))
            if not process:
                return False
            kernel32.CloseHandle(process)
            return True
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def _read_lock():
    try:
        raw = open(LOCKFILE, encoding="utf-8").read().strip()
        data = json.loads(raw) if raw else {}
        if not isinstance(data, dict):
            return {}
        return data
    except Exception:
        return {}


def acquire_lock(run_id):
    os.makedirs(RUNS, exist_ok=True)
    existing = _read_lock()
    if os.path.exists(LOCKFILE):
        age = time.time() - os.path.getmtime(LOCKFILE)
        alive = _pid_alive(existing.get("pid"))
        cleanup = bool(existing.get("cleanup_pending"))
        if alive and not cleanup:
            return False
        if cleanup:
            print("锁记录了未确认的进程清理；请检查后手动删除 .runs/.lock")
            return False
        if age < 5 and not existing:
            return False
        try:
            os.remove(LOCKFILE)
        except OSError:
            return False
    try:
        fd = os.open(LOCKFILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"run_id": run_id, "pid": os.getpid(), "created_at": time.time(),
                       "heartbeat_at": time.time()}, handle, ensure_ascii=False)
        return True
    except FileExistsError:
        return False


def touch_lock():
    try:
        data = _read_lock()
        data["heartbeat_at"] = time.time()
        temporary = LOCKFILE + ".tmp-" + uuid.uuid4().hex
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False)
        os.replace(temporary, LOCKFILE)
    except Exception:
        pass


def release_lock():
    try:
        data = _read_lock()
        if data.get("pid") not in (None, os.getpid()):
            return
        if os.path.exists(LOCKFILE):
            os.remove(LOCKFILE)
    except Exception:
        pass


def load_modules():
    return read_json(MODULES_PATH, {}).get("modules", [])


def load_canvas():
    return read_json(CANVAS_PATH, {"nodes": [], "edges": []})


def save_canvas(graph):
    write_json(CANVAS_PATH, graph)


def topo_order(graph):
    nodes = graph.get("nodes", [])
    edges = graph.get("edges", [])
    ids = [n["id"] for n in nodes]
    index = {nid: i for i, nid in enumerate(ids)}
    indeg = {nid: 0 for nid in ids}
    adj = {nid: [] for nid in ids}
    for e in edges:
        a, b = e.get("from"), e.get("to")
        if a not in indeg or b not in indeg:
            raise SystemExit(f"连线引用了不存在的节点：{a} -> {b}")
        adj[a].append(b)
        indeg[b] += 1
    ready = sorted([nid for nid, d in indeg.items() if d == 0], key=lambda x: index[x])
    order = []
    while ready:
        cur = ready.pop(0)
        order.append(cur)
        for nxt in adj[cur]:
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                ready.append(nxt)
        ready.sort(key=lambda x: index[x])
    if len(order) != len(ids):
        raise SystemExit("画布存在环，无法生成顺序工作流")
    return order


def sync_definition():
    graph = load_canvas()
    modules = {m["id"]: m for m in load_modules()}
    order = topo_order(graph)
    by_id = {n["id"]: n for n in graph.get("nodes", [])}
    nodes = []
    for nid in order:
        node = by_id[nid]
        mod = modules.get(node.get("module"))
        if not mod:
            raise SystemExit(f"节点 {nid} 引用了未知模块：{node.get('module')}")
        entry = {
            "id": nid,
            "name": node.get("name") or mod.get("name", nid),
            "kind": mod.get("kind", "auto"),
            "executor": mod.get("executor"),
            "requires": node.get("requires") if isinstance(node.get("requires"), list) else mod.get("requires", []),
            "artifacts": node.get("artifacts") if isinstance(node.get("artifacts"), list) else mod.get("artifacts", []),
            "retry": mod.get("retry", 1),
            "timeout_sec": mod.get("timeout_sec", 300),
            "reuse": True,
            "description": mod.get("description", ""),
        }
        if node.get("on_error") in ("stop", "continue"):
            entry["on_error"] = node["on_error"]
        if node.get("agent_prompt"):
            entry["agent_prompt"] = node["agent_prompt"]
        if isinstance(node.get("checklist"), list):
            entry["checklist"] = node["checklist"]
        for key in ("gate", "condition", "parallel_group"):
            if key in mod:
                entry[key] = mod[key]
        nodes.append(entry)
    from datetime import datetime as _dt
    definition = {
        "version": 3,
        "name": "画布生成的工作流",
        "updated": _dt.now().strftime("%Y-%m-%d"),
        "episode_pattern": "day*",
        "defaults": {"retry": 1, "retry_delay_ms": 800, "on_error": "fail"},
        "source": "canvas",
        "nodes": nodes,
    }
    write_json(DEF_PATH, definition)
    return {"ok": True, "nodes": len(nodes), "order": order}


def canvas_manage(args):
    graph = load_canvas()
    modules = {m["id"]: m for m in load_modules()}
    cmd = args.canvas_cmd
    if cmd == "list":
        print(json.dumps({"ok": True, "graph": graph, "modules": [{"id": m["id"], "name": m["name"], "category": m.get("category", "")} for m in modules.values()]}, ensure_ascii=False, indent=2))
        return
    if cmd == "sync":
        print(json.dumps(sync_definition(), ensure_ascii=False, indent=2))
        return
    if cmd == "reset":
        default = {
            "nodes": [
                {"id": "spec", "module": "spec", "x": 60, "y": 80},
                {"id": "plan", "module": "plan", "x": 300, "y": 80},
                {"id": "script", "module": "script", "x": 540, "y": 80},
                {"id": "produce", "module": "produce", "x": 780, "y": 80},
                {"id": "qa", "module": "qa", "x": 1020, "y": 80},
                {"id": "review", "module": "review", "x": 1260, "y": 80},
                {"id": "metrics", "module": "metrics", "x": 1500, "y": 80},
            ],
            "edges": [{"from": a, "to": b} for a, b in
                      zip(["spec", "plan", "script", "produce", "qa", "review"],
                          ["plan", "script", "produce", "qa", "review", "metrics"])],
        }
        save_canvas(default)
        print(json.dumps({"ok": True, "nodes": len(default["nodes"]), "synced": sync_definition()}, ensure_ascii=False))
        return
    if cmd == "add":
        module = args.module
        if module not in modules:
            raise SystemExit("未知模块：" + module)
        ids = [n["id"] for n in graph["nodes"]]
        nid = module
        i = 2
        while nid in ids:
            nid = f"{module}-{i}"
            i += 1
        y = 80 + (len(ids) % 4) * 130
        graph["nodes"].append({"id": nid, "module": module, "x": 60 + (len(ids) % 6) * 240, "y": y})
        if args.after:
            if args.after not in ids:
                raise SystemExit("after 节点不存在：" + args.after)
            graph["edges"].append({"from": args.after, "to": nid})
        elif ids:
            graph["edges"].append({"from": ids[-1], "to": nid})
        save_canvas(graph)
        print(json.dumps({"ok": True, "added": nid, "synced": sync_definition()}, ensure_ascii=False))
        return
    if cmd == "remove":
        if args.id not in [n["id"] for n in graph["nodes"]]:
            raise SystemExit("节点不存在：" + args.id)
        graph["nodes"] = [n for n in graph["nodes"] if n["id"] != args.id]
        graph["edges"] = [e for e in graph["edges"] if e["from"] != args.id and e["to"] != args.id]
        save_canvas(graph)
        print(json.dumps({"ok": True, "removed": args.id, "synced": sync_definition()}, ensure_ascii=False))
        return
    if cmd == "connect":
        ids = [n["id"] for n in graph["nodes"]]
        if args.from_ not in ids or args.to not in ids:
            raise SystemExit("连线节点不存在")
        if not any(e["from"] == args.from_ and e["to"] == args.to for e in graph["edges"]):
            graph["edges"].append({"from": args.from_, "to": args.to})
        save_canvas(graph)
        print(json.dumps({"ok": True, "edges": len(graph["edges"]), "synced": sync_definition()}, ensure_ascii=False))
        return
    if cmd == "disconnect":
        graph["edges"] = [e for e in graph["edges"] if not (e["from"] == args.from_ and e["to"] == args.to)]
        save_canvas(graph)
        print(json.dumps({"ok": True, "edges": len(graph["edges"]), "synced": sync_definition()}, ensure_ascii=False))
        return
    raise SystemExit("未知 canvas 命令")


def run_workflow(stem, resume_run=None, force=False, until=None, fresh=False):
    d = load_def()
    version = def_hash(d)
    if resume_run:
        run_id = resume_run
        state = read_json(os.path.join(run_dir(run_id), "state.json"))
        if not state:
            raise SystemExit("找不到 run：" + run_id)
        stem = state["episode"]
        if state["definition_version"] != version:
            print("WARN 定义已变更，继续时以当前定义执行；新版本：" + version)
            state["definition_version"] = version
    else:
        run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + stem
        os.makedirs(run_dir(run_id), exist_ok=True)
        state = {
            "run_id": run_id,
            "episode": stem,
            "status": "running",
            "created_at": now(),
            "updated_at": now(),
            "definition_name": d.get("name", ""),
            "definition_version": version,
            "cursor": 0,
            "nodes": [{"id": n["id"], "name": n.get("name", n["id"]), "kind": n.get("kind", "auto"),
                       "status": "pending", "detail": "", "attempts": 0} for n in d["nodes"]],
        }
        write_json(os.path.join(run_dir(run_id), "definition.json"), d)
        set_latest(run_id)
        event(run_id, "run_start", episode=stem, definition_version=version)

    def save():
        state["updated_at"] = now()
        write_json(os.path.join(run_dir(run_id), "state.json"), state)

    if not acquire_lock(run_id):
        print("另一个运行正在进行（.runs/.lock）；结束或删除锁后再试")
        return 5
    try:
        rc = _run_nodes(d, state, run_id, stem, save, force, until)
        try:
            ret = load_def().get("retention") or {}
            if ret.get("auto", True):
                pruned = prune_runs(int(ret.get("keep", 20)))
                if pruned:
                    print(f"🧹 自动归档 {len(pruned)} 个旧运行（保留 {ret.get('keep', 20)}）")
        except Exception:
            pass
        return rc
    finally:
        release_lock()


def _record(node, state, run_id, stem, status, detail, save, idx):
    ns = node_state(state, node["id"])
    if status == STATUS_DONE:
        missing = [a for a in node.get("artifacts", []) if not exists(a, stem, run_id)]
        if missing:
            status = STATUS_FAIL
            detail = "节点完成但缺少产物：" + "、".join(missing)
        elif not ns.get("artifact_evidence"):
            evidence, unreadable = _artifact_evidence(node, state)
            if unreadable:
                status = STATUS_FAIL
                detail = "节点完成但产物为空或不可读：" + "、".join(unreadable)
            else:
                ns["artifact_evidence"] = evidence
                ns["checks"] = []
                ns["checks_source"] = "artifacts"
                event(run_id, "node_evidence", node=node["id"], artifacts=len(evidence))
    ns["status"] = status
    ns["detail"] = str(detail)[:600]
    ns["attempts"] = ns.get("attempts", 0) + 1
    ns["finished_at"] = now()
    state["cursor"] = idx
    save()
    event(run_id, "node_result", node=node["id"], status=status, detail=ns["detail"])
    return status


def _terminal(status, node, state, save, run_id):
    if status == STATUS_WAIT:
        state["status"] = STATUS_WAIT
        save()
        print(f"⏸ 运行停在人工闸门：{node['name']}（{run_id}）")
        print(f"   批准：python wf.py approve {run_id}")
        return 3
    if status == STATUS_BLOCK:
        state["status"] = STATUS_BLOCK
        save()
        print(f"⛔ 阻塞：{node['name']} — {node_state(state, node['id'])['detail']}")
        return 4
    if status == STATUS_FAIL:
        state["status"] = STATUS_FAIL
        save()
        print(f"❌ 失败：{node['name']} — {node_state(state, node['id'])['detail']}")
        return 1
    return None


def _run_nodes(d, state, run_id, stem, save, force, until=None):
    nodes = d["nodes"]
    i = 0
    while i < len(nodes):
        node = nodes[i]
        ns = node_state(state, node["id"])
        if ns["status"] == STATUS_DONE and not force:
            i += 1
            continue
        cond_ok, cond_detail = eval_condition(node, stem, run_id)
        if not cond_ok:
            _record(node, state, run_id, stem, "skipped", "条件不成立，跳过：" + cond_detail, save, i)
            print(f"↷ 跳过（条件不成立）：{node['name']} — {cond_detail}")
            i += 1
            continue

        group = node.get("parallel_group")
        group_nodes = [node]
        j = i + 1
        if group:
            while j < len(nodes) and nodes[j].get("parallel_group") == group:
                group_nodes.append(nodes[j])
                j += 1
        pending = []
        for n in group_nodes:
            nst = node_state(state, n["id"])
            if nst["status"] == STATUS_DONE and not force:
                continue
            ok, det = eval_condition(n, stem, run_id)
            if not ok:
                _record(n, state, run_id, stem, "skipped", "条件不成立，跳过：" + det, save, i)
                print(f"↷ 跳过（条件不成立）：{n['name']}")
                continue
            pending.append(n)

        if not pending:
            i = j
            continue

        state["status"] = "running"
        save()
        if len(pending) == 1:
            n = pending[0]
            status, detail = execute_node(n, state, force=force)
            status = _record(n, state, run_id, stem, status, detail, save, i)
            rc = _terminal(status, n, state, save, run_id)
            if rc is not None:
                return rc
        else:
            print(f"⇉ 并行执行 {len(pending)} 个节点：" + "、".join(n["name"] for n in pending))
            results = []
            with ThreadPoolExecutor(max_workers=len(pending)) as ex:
                futures = [(n, ex.submit(execute_node, n, state, force)) for n in pending]
                for n, fut in futures:
                    try:
                        status, detail = fut.result()
                    except Exception as exc:
                        status, detail = STATUS_FAIL, "并行异常：" + str(exc)[:300]
                    status = _record(n, state, run_id, stem, status, detail, save, i)
                    results.append((n, status))
            worst = next((n for n, st in results if st == STATUS_WAIT), None)
            if worst is None:
                worst = next((n for n, st in results if st == STATUS_BLOCK), None)
            if worst is None:
                worst = next((n for n, st in results if st == STATUS_FAIL), None)
            if worst is not None:
                rc = _terminal(node_state(state, worst["id"])["status"], worst, state, save, run_id)
                if rc is not None:
                    return rc
        done_ids = [n["id"] for n in pending]
        if until and until in done_ids:
            state["status"] = "creation_ready"
            save()
            event(run_id, "creation_ready", node=until)
            print(f"🎬 创作包已就绪（停到 {until}）：{run_id}")
            return 0
        i = j
    state["status"] = "completed"
    save()
    event(run_id, "run_completed")
    print(f"✅ 完成：{run_id}（{len(nodes)} 个节点）")
    try:
        keep = int((load_def().get("retention") or {}).get("keep", 20))
        if (load_def().get("retention") or {}).get("auto", True):
            pruned = prune_runs(keep)
            if pruned:
                print(f"🧹 自动归档 {len(pruned)} 个旧运行（保留 {keep}）")
    except Exception:
        pass
    return 0

def _canvas_graph_for(run_id):
    return read_json(os.path.join(run_dir(run_id), "graph.json"), {"nodes": [], "edges": []})


def _canvas_predecessors(graph):
    preds = {n["id"]: [] for n in graph.get("nodes", [])}
    for e in graph.get("edges", []):
        if e.get("to") in preds:
            preds[e["to"]].append(e.get("from"))
    return preds


def _canvas_ancestors(graph, node_id):
    incoming = {}
    for edge in graph.get("edges", []) or []:
        incoming.setdefault(edge.get("to"), []).append(edge.get("from"))
    seen = set()
    stack = list(incoming.get(node_id, []))
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        stack.extend(incoming.get(current, []))
    return seen


def _pattern_key(value):
    text = str(value).replace("\\", "/").strip()
    return text.replace("{stem}", "<stem>").replace("{run_id}", "<run>")


def _validate_canvas(graph):
    nodes = graph.get("nodes", [])
    if not nodes:
        raise ValueError("画布为空，先添加模块")
    ids = [safe_component(node.get("id"), "node_id") for node in nodes]
    if len(ids) != len({value.casefold() for value in ids}):
        raise ValueError("Duplicate canvas node id")
    try:
        order = topo_order(graph)
    except SystemExit as exc:
        raise ValueError(str(exc)) from exc
    # Compose-time data-flow check: a declared input must be produced by an
    # upstream node, or live in shared/task knowledge. This turns "连线只是顺序"
    # into an explicit contract instead of failing halfway through a run.
    modules = {m["id"]: m for m in load_modules()}
    by_id = {n["id"]: n for n in nodes}
    for nid in order:
        node = by_id[nid]
        module = modules.get(node.get("module"), {})
        requires = node.get("requires") if isinstance(node.get("requires"), list) else module.get("requires", [])
        if not requires:
            continue
        upstream_patterns = set()
        for ancestor in _canvas_ancestors(graph, nid):
            prev = by_id.get(ancestor, {})
            prev_module = modules.get(prev.get("module"), {})
            artifacts = prev.get("artifacts") if isinstance(prev.get("artifacts"), list) else prev_module.get("artifacts", [])
            for artifact in artifacts or []:
                upstream_patterns.add(_pattern_key(artifact))
        for req in requires:
            value = _pattern_key(req)
            if not value or value.startswith(("knowledge/", "shared/", "files/", "inputs/")):
                continue
            if value in upstream_patterns:
                continue
            if any(pattern.endswith("/") and value.startswith(pattern) for pattern in upstream_patterns):
                continue
            raise ValueError("节点「" + str(node.get("name") or nid) + "」缺输入：" + value
                             + "；上游没有任何节点声明这个产物。请先连上产出它的节点，或把资料放进知识库。")


def active_workflow_meta():
    """Return the currently selected workflow library entry, if any."""
    try:
        index = load_json(os.path.join(BASE, "kb", "workflows", "index.json"), {})
        active = index.get("active", "")
        row = next((w for w in index.get("workflows", []) if w.get("id") == active), None)
        return active, (row or {}).get("name", active)
    except Exception:
        return "", ""


def canvas_run(episode=None, until=None, fresh=False, resume_run=None, reply=None, _lock_owned=False, project_id=None, request_id=None):
    import kb

    command = None
    if project_id:
        from wf_projects import ProjectStore
        command = ProjectStore(BASE).run_command(request_id)
        if command['project_id'] != project_id:
            raise ValueError('运行命令与项目不匹配')
        episode = command['episode']
        until = command.get('until')
    if resume_run:
        run_id = safe_component(resume_run, "run_id")
    else:
        task_name = safe_component(episode, "task name")
        run_id = command["run_id"] if command else datetime.now().strftime("%Y%m%d-%H%M%S-%f") + "-" + kb.slugify(task_name)[:40] + "-" + uuid.uuid4().hex[:6]
    if not _lock_owned and not acquire_lock(run_id):
        print("另一个运行仍在执行或等待进程清理，请稍后再试。")
        return 5
    state = None
    try:
        if command and os.path.isfile(os.path.join(run_dir(run_id), 'state.json')):
            return 0  # An admitted command never starts a second execution.
        if resume_run:
            state = read_json(os.path.join(run_dir(run_id), "state.json"), None)
            if not state:
                raise ValueError("找不到 run：" + run_id)
            if state.get("cleanup_pending"):
                raise ValueError("执行进程清理未确认，不能启动新的执行。")
            try:
                os.remove(os.path.join(run_dir(run_id), ".pause"))
            except OSError:
                pass
            graph = _canvas_graph_for(run_id)
            snapshot_path = os.path.join(run_dir(run_id), "modules.json")
            snapshot = read_json(snapshot_path, None)
            if snapshot is None:
                snapshot = {"modules": load_modules()}
                write_json(snapshot_path, snapshot)
                state["snapshot_migrated"] = True
            modules = {module["id"]: module for module in snapshot.get("modules", [])}
            stem = safe_component(state["episode"], "task name")
            state["task_id"] = kb.ensure_task(state.get("task_id") or stem)
            if reply is not None:
                node_id = safe_component(reply.get("node_id"), "node_id")
                answer = str(reply.get("answer") or "").strip()
                target = node_state(state, node_id)
                if (state["status"] != STATUS_INPUT or not target or target["status"] != STATUS_INPUT
                        or not answer or len(answer) > 20000):
                    raise ValueError("只能回答当前暂停的问题，回复须为 1..20000 个字符。")
                target.setdefault("user_answers", []).append(answer)
                _message(state, target, 'assistant', target.get('question'), kind='question',
                         key='turn:' + str(int(target.get('agent_turns',0))) + ':question', source='workflow_question')
                _message(state, target, 'user', answer, kind='answer',
                         key='answer:' + str(len(target['user_answers'])), source='user')
                target.setdefault("questions", []).append({"question": target.get("question"), "answer": answer, "at": now()})
                target["status"] = "pending"
                target.pop("question", None)
                state["status"] = "running"
                event(run_id, "user_reply", node=node_id, answer=answer)
            elif state.get("status") == STATUS_INPUT:
                return 6
            until = state.get("until") if until is None else until
        else:
            graph = command["project"]["graph"] if command else load_canvas()
            _validate_canvas(graph)
            modules = {module["id"]: module for module in load_modules()}
            stem = (run_id + "-" + kb.slugify(task_name)[:40]) if command else task_name
            task_id = kb.ensure_task(project_id if command else task_name)
            state = {
                "run_id": run_id, "episode": stem, "task_id": task_id, "task_name": task_name,
                "status": "running", "mode": "canvas", "agent_mode": "automatic", "until": until,
                "created_at": now(), "updated_at": now(),
                "definition_name": "画布工作流", "definition_version": "canvas-auto-v1",
                "nodes": [{"id": node["id"], "module": node.get("module"),
                           "name": node.get("name") or modules.get(node.get("module"), {}).get("name", node["id"]),
                           "status": "pending", "detail": "", "attempts": 0, "user_answers": [],
                           "checks": initial_checks(node, modules)}
                          for node in graph["nodes"]],
                "edges": graph.get("edges", []),
            }
            if command:
                state.update(schema_version=2, project_id=project_id, project_revision=command['project_revision'],
                             project_goal=command['project']['goal'], delivery_requirements=command['project']['delivery'],
                             request_id=request_id)
                state['run_kind'] = command.get('purpose') or 'execution'
            write_json(os.path.join(run_dir(run_id), "graph.json"), graph)
            write_json(os.path.join(run_dir(run_id), "modules.json"), {"modules": list(modules.values())})
            registry = read_json(os.path.join(BASE, "kb", "skill-registry.json"), None)
            if registry is not None:
                write_json(os.path.join(run_dir(run_id), "skill-registry.json"), registry)
            tools = read_json(os.path.join(BASE, "kb", "execution-tools.json"), None)
            if tools is not None:
                write_json(os.path.join(run_dir(run_id), "execution-tools.json"), tools)
            # New runs do not import old project output files into their execution workspaces.
            set_latest(run_id)
            event(run_id, "run_start", episode=stem, task_id=task_id, mode="canvas", agent_mode="automatic")
        _validate_canvas(graph)
        graph_nodes = {node["id"]: node for node in graph["nodes"]}
        predecessors = _canvas_predecessors(graph)
        cancel_path = os.path.join(run_dir(run_id), ".cancel")
        pause_path = os.path.join(run_dir(run_id), ".pause")
        _canvas_save(state)
        while True:
            touch_lock()
            if os.path.exists(cancel_path):
                state["status"] = "cancelled"
                _canvas_save(state)
                event(run_id, "run_cancelled")
                return 130
            if os.path.exists(pause_path):
                try:
                    os.remove(pause_path)
                except OSError:
                    pass
                state["status"] = "paused"
                _canvas_save(state)
                event(run_id, "run_paused")
                return 7
            if until and node_state(state, until) and node_state(state, until)["status"] in (STATUS_DONE, STATUS_SKIP):
                state["status"] = "completed" if all(n["status"] in (STATUS_DONE, STATUS_SKIP) for n in state["nodes"]) else "creation_ready"
                _canvas_save(state)
                event(run_id, "run_completed" if state["status"] == "completed" else "creation_ready", node=until)
                return 0
            waiting = [node for node in state["nodes"] if node["status"] == STATUS_INPUT]
            if waiting:
                state["status"] = STATUS_INPUT
                _canvas_save(state)
                return 6
            pending = [node for node in state["nodes"] if node["status"] in ("pending", STATUS_AGENT)]
            if not pending:
                uncertain = [node for node in state["nodes"] if node["status"] not in (STATUS_DONE, STATUS_SKIP)]
                if uncertain:
                    uncertain[0]["status"] = STATUS_INPUT
                    uncertain[0]["question"] = "步骤尚未取得已验证的完成结果，请说明如何继续。"
                    state["status"] = STATUS_INPUT
                    _canvas_save(state)
                    return 6
                state["status"] = "completed"
                _canvas_save(state)
                event(run_id, "run_completed", errors=False)
                return 0
            ready = []
            for ns in pending:
                previous = [node_state(state, item) for item in predecessors.get(ns["id"], [])]
                if not all(item and item["status"] in (STATUS_DONE, STATUS_SKIP) for item in previous):
                    continue
                gn = graph_nodes[ns["id"]]
                module = modules.get(gn.get("module"))
                if not module:
                    ns["status"] = STATUS_INPUT
                    ns["question"] = "找不到模块配置：" + str(gn.get("module"))
                    continue
                node = {"id": ns["id"], "module": gn.get('module'), "name": gn.get("name") or ns["name"], "executor": module.get("executor")}
                for key, default in (("requires", []), ("artifacts", []), ("checklist", [])):
                    node[key] = gn[key] if isinstance(gn.get(key), list) else module.get(key, default)
                for key, default in (("agent_prompt", ""), ("condition", None), ("timeout_seconds", 180), ("knowledge_aliases", {}), ("skills", []), ("skill_focus", {}), ("validation", {}), ("capture", {}), ("capture_url", None), ("capture_width", None), ("capture_height", None)):
                    node[key] = gn.get(key, module.get(key, default))
                node['timeout_seconds'] = gn.get('timeout_seconds', gn.get('timeout_sec', module.get('timeout_seconds', module.get('timeout_sec', 180))))
                for key, default in (('input_bindings', {}), ('input_schemas', {}), ('deliveries', {}),
                                     ('required_knowledge', []), ('review_required', False), ('capabilities', [])):
                    node[key] = gn.get(key, module.get(key, default))
                if "knowledge_aliases" not in gn and isinstance(gn.get("artifacts"), list):
                    defaults = module.get("artifacts", [])
                    node["knowledge_aliases"] = {
                        (node["artifacts"][defaults.index(source)]
                         if source in defaults and defaults.index(source) < len(node["artifacts"]) else source): target
                        for source, target in (node["knowledge_aliases"] or {}).items()
                        if source in node['artifacts'] or (source in defaults and defaults.index(source) < len(node['artifacts']))
                    }
                # The adapter has a bounded turn. Failed/uncertain results pause rather than looping models.
                node["retry"] = 0 if node["executor"] == "agent" else min(int(module.get("retry", 0)), 2)
                ok, detail = eval_condition(node, stem, run_id)
                if not ok:
                    ns.update(status=STATUS_SKIP, detail="条件不成立：" + detail, finished_at=now())
                    event(run_id, "node_result", node=ns["id"], status=STATUS_SKIP, detail=ns["detail"])
                else:
                    ready.append((ns, node))
            if any(node["status"] == STATUS_INPUT for node in state["nodes"]):
                continue
            if not ready:
                if all(node["status"] in (STATUS_DONE, STATUS_SKIP) for node in state["nodes"]):
                    continue
                for ns in pending:
                    if ns["status"] == "pending":
                        ns["status"] = STATUS_INPUT
                        ns["question"] = "步骤依赖无法继续，请检查上游结果或连线。"
                        break
                _canvas_save(state)
                continue
            state["status"] = "running"
            _canvas_save(state)
            event(run_id, "batch_start", nodes=len(ready))

            def do_one(item):
                ns, node = item
                with STATE_LOCK:
                    ns.pop('finished_at', None)
                    ns.pop('finished_at_epoch', None)
                    ns.update(status="running", started_at=now(), started_at_epoch=time.time())
                    _canvas_save(state)
                status, detail = _execute_with_heartbeat(node, state)
                if status == STATUS_DONE and node["executor"] != "agent":
                    missing = [item for item in node["artifacts"] if not exists(item, stem, run_id)]
                    if missing:
                        status, detail = STATUS_FAIL, "节点缺少产物：" + "、".join(missing)
                    elif not ns.get("artifact_evidence"):
                        evidence, unreadable = _artifact_evidence(node, state)
                        if unreadable:
                            status, detail = STATUS_FAIL, "节点产物为空或不可读：" + "、".join(unreadable)
                        else:
                            with STATE_LOCK:
                                ns["artifact_evidence"] = evidence
                                ns["checks"] = []
                                ns["checks_source"] = "artifacts"
                                _canvas_save(state)
                            event(run_id, "node_evidence", node=node["id"], artifacts=len(evidence))
                if status in (STATUS_FAIL, STATUS_BLOCK):
                    status = STATUS_INPUT
                    _node_update(ns, question="这一步遇到问题：" + str(detail) + "。请说明如何继续。")
                if status == STATUS_DONE:
                    try:
                        ns['artifact_evidence'] = _freeze_evidence(state, ns)
                        if node.get('review_required') or any(p.get('review_required') for p in (node.get('deliveries') or {}).values()):
                            ns['review_status'] = 'pending'
                    except Exception as exc:
                        status, detail = STATUS_INPUT, '保存版本成果失败：' + str(exc)
                        _node_update(ns, question=detail)
                if status == STATUS_INPUT:
                    _message(state, ns, 'assistant', ns.get('question') or str(detail), kind='question',
                             key='turn:' + str(int(ns.get('agent_turns',0))) + ':question', source='workflow_question')
                with STATE_LOCK:
                    ns.update(status=status, detail=str(detail)[:600], finished_at_epoch=time.time(),
                              attempts=int(ns.get("attempts", 0)) + 1, finished_at=now())
                    _record_progress(state, ns, {"phase": "completed" if status == STATUS_DONE else "waiting_input" if status == STATUS_INPUT else status, "worker_alive": False})
                    _canvas_save(state)
                event(run_id, "node_result", node=ns["id"], status=status, detail=ns["detail"])
                return status

            # Agent nodes are serialized: one DeepSeek worker at a time avoids
            # token/cost races and overlapping project writes. Other ready nodes
            # stay pending for the next scheduler iteration.
            agent_ready = [item for item in ready if item[1].get("executor") == "agent"]
            if agent_ready:
                ready = [agent_ready[0]]
            if len(ready) == 1:
                results = [do_one(ready[0])]
            else:
                with ThreadPoolExecutor(max_workers=min(4, len(ready))) as executor:
                    results = list(executor.map(do_one, ready))
            _canvas_save(state)
            if state.get("cleanup_pending"):
                state["status"] = STATUS_INPUT
                _canvas_save(state)
                write_json(LOCKFILE, {"run_id": run_id, "pid": os.getpid(), "cleanup_pending": True})
                return 6
            if "cancelled" in results:
                state["status"] = "cancelled"
                _canvas_save(state)
                return 130
            if STATUS_WAIT in results:
                state["status"] = STATUS_WAIT
                _canvas_save(state)
                return 3
    finally:
        if not state or not state.get("cleanup_pending"):
            release_lock()


def canvas_reply(run_id, node_id, answer):
    return canvas_run(resume_run=run_id, reply={"node_id": node_id, "answer": answer})


def canvas_revise(run_id, node_id, answer, request_id=None, _lock_owned=False):
    """Revise one node, preserve its previous generation and invalidate dependents."""
    run_id, node_id = safe_component(run_id, 'run_id'), safe_component(node_id, 'node_id')
    answer = str(answer or '').strip()
    if not answer or len(answer) > 20000:
        raise ValueError('请输入修改要求（最多20000字符）')
    request_id = safe_component(request_id, 'request_id') if request_id else uuid.uuid4().hex
    if not _lock_owned and not acquire_lock(run_id):
        return 5
    delegated = False
    try:
        state = read_json(os.path.join(run_dir(run_id),'state.json'),None)
        if not state:
            raise ValueError('找不到运行')
        requests = state.setdefault('revision_requests',{})
        if request_id in requests:
            return 0
        if state.get('status') == 'running' or state.get('cleanup_pending'):
            raise ValueError('请先停止当前执行，再提交修订')
        target = node_state(state,node_id)
        if not target:
            raise ValueError('该运行没有此节点')
        graph = _canvas_graph_for(run_id)
        if not graph:
            raise ValueError('运行缺少流程快照，无法安全修订')
        old = copy.deepcopy({k:v for k,v in target.items() if k != 'generation_history'})
        old['artifact_evidence'] = _freeze_evidence(state,target)
        old['generation'] = int(target.get('execution_generation',1))
        old['archived_at'] = now()
        target.setdefault('generation_history',[]).append(old)
        generation = old['generation'] + 1
        for key in ('agent_session_id','agent_has_session','agent_report','artifact_evidence','knowledge_evidence',
                    'validation_evidence','question','finished_at','started_at','live','activity','started_at_epoch',
                    'finished_at_epoch','agent_turns','stale_due_to'):
            target.pop(key,None)
        target.update(execution_generation=generation,status='pending',attempts=0,detail='',
                      user_answers=[answer],checks=[False]*len(target.get('checks') or []))
        if target.get('review_status'):
            target['review_status'] = 'pending'
        _message(state,target,'user',answer,kind='revision',key='revision:' + request_id,source='user')
        adjacent = {n['id']:[] for n in graph.get('nodes',[])}
        for edge in graph.get('edges',[]):
            adjacent.setdefault(edge['from'],[]).append(edge['to'])
        affected, stack = set(), list(adjacent.get(node_id,[]))
        while stack:
            current = stack.pop()
            if current in affected:
                continue
            affected.add(current); stack.extend(adjacent.get(current,[]))
        for downstream in state['nodes']:
            if downstream['id'] in affected:
                downstream.update(status='stale',stale_due_to={'node_id':node_id,'generation':generation},
                                  detail='上游已修订，请更新此步骤后再交付')
        requests[request_id] = {'node_id':node_id,'generation':generation,'at':now(),'affected':sorted(affected)}
        state['status']='running'
        cancel_flag = os.path.join(run_dir(run_id),'.cancel')
        if os.path.exists(cancel_flag):
            os.remove(cancel_flag)
        pause_flag = os.path.join(run_dir(run_id), '.pause')
        if os.path.exists(pause_flag):
            os.remove(pause_flag)
        _canvas_save(state)
        event(run_id,'node_revision',node=node_id,generation=generation,affected=sorted(affected),request_id=request_id)
        delegated=True
        return canvas_run(resume_run=run_id,until=node_id,_lock_owned=True)
    finally:
        if not delegated:
            release_lock()


def canvas_rerun(run_id, from_node=None, feedback=None, _lock_owned=False):
    run_id = safe_component(run_id, "run_id")
    from_node = safe_component(from_node, "node_id") if from_node else None
    if not _lock_owned and not acquire_lock(run_id):
        return 5
    delegated = False
    try:
        state = read_json(os.path.join(run_dir(run_id), "state.json"), None)
        graph = _canvas_graph_for(run_id)
        if not state or not graph:
            raise ValueError("找不到 run 或画布快照：" + run_id)
        if state.get("status") == "running" or state.get("cleanup_pending"):
            raise ValueError("运行尚未停止或进程清理未确认，不能重跑。")
        ids = [node["id"] for node in state["nodes"]]
        if from_node is not None and from_node not in ids:
            raise ValueError("节点不存在：" + from_node)
        adjacent = {node["id"]: [] for node in graph["nodes"]}
        for edge in graph.get("edges", []):
            adjacent[edge["from"]].append(edge["to"])
        targets, stack = set(), ([from_node] if from_node else [ns['id'] for ns in state['nodes'] if ns['status'] == 'stale'])
        if not stack:
            raise ValueError("没有需要更新的下游节点。")
        while stack:
            current = stack.pop()
            if current in targets:
                continue
            targets.add(current)
            stack.extend(adjacent.get(current, []))
        for ns in state["nodes"]:
            if ns["id"] in targets:
                previous_generation = copy.deepcopy({k:v for k,v in ns.items() if k != 'generation_history'})
                previous_generation['generation'] = int(ns.get('execution_generation',1))
                previous_generation['archived_at'] = now()
                previous_generation['artifact_evidence'] = _freeze_evidence(state,ns)
                has_deliverable = bool(previous_generation.get('artifact_evidence'))
                if has_deliverable:
                    ns.setdefault('generation_history',[]).append(previous_generation)
                else:
                    ns.setdefault('attempt_history', []).append(previous_generation)
                ns.update(status="pending", detail="", questions=[])
                if has_deliverable:
                    ns.update(attempts=0, execution_generation=int(ns.get("execution_generation", 1)) + 1,
                              user_answers=[], agent_turns=0)
                    if ns.get('review_status'):
                        ns['review_status'] = 'pending'
                for key in ("agent_session_id", "agent_has_session", "agent_report", "artifact_evidence", "knowledge_evidence", "loaded_skills", "validation_evidence", "question", "finished_at", "live", "activity", "supervisor_heartbeat_at", "started_at_epoch", "finished_at_epoch", "stale_due_to"):
                    ns.pop(key, None)
                ns["checks"] = [False] * len(ns.get("checks") or [])
                if feedback and (from_node is None or ns['id'] == from_node):
                    ns.setdefault('user_answers', []).append(str(feedback))
                    _message(state, ns, 'user', str(feedback), kind='retry_feedback', source='user')
        state["status"] = "running"
        state["until"] = None
        cancel_flag = os.path.join(run_dir(run_id), ".cancel")
        if os.path.exists(cancel_flag):
            os.remove(cancel_flag)
        pause_flag = os.path.join(run_dir(run_id), ".pause")
        if os.path.exists(pause_flag):
            os.remove(pause_flag)
        _canvas_save(state)
        event(run_id, "canvas_rerun", node=from_node, affected=sorted(targets))
        delegated = True
        return canvas_run(resume_run=run_id, _lock_owned=True)
    finally:
        if not delegated:
            release_lock()


def prune_runs(keep):
    import shutil as _sh
    os.makedirs(RUNS, exist_ok=True)
    dirs = [d for d in sorted(os.listdir(RUNS)) if os.path.isdir(os.path.join(RUNS, d)) and not d.startswith("_")]
    old = dirs[:-keep] if keep > 0 and len(dirs) > keep else []
    archive = os.path.join(RUNS, "_archive")
    os.makedirs(archive, exist_ok=True)
    for d in old:
        _sh.move(os.path.join(RUNS, d), os.path.join(archive, d))
    return old


def print_status(state):
    if not state:
        print("没有运行记录")
        return
    print(f"运行 {state['run_id']} ｜ {state['episode']} ｜ 状态 {state['status']} ｜ 定义 {state['definition_version']}")
    print(f"{'节点':<16}{'状态':<18}说明")
    for n in state["nodes"]:
        print(f"{n['name']:<16}{n['status']:<18}{n.get('detail','')[:90]}")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run")
    p_run.add_argument("--episode", required=True)
    p_run.add_argument("--force", action="store_true")
    p_run.add_argument("--resume", default=None)
    p_run.add_argument("--until", default=None, help="做到指定节点后停止（创作模式，如 review）")
    p_run.add_argument("--fresh", action="store_true", help="清除成片/质检/审核产物后重新制作（改稿后使用）")

    p_st = sub.add_parser("status")
    p_st.add_argument("--run", default=None)
    p_st.add_argument("--json", action="store_true")

    p_ap = sub.add_parser("approve")
    p_ap.add_argument("run_id")
    p_ap.add_argument("--note", default="")

    p_rj = sub.add_parser("reject")
    p_rj.add_argument("run_id")
    p_rj.add_argument("--note", default="")

    p_rs = sub.add_parser("resume")
    p_rs.add_argument("run_id")
    p_rs.add_argument("--force", action="store_true")
    p_rs.add_argument("--until", default=None)

    p_log = sub.add_parser("logs")
    p_log.add_argument("--run", default=None)

    p_ad = sub.add_parser("agent-done")
    p_ad.add_argument("--run", required=True)
    p_ad.add_argument("--node", required=True)
    p_ad.add_argument("--accept-all", action="store_true")
    p_ac = sub.add_parser("agent-check")
    p_ac.add_argument("--run", required=True)
    p_ac.add_argument("--node", required=True)
    p_ac.add_argument("--index", type=int, default=None)
    p_ac.add_argument("--all", action="store_true")
    p_as = sub.add_parser("agent-skip")
    p_as.add_argument("--run", required=True)
    p_as.add_argument("--node", required=True)
    p_af = sub.add_parser("agent-fail")
    p_af.add_argument("--run", required=True)
    p_af.add_argument("--node", required=True)

    p_cr = sub.add_parser("canvas-run")
    p_cr.add_argument("--project-id")
    p_cr.add_argument("--request-id")
    p_cr.add_argument("--episode")
    p_cr.add_argument("--until", default=None)
    p_cr.add_argument("--fresh", action="store_true")
    p_cr.add_argument("--resume", default=None)
    p_cc = sub.add_parser("canvas-cancel")
    p_cc.add_argument("--run", default=None)
    p_ch = sub.add_parser("canvas-history")
    p_crr = sub.add_parser("canvas-rerun")
    p_crr.add_argument("--run", required=True)
    p_crr.add_argument("--from", dest="from_node", default=None, help="省略时仅更新已过期的下游")
    p_crr.add_argument("--feedback", default=None)
    p_reply = sub.add_parser("canvas-reply")
    p_reply.add_argument("--run", required=True)
    p_reply.add_argument("--node", required=True)
    p_reply.add_argument("--answer", required=True)
    p_revise = sub.add_parser('canvas-revise')
    p_revise.add_argument('--run',required=True)
    p_revise.add_argument('--node',required=True)
    p_revise.add_argument('--answer',required=True)
    p_revise.add_argument('--request-id',default=None)
    p_discuss = sub.add_parser('canvas-discuss')
    for flag in ('run', 'node', 'answer', 'request-id'):
        p_discuss.add_argument('--' + flag, required=True)
    p_discuss.add_argument('--expected-generation', required=True, type=int)

    p_create = sub.add_parser("create")
    p_create.add_argument("--topic", required=True)
    p_create.add_argument("--episode", default=None)
    p_create.add_argument("--duration", type=int, default=90)
    p_create.add_argument("--type", default="tool_test",
                          choices=["tool_test", "tutorial", "industry", "report", "case"])
    sub.add_parser("list")
    p_canvas = sub.add_parser("canvas")
    p_canvas.add_argument("canvas_cmd", choices=["list", "sync", "reset", "add", "remove", "connect", "disconnect"])
    p_canvas.add_argument("--module", default=None)
    p_canvas.add_argument("--after", default=None)
    p_canvas.add_argument("--id", default=None)
    p_canvas.add_argument("--from", dest="from_", default=None)
    p_canvas.add_argument("--to", default=None)
    sub.add_parser("audit")

    p_rb = sub.add_parser("rollback")
    p_rb.add_argument("version")
    p_rb.add_argument("--yes", action="store_true", help="确认写回旧定义（会先备份当前定义）")
    p_pr = sub.add_parser("prune")
    p_pr.add_argument("--keep", type=int, default=20)
    p_tr = sub.add_parser("trigger")
    p_tr.add_argument("--once", action="store_true")
    p_tr.add_argument("--interval", type=int, default=300)
    sub.add_parser("versions")

    p_rh = sub.add_parser("rehearse")
    p_rh.add_argument("episode")
    p_g = sub.add_parser("graph")
    p_g.add_argument("--json", action="store_true")

    args = ap.parse_args()
    os.makedirs(RUNS, exist_ok=True)

    if args.cmd == "versions":
        for rid in sorted(os.listdir(RUNS)):
            if not os.path.isdir(os.path.join(RUNS, rid)) or rid.startswith("_"):
                continue
            st = read_json(os.path.join(RUNS, rid, "state.json"), {})
            print(f"{st.get('definition_version','?'):<14}{rid:<28}{st.get('status','')}")
        return

    if args.cmd == "rollback":
        if not getattr(args, "yes", False):
            raise SystemExit("回滚会覆盖当前定义；确认请加 --yes（当前定义会自动备份）")
        target = None
        for rid in sorted(os.listdir(RUNS)):
            st = read_json(os.path.join(RUNS, rid, "state.json"), {})
            if st.get("definition_version") == args.version:
                target = os.path.join(RUNS, rid, "definition.json")
        if not target or not os.path.exists(target):
            raise SystemExit("找不到该定义版本的快照：" + args.version)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        if os.path.exists(DEF_PATH):
            import shutil as _sh
            _sh.copyfile(DEF_PATH, os.path.join(BASE, "kb", f"workflow-def.backup-{ts}.json"))
        import shutil as _sh
        _sh.copyfile(target, DEF_PATH)
        print("已回滚到定义版本 " + args.version + "（原定义已备份）")
        return

    if args.cmd == "prune":
        import shutil as _sh
        dirs = [d for d in sorted(os.listdir(RUNS)) if os.path.isdir(os.path.join(RUNS, d)) and not d.startswith("_")]
        old = dirs[:-args.keep] if len(dirs) > args.keep else []
        archive = os.path.join(RUNS, "_archive")
        os.makedirs(archive, exist_ok=True)
        for d in old:
            _sh.move(os.path.join(RUNS, d), os.path.join(archive, d))
        print(f"归档 {len(old)} 个旧运行到 .runs/_archive（保留最近 {args.keep}）")
        return

    if args.cmd == "trigger":
        if not args.once:
            try:
                fd = os.open(WATCH_LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
            except FileExistsError:
                print("已有监听进程在运行（.runs/.watch.lock）；删除锁后可重启")
                return
            import atexit
            atexit.register(lambda: os.path.exists(WATCH_LOCK) and os.remove(WATCH_LOCK))
        def once():
            d = load_def()
            latest_by_episode = {}
            for rid in sorted(os.listdir(RUNS)):
                if not os.path.isdir(os.path.join(RUNS, rid)):
                    continue
                st = read_json(os.path.join(RUNS, rid, "state.json"), {})
                latest_by_episode[st.get("episode")] = st.get("status")
            for tr in d.get("triggers", []):
                if tr.get("type") != "file":
                    continue
                import glob as _glob
                for brief in sorted(_glob.glob(os.path.join(BASE, tr.get("glob", "")))):
                    ep = os.path.basename(brief).replace("-brief.json", "")
                    if latest_by_episode.get(ep) in ("running", "awaiting_approval"):
                        print(f"skip {ep}: 已有活动运行")
                        continue
                    if latest_by_episode.get(ep) == "completed":
                        print(f"skip {ep}: 已完成")
                        continue
                    if os.path.exists(LOCKFILE):
                        print("skip: 有运行锁")
                        return
                    print(f"trigger: 启动 {ep}")
                    sys.stdout.flush()
                    subprocess.Popen([PY, WF, "run", "--episode", ep], cwd=BASE,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     creationflags=0x00000008 if os.name == "nt" else 0)
                    return
            print("trigger: 没有需要排队的新选题")
        if args.once:
            once()
        else:
            while True:
                once()
                time.sleep(max(30, args.interval))
        return

    if args.cmd == "rehearse":
        rid = latest_run()
        if not rid:
            raise SystemExit("没有运行记录，先 wf.py run")
        checks, problems, cmd, cmd_path = publish_preflight(args.episode, rid)
        print(json.dumps({"ok": not problems, "checks": checks, "problems": problems,
                          "command_file": os.path.relpath(cmd_path, BASE), "command": cmd},
                         ensure_ascii=False, indent=2))
        sys.exit(0 if not problems else 2)

    if args.cmd == "create":
        stem = args.episode or datetime.now().strftime("create-%Y%m%d-%H%M%S")
        brief = os.path.join(EPISODES, stem + "-brief.json")
        spec = os.path.join(EPISODES, stem + "-spec.json")
        plan = os.path.join(EPISODES, stem + ".json")
        write_json(brief, {"type": args.type, "topic": args.topic,
                           "duration": args.duration, "notes": ""})
        cmds = [
            [PY, "spec_compose.py", "--topic", args.topic, "--brief", brief,
             "--out", spec, "--md", spec.replace(".json", ".md")],
            [PY, "selector.py", "--brief", brief, "--out", plan],
        ]
        outputs = []
        for cmd in cmds:
            code, out, err = run_cmd(cmd, 300)
            outputs.append({"cmd": " ".join(cmd[1:3]), "ok": code == 0, "detail": (err or out)[:300]})
            if code != 0:
                print(json.dumps({"ok": False, "episode": stem, "outputs": outputs}, ensure_ascii=False, indent=2))
                sys.exit(1)
        print(json.dumps({
            "ok": True, "episode": stem, "topic": args.topic, "type": args.type,
            "brief": os.path.relpath(brief, BASE), "spec": os.path.relpath(spec, BASE),
            "plan": os.path.relpath(plan, BASE),
            "next": f"python wf.py run --episode {stem} --until review",
            "outputs": outputs,
        }, ensure_ascii=False, indent=2))
        return

    if args.cmd == "graph":
        d = load_def()
        if args.json:
            print(json.dumps(d, ensure_ascii=False, indent=2))
        else:
            for i, n in enumerate(d["nodes"]):
                arrow = "→" if i < len(d["nodes"]) - 1 else ""
                print(f"{n['id']:<12} {n['name']:<14} [{n.get('kind','auto')}] {arrow}")
        return

    if args.cmd == "agent-check":
        rid, nid = args.run, args.node
        state = read_json(os.path.join(run_dir(rid), "state.json"), None)
        if not state:
            raise SystemExit("找不到 run：" + rid)
        node = next((n for n in state.get("nodes", []) if n["id"] == nid), None)
        if not node:
            raise SystemExit("找不到节点：" + nid)
        checks = node.get("checks") or []
        if args.all:
            checks = [True] * len(checks)
        elif args.index is not None and 0 <= args.index < len(checks):
            checks[args.index] = True
        else:
            raise SystemExit("需要 --index 或 --all")
        node["checks"] = checks
        write_json(os.path.join(run_dir(rid), "state.json"), state)
        print(json.dumps({"ok": True, "checks": checks}, ensure_ascii=False))
        return

    if args.cmd == "agent-skip":
        rid, nid = args.run, args.node
        state = read_json(os.path.join(run_dir(rid), "state.json"), None)
        if not state:
            raise SystemExit("找不到 run：" + rid)
        node = next((n for n in state.get("nodes", []) if n["id"] == nid), None)
        if not node:
            raise SystemExit("找不到节点：" + nid)
        node["status"] = "skipped"
        node["detail"] = "人工跳过"
        state["status"] = "running"
        write_json(os.path.join(run_dir(rid), "state.json"), state)
        event(rid, "agent_skipped", node=nid)
        print("↷ 已跳过：" + nid)
        sys.exit(canvas_run(resume_run=rid))

    if args.cmd in ("agent-done", "agent-fail"):
        rid, nid = args.run, args.node
        state = read_json(os.path.join(run_dir(rid), "state.json"), None)
        if not state:
            raise SystemExit("找不到 run：" + rid)
        if args.cmd == "agent-done" and state.get("agent_mode") == "automatic":
            print(json.dumps({"ok": False, "error": "自动任务必须由执行回报和新产物验证完成，不能手工勾选绕过。"}, ensure_ascii=False))
            sys.exit(2)
        graph = _canvas_graph_for(rid)
        modules = {m["id"]: m for m in load_modules()}
        node = next((n for n in state.get("nodes", []) if n["id"] == nid), None)
        if not node:
            raise SystemExit("找不到节点：" + nid)
        if args.cmd == "agent-fail":
            node["status"] = STATUS_FAIL
            node["detail"] = "AI 任务失败"
            state["status"] = STATUS_FAIL
            write_json(os.path.join(run_dir(rid), "state.json"), state)
            event(rid, "agent_failed", node=nid)
            print(json.dumps({"ok": True, "failed": nid}, ensure_ascii=False))
            return
        gn = next((n for n in graph.get("nodes", []) if n["id"] == nid), {})
        mod = modules.get(gn.get("module"), {})
        artifacts = gn.get("artifacts") if isinstance(gn.get("artifacts"), list) else mod.get("artifacts", [])
        missing = [a for a in artifacts if not path_for(a, state["episode"], rid) or not os.path.exists(path_for(a, state["episode"], rid))]
        if missing:
            print(json.dumps({"ok": False, "error": "AI 任务产物还不存在", "missing": missing}, ensure_ascii=False))
            return
        if args.accept_all:
            node["checks"] = [True] * len(node.get("checks") or [])
        checks = node.get("checks") or []
        if checks and not all(checks):
            print(json.dumps({"ok": False, "error": "验收清单未全部确认", "checks": checks}, ensure_ascii=False))
            return
        node["status"] = STATUS_DONE
        node["detail"] = "AI 任务已完成"
        state["status"] = "running"
        write_json(os.path.join(run_dir(rid), "state.json"), state)
        event(rid, "agent_done", node=nid)
        print("✅ AI 任务完成，继续执行…")
        sys.exit(canvas_run(resume_run=rid))

    if args.cmd == "canvas-run":
        code = canvas_run(args.episode, args.until, args.fresh, args.resume, project_id=args.project_id, request_id=args.request_id)
        if code == 6:
            rid = latest_run()
            state = read_json(os.path.join(run_dir(rid), "state.json"), {}) if rid else {}
            waiting = [n for n in (state.get("nodes") or []) if n.get("status") == STATUS_INPUT]
            if waiting:
                print("⏸ 等待你的回复：" + (waiting[0].get("name") or waiting[0].get("id")))
                print("   在画布任务卡回复，或运行 python wf.py canvas-reply --run " + rid
                      + " --node " + waiting[0].get("id", "") + " --answer \"…\"")
        sys.exit(code)

    if args.cmd == "canvas-reply":
        sys.exit(canvas_reply(args.run, args.node, args.answer))

    if args.cmd == 'canvas-revise':
        sys.exit(canvas_revise(args.run,args.node,args.answer,args.request_id))

    if args.cmd == 'canvas-discuss':
        from wf_discussion import discuss
        print(json.dumps(discuss(sys.modules[__name__], args.run, args.node, args.answer,
                                 args.expected_generation, args.request_id), ensure_ascii=False))
        return

    if args.cmd == "canvas-cancel":
        rid = args.run or latest_run()
        if not rid:
            raise SystemExit("没有运行记录")
        open(os.path.join(run_dir(rid), ".cancel"), "w", encoding="utf-8").write("1")
        print(json.dumps({"ok": True, "cancel": rid}, ensure_ascii=False))
        return

    if args.cmd == "canvas-history":
        rows = []
        for rid in sorted(os.listdir(RUNS)):
            if rid.startswith("_") or not os.path.isdir(os.path.join(RUNS, rid)):
                continue
            st = read_json(os.path.join(RUNS, rid, "state.json"), {})
            rows.append({"run_id": rid, "episode": st.get("episode"), "status": st.get("status"),
                         "mode": st.get("mode", "linear"), "created_at": st.get("created_at")})
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return

    if args.cmd == "canvas-rerun":
        sys.exit(canvas_rerun(args.run, args.from_node, args.feedback))

    if args.cmd == "run":
        sys.exit(run_workflow(args.episode, args.resume, args.force, args.until, args.fresh))

    if args.cmd == "resume":
        st = read_json(os.path.join(run_dir(args.run_id), "state.json"), {})
        if st.get("mode") == "canvas":
            sys.exit(canvas_run(resume_run=args.run_id))
        sys.exit(run_workflow(None, args.run_id, args.force, args.until))

    if args.cmd == "status":
        rid = args.run or latest_run()
        state = read_json(os.path.join(run_dir(rid), "state.json")) if rid else None
        if args.json:
            print(json.dumps(state or {}, ensure_ascii=False, indent=2))
        else:
            print_status(state)
        return

    if args.cmd == "audit":
        r = subprocess.run([PY, "selfcheck.py", "--no-e2e"], cwd=BASE)
        sys.exit(r.returncode)

    if args.cmd == "canvas":
        canvas_manage(args)
        return

    if args.cmd == "list":
        if not os.path.isdir(RUNS):
            return
        for rid in sorted(os.listdir(RUNS)):
            if rid in ("LATEST", ".lock") or rid.startswith("_") or not os.path.isdir(os.path.join(RUNS, rid)):
                continue
            st = read_json(os.path.join(RUNS, rid, "state.json"), {})
            print(f"{rid}  {st.get('status','?'):<18}{st.get('episode','')}  {st.get('created_at','')}")
        return

    if args.cmd == "logs":
        rid = getattr(args, "run", None) or latest_run()
        path = os.path.join(run_dir(rid), "events.jsonl")
        if not os.path.exists(path):
            print("没有日志")
            return
        for line in open(path, encoding="utf-8"):
            try:
                rec = json.loads(line)
            except Exception:
                continue
            print(f"{rec.get('ts','')}  {rec.get('kind',''):<14}{rec.get('node',''):<14}{rec.get('status','')}  {str(rec.get('detail',''))[:100]}")
        return

    if args.cmd in ("approve", "reject"):
        rid = args.run_id
        rd = run_dir(rid)
        if not os.path.isdir(rd):
            raise SystemExit("找不到 run：" + rid)
        approved = args.cmd == "approve"
        write_json(os.path.join(rd, "approval.json"),
                   {"approved": approved, "by": "alex", "at": now(), "note": args.note})
        state = read_json(os.path.join(rd, "state.json"), {})
        ns = node_state(state, "approval")
        if ns is None:
            for cand in state.get("nodes", []):
                if cand.get("module") == "approval":
                    ns = cand
                    break
        if ns:
            ns["status"] = STATUS_DONE if approved else STATUS_FAIL
            ns["detail"] = ("使用者 批准" if approved else "使用者 驳回") + (("：" + args.note) if args.note else "")
            state["status"] = "running"
            write_json(os.path.join(rd, "state.json"), state)
        event(rid, "human_decision", status="approved" if approved else "rejected", note=args.note)
        print(("✅ 已批准 " if approved else "❌ 已驳回 ") + rid)
        if approved:
            print("继续运行：python wf.py resume " + rid)
        return


if __name__ == "__main__":
    main()
