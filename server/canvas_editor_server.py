#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Local editor API for the workflow canvas.

Serves the canvas HTML and reads/writes:
  GET  /api/workflow   -> kb/workflow.json
  POST /api/workflow   -> validated save (auto backup kb/workflow.backup-<ts>.json)
  GET  /api/layout     -> kb/workflow-canvas-layout.json (node positions)
  POST /api/layout     -> save positions
  GET  /api/status     -> workflow.py status --json
  GET  /api/health
CORS enabled so the canvas served from the sidebar plugin (127.0.0.1:8792) can call it.
"""
import base64
import hashlib
import hmac
import secrets
import time
import json
import os
import subprocess
import sys
import threading
import socket
import re
import uuid
import copy
import importlib.util
from pathlib import Path
from urllib.parse import parse_qs, urlsplit, urlencode, quote
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.environ.get("CANVAS_HOME") or os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.dirname(os.path.abspath(__file__))
API_VERSION = "0.17.0-rc.2"
API_PORT = int(os.environ.get("CANVAS_PORT") or 8793)
API_TOKEN = os.environ.get('CANVAS_API_TOKEN') or secrets.token_urlsafe(32)
BUILD_FILE = os.path.join(os.path.dirname(APP_DIR), 'build-info.json')
try:
    with open(BUILD_FILE, encoding='utf-8-sig') as _build_handle:
        BUILD_INFO = json.load(_build_handle)
except (OSError, ValueError):
    BUILD_INFO = {'build_id': 'development', 'schema': 2}


def media_signature(run_id, path, expires):
    message = '\n'.join([str(run_id), str(path), str(expires)]).encode('utf-8')
    return hmac.new(API_TOKEN.encode('utf-8'), message, hashlib.sha256).hexdigest()


def authorized_media(url):
    if urlsplit(url).path != '/api/wf/raw':
        return False
    q = query_params(url)
    try:
        expires = int(q.get('expires', 0))
    except (ValueError, TypeError):
        return False
    if expires < time.time() or expires > time.time() + 3700:
        return False
    return hmac.compare_digest(str(q.get('media_token') or ''), media_signature(q.get('run_id'), q.get('path'), expires))
CANVAS = os.path.join(APP_DIR, "workflow-canvas.html") if os.path.isfile(os.path.join(APP_DIR, "workflow-canvas.html")) else os.path.join(BASE, "workflow-canvas.html")
WORKFLOW = os.path.join(BASE, "kb", "workflow.json")
LAYOUT = os.path.join(BASE, "kb", "workflow-canvas-layout.json")
LIBRARY = os.path.join(BASE, "kb", "spec-library.json")
SPEC_COMPOSE = os.path.join(BASE, "spec_compose.py")
RUNS = os.path.join(BASE, ".runs")
WF = os.path.join(APP_DIR, "wf.py")
PY = sys.executable
LOCK = threading.Lock()
def workflows_dir():
    return os.path.join(BASE, "kb", "workflows")


def workflows_index():
    return os.path.join(workflows_dir(), "index.json")


def query_params(url):
    return {key: values[-1] for key, values in parse_qs(urlsplit(url).query, keep_blank_values=True).items()}


def trusted_origin(origin):
    if origin is None:
        return True  # Local CLI and the same-origin DSH proxy do not send Origin.
    try:
        value = urlsplit(origin)
        return (value.scheme == "http" and value.hostname in {"127.0.0.1", "localhost"}
                and value.port is not None and not value.username and not value.password
                and value.path in {"", "/"} and not value.query and not value.fragment)
    except ValueError:
        return False


_PLUGIN_MODULES = {}


def plugin_module(name):
    """Load the shipped implementation, never a stale development copy."""
    key = (name, os.path.realpath(BASE))
    if key not in _PLUGIN_MODULES:
        source = os.path.join(APP_DIR, name + '.py')
        spec = importlib.util.spec_from_file_location('canvas_' + name + '_' + hashlib.sha256(BASE.encode()).hexdigest()[:10], source)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        _PLUGIN_MODULES[key] = module
    return _PLUGIN_MODULES[key]


def knowledge_module():
    return plugin_module('kb')


def engine_module():
    engine = plugin_module('wf')
    engine.BASE = BASE
    for key, relative in {'RUNS': '.runs', 'LATEST': '.runs/LATEST', 'LOCKFILE': '.runs/.lock',
                          'DEF_PATH': 'kb/workflow-def.json', 'MODULES_PATH': 'kb/modules.json',
                          'CANVAS_PATH': 'kb/canvas-workflow.json', 'EPISODES': 'episodes'}.items():
        setattr(engine, key, os.path.join(BASE, relative))
    return engine


def projects_store_conflict():
    return plugin_module("wf_projects").Conflict


def projects_store():
    return plugin_module("wf_projects").ProjectStore(BASE)


def conversation_module():
    return plugin_module('wf_conversation')


def host_session_snapshot(run_id, node_id):
    host = read_json(os.path.join(RUNS, 'host-agent.json'), {})
    if not host.get('url') or not host.get('token'):
        raise ValueError('宿主尚未连接，暂时无法确认原生会话状态')
    import urllib.request
    from urllib.parse import urlencode
    url = host['url'].replace('/internal/agent', '/internal/session') + '?' + urlencode({'run_id': run_id, 'node_id': node_id})
    request = urllib.request.Request(url, headers={'x-canvas-token': host['token']})
    with urllib.request.urlopen(request, timeout=15) as response:
        data = json.load(response)
    if 'executing' not in data:
        raise ValueError('宿主尚未加载会话状态适配，请重新打开 Harness')
    return data


def selected_task(values):
    task = str(values.get("task_id") or "").strip()
    if not task:
        raise ValueError("请先选择当前任务的知识库分区")
    return knowledge_module().slugify(task)


def promote_artifact(body):
    """Explicitly confirm an immutable output as a versioned knowledge source."""
    rid, nid = safe_run_part(body.get('run_id')), safe_run_part(body.get('node'))
    state = read_json(run_path(rid, 'state.json'), {})
    if body.get('project_id') and projects_store().owner(rid) != body['project_id']:
        raise ValueError('运行不属于当前项目')
    generation = int(body.get('generation') or 0)
    payload = build_workspace_payload(state, nid, generation)
    row = next((a for a in payload['artifacts'] if a['path'] == body.get('artifact_path')), None)
    if not row or row['status'] != 'verified' or row.get('sha256') != body.get('source_sha256'):
        raise ValueError('请重新查看有完整证据的指定成果版本')
    task = state.get('project_id') or state.get('task_id')
    kb = knowledge_module()
    target = str(body.get('path') or '')
    if not (target.startswith('shared/') or target.startswith('tasks/' + str(task) + '/')):
        raise ValueError('仅可沉淀到共享知识或本项目分区')
    full = kb.safe_rel(target, task=None if target.startswith('shared/') else task)
    source = contained_path(BASE, row['path'])
    request = safe_run_part(body.get('request_id'))
    provenance = {'project_id': task, 'run_id': rid, 'node_id': nid, 'generation': generation,
                  'artifact_path': row['path'], 'logical_path': row.get('logical_path'), 'sha256': row['sha256']}
    with LOCK:
        previous = plugin_module('wf_knowledge').decision(BASE, request)
        if previous:
            if previous.get('path') != target or previous.get('source_revision') != provenance or previous.get('module_scope') != body.get('module'):
                raise ValueError('命令标识已用于其他知识决定')
            return dict(previous, ok=True, source=provenance)
        if os.path.isfile(full):
            old = kb.read_entry_content(target, task=None if target.startswith('shared/') else task)
            same = old['file_sha256'] == row['sha256'] and old.get('source_revision') == provenance
            if not same and old['file_sha256'] != body.get('expected_sha256'):
                raise ValueError('已有同名知识；请查看旧版，再选择替换并保留旧版，或另存新路径')
        os.makedirs(os.path.dirname(full), exist_ok=True)
        temporary = full + '.tmp-' + uuid.uuid4().hex
        try:
            import shutil
            shutil.copyfile(source, temporary)
            with open(temporary, 'rb') as handle:
                if hashlib.file_digest(handle, 'sha256').hexdigest() != row['sha256']:
                    raise ValueError('成果文件在沉淀时变化，请重新校验')
            os.replace(temporary, full)
        finally:
            if os.path.exists(temporary): os.remove(temporary)
        item = kb.read_entry_content(target, task=None if target.startswith('shared/') else task)
        result = plugin_module('wf_knowledge').decide(BASE, target, item['knowledge_revision'],
            'confirmed', request, source=provenance, module=body.get('module'))
    return dict(result, ok=True, path=target, source=provenance)


def safe_run_part(value):
    value = str(value or "")
    if not value or not re.fullmatch(r"[\w\-]+", value, re.UNICODE) or value in {".", ".."}:
        raise ValueError("无效的运行或节点标识")
    return value


def contained_path(root, *parts):
    root = os.path.realpath(root)
    path = os.path.realpath(os.path.join(root, *parts))
    try:
        inside = os.path.commonpath([root, path]) == root
    except ValueError:
        inside = False
    if not inside:
        raise ValueError("路径超出当前运行或版本目录")
    return path


def run_path(run_id, *parts):
    run = contained_path(RUNS, safe_run_part(run_id))
    return contained_path(run, *parts)


def process_alive(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            handle = ctypes.windll.kernel32.OpenProcess(0x00100000, False, pid)
            if not handle:
                return False
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def live_worker(run_id):
    """True only while a runner process holds the lock for this run."""
    try:
        with open(os.path.join(RUNS, ".lock"), encoding="utf-8") as handle:
            lock = json.load(handle)
    except Exception:
        return False
    if str(lock.get("run_id") or "") != str(run_id):
        return False
    if lock.get("cleanup_pending"):
        return False
    return process_alive(lock.get("pid"))


def append_run_event(run_id, kind, **fields):
    record = {"ts": datetime.now().isoformat(timespec="seconds"), "kind": kind}
    record.update(fields)
    with open(run_path(run_id, "events.jsonl"), "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def canvas_revision(graph=None):
    data = graph if graph is not None else read_json(os.path.join(BASE, "kb", "canvas-workflow.json"), {})
    raw = json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def workflow_id(value):
    value = str(value or "").strip()
    value = re.sub(r"[^0-9A-Za-z_\-\u4e00-\u9fff]+", "-", value).strip("-")
    return value[:60] or "workflow"


def workflow_file(wid):
    return contained_path(workflows_dir(), workflow_id(wid) + ".json")


def load_workflow_index():
    ensure_workflows()
    return read_json(workflows_index(), {"active": "", "workflows": []})


def save_workflow_index(index):
    os.makedirs(workflows_dir(), exist_ok=True)
    write_json(workflows_index(), index)


def save_active_workflow(graph):
    index = load_workflow_index()
    active = index.get("active")
    if not active:
        return
    path = workflow_file(active)
    data = read_json(path, {"id": active, "name": active, "description": "", "graph": {}})
    data["graph"] = graph
    data["updated"] = datetime.now().isoformat(timespec="seconds")
    write_json(path, data)


def sync_active_workflow_from_mirror():
    """The active workflow and the executable canvas mirror must converge.

    CLI commands and tests can edit the mirror while the server is alive; the
    active workflow entry must not keep a stale graph that would replace it on
    the next switch.
    """
    if not os.path.isfile(workflows_index()):
        return
    index = read_json(workflows_index(), {})
    active = index.get("active")
    if not active:
        return
    mirror = read_json(os.path.join(BASE, "kb", "canvas-workflow.json"), {})
    if not mirror.get("nodes"):
        return
    data = read_json(workflow_file(active), None)
    if not isinstance(data, dict):
        return
    if data.get("graph") != mirror:
        data["graph"] = mirror
        data["updated"] = datetime.now().isoformat(timespec="seconds")
        write_json(workflow_file(active), data)
        row = next((w for w in index.get("workflows", []) if w.get("id") == active), None)
        if row:
            row["updated"] = data["updated"]
            save_workflow_index(index)


def module_history_dir(mid):
    safe = safe_run_part(mid)
    root = os.path.join(BASE, "kb", "module-history", safe)
    os.makedirs(root, exist_ok=True)
    return root


def snapshot_module(module):
    if not isinstance(module, dict) or not module.get("id"):
        return None
    version = int(module.get("version") or 1)
    path = os.path.join(module_history_dir(module["id"]), "v%04d.json" % version)
    write_json(path, module)
    return path


def validate_import_graph(graph):
    """Validate a portable workflow without executing the engine."""
    if not isinstance(graph, dict) or not isinstance(graph.get("nodes"), list) or not graph["nodes"]:
        raise ValueError("导入文件缺少非空 nodes")
    modules = {m.get("id"): m for m in read_json(os.path.join(BASE, "kb", "modules.json"), {"modules": []}).get("modules", [])}
    ids = []
    for node in graph["nodes"]:
        nid = str(node.get("id") or "")
        if not nid or nid in ids:
            raise ValueError("节点 id 为空或重复")
        ids.append(nid)
        if node.get("module") not in modules:
            raise ValueError("节点引用了未知模块：" + str(node.get("module")))
    incoming = {nid: [] for nid in ids}
    for edge in graph.get("edges", []) or []:
        a, b = edge.get("from"), edge.get("to")
        if a not in incoming or b not in incoming:
            raise ValueError("连线引用了不存在的节点")
        incoming[b].append(a)
    # Kahn cycle check
    indeg = {nid: len(incoming[nid]) for nid in ids}
    queue = [nid for nid in ids if indeg[nid] == 0]
    seen = 0
    while queue:
        cur = queue.pop(0)
        seen += 1
        for nid in ids:
            if cur in incoming[nid]:
                indeg[nid] -= 1
                if indeg[nid] == 0:
                    queue.append(nid)
    if seen != len(ids):
        raise ValueError("导入工作流存在环")
    return graph


def workflow_dependencies(graph):
    """List the module contract needed to run this workflow elsewhere."""
    modules = {m.get("id"): m for m in read_json(os.path.join(BASE, "kb", "modules.json"), {"modules": []}).get("modules", [])}
    used = []
    for node in graph.get("nodes", []) if isinstance(graph, dict) else []:
        mid = node.get("module")
        if mid and mid not in {row["id"] for row in used}:
            mod = modules.get(mid, {})
            used.append({"id": mid, "name": mod.get("name", mid), "category": mod.get("category", ""),
                         "executor": mod.get("executor", ""), "requires": mod.get("requires", []),
                         "artifacts": mod.get("artifacts", [])})
    return used


def normalize_workflow_meta(data, wid=None):
    if not isinstance(data, dict):
        return data
    data.setdefault("tier", "experimental")
    data.setdefault("id", wid or data.get("id") or "")
    return data


def ensure_workflows():
    os.makedirs(workflows_dir(), exist_ok=True)
    if os.path.isfile(workflows_index()):
        return
    graph = read_json(os.path.join(BASE, "kb", "canvas-workflow.json"), {"nodes": [], "edges": []})
    wid = "video"
    name = "视频制作"
    data = {"id": wid, "name": name, "description": "当前默认工作流（已从旧画布迁移）",
            "tier": "experimental",
            "created": datetime.now().isoformat(timespec="seconds"),
            "updated": datetime.now().isoformat(timespec="seconds"), "graph": graph}
    write_json(workflow_file(wid), data)
    save_workflow_index({"active": wid, "workflows": [{"id": wid, "name": name, "description": data["description"],
                                                       "tier": "experimental", "updated": data["updated"]}]})


def activate_workflow(wid):
    index = load_workflow_index()
    wid = workflow_id(wid)
    if not any(w.get("id") == wid for w in index.get("workflows", [])):
        raise ValueError("工作流不存在：" + wid)
    data = normalize_workflow_meta(read_json(workflow_file(wid), None), wid)
    if not isinstance(data, dict) or not isinstance(data.get("graph"), dict):
        raise ValueError("工作流数据损坏：" + wid)
    if data["graph"].get("nodes"):
        code, err = apply_canvas_graph(data["graph"])
        if code != 0:
            raise ValueError(err or "工作流无法同步")
    else:
        write_json(os.path.join(BASE, "kb", "canvas-workflow.json"), {"nodes": [], "edges": []})
    index["active"] = wid
    save_workflow_index(index)
    return data


def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def write_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def save_modules(data):
    path = os.path.join(BASE, "kb", "modules.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = path + ".tmp-" + uuid.uuid4().hex
    try:
        write_json(temporary, data)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def run_json(args, timeout=120):
    try:
        r = subprocess.run([PY] + args, cwd=APP_DIR, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        return json.loads(r.stdout), r.returncode, (r.stderr or "")
    except Exception as exc:
        return None, 1, str(exc)


def canvas_dir():
    return os.path.join(BASE, "kb", "canvas-revisions")


def save_revision(graph):
    d = canvas_dir()
    os.makedirs(d, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:22]
    path = os.path.join(d, stamp + ".json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(graph, f, ensure_ascii=False, indent=2)
    files = sorted(f for f in os.listdir(d) if f.endswith(".json"))
    for old in files[:-40]:
        try:
            os.remove(os.path.join(d, old))
        except Exception:
            pass
    return os.path.relpath(path, BASE)


def list_revisions():
    d = canvas_dir()
    rows = []
    if os.path.isdir(d):
        for f in sorted(os.listdir(d), reverse=True):
            if not f.endswith(".json"):
                continue
            path = os.path.join(d, f)
            try:
                path = contained_path(d, f)
                g = json.load(open(path, encoding="utf-8"))
                rows.append({"id": f, "saved_at": datetime.fromtimestamp(os.path.getmtime(path)).isoformat(timespec="seconds"),
                             "nodes": len(g.get("nodes", [])), "edges": len(g.get("edges", []))})
            except Exception:
                continue
    return rows[:40]


def sync_canvas(graph):
    canvas_path = os.path.join(BASE, "kb", "canvas-workflow.json")
    write_json(canvas_path, graph)
    r = subprocess.run([PY, "wf.py", "canvas", "sync"], cwd=APP_DIR, capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=60)
    return r.returncode, (r.stderr or r.stdout)


def apply_canvas_graph(graph):
    """Write a graph and sync it atomically; restore the old file on failure."""
    canvas_path = os.path.join(BASE, "kb", "canvas-workflow.json")
    old = read_json(canvas_path, {})
    code, err = sync_canvas(graph)
    if code != 0:
        write_json(canvas_path, old)
        return code, err
    return 0, ""


def known_gates():
    data, code, err = run_json(["workflow.py", "--gates"])
    if code == 0 and isinstance(data, list):
        return {g["id"]: g.get("label", g["id"]) for g in data}
    return {}


def audit_library(library):
    data, code, err = run_json(["spec_compose.py", "--audit"])
    return code == 0, data, err


def latest_run_state():
    try:
        rid = open(os.path.join(RUNS, "LATEST"), encoding="utf-8").read().strip()
        with open(run_path(rid, "state.json"), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def annotate_run(state):
    """Read-only audit annotations: missing evidence files and execution trail.

    Never mutates files on disk; the returned dict is a per-request copy.
    """
    if not isinstance(state, dict):
        return state
    missing_total = 0
    for node in state.get("nodes", []) or []:
        missing = []
        for evidence in node.get("artifact_evidence") or []:
            relative = str((evidence or {}).get("path") or "")
            parts = relative.split("/")
            if len(parts) < 2 or parts[0] not in ("episodes", "artifacts") or ".." in parts:
                continue
            if not os.path.exists(os.path.join(BASE, *parts)):
                missing.append(relative)
        if missing:
            node["evidence_missing"] = missing
            missing_total += len(missing)
    state["evidence_missing_count"] = missing_total
    try:
        run_id = safe_run_part(state.get("run_id"))
    except ValueError:
        run_id = ""
    state["audit_trail"] = bool(run_id) and os.path.isfile(os.path.join(RUNS, run_id, "events.jsonl"))
    return state


MIME_BY_EXT = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
    ".gif": "image/gif", ".bmp": "image/bmp",
    ".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime", ".mkv": "video/x-matroska",
    ".m4v": "video/x-m4v",
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4", ".aac": "audio/aac",
    ".flac": "audio/flac", ".ogg": "audio/ogg",
    ".pdf": "application/pdf",
}
TEXT_EXT = {".md", ".txt", ".json", ".srt", ".js", ".ts", ".tsx", ".css", ".html",
            ".yaml", ".yml", ".csv", ".log", ".jsonl", ".vtt"}


def as_rel(value):
    return str(value or "").replace("\\", "/").strip().strip("/")


def expand_artifact_path(value, state):
    stem = str(state.get("episode") or state.get("task_id") or "")
    task_id = str(state.get("task_id") or stem)
    run_id = str(state.get("run_id") or "")
    return (as_rel(value).replace("{stem}", stem).replace("{task_id}", task_id)
            .replace("{run_id}", run_id))


def run_snapshot(state, name):
    rid = str((state or {}).get("run_id") or "")
    if not rid:
        return {}
    return read_json(os.path.join(RUNS, rid, name), {})


def artifact_sources(state):
    """Run-scoped declarations only; today's module library must never leak in."""
    modules = run_snapshot(state, "modules.json")
    graph = run_snapshot(state, "graph.json")
    module_map = {str(item.get("id")): item for item in (modules.get("modules") or [])
                  if isinstance(item, dict)}
    node_map = {str(item.get("id")): item for item in (graph.get("nodes") or [])
                if isinstance(item, dict)}
    return {"modules": module_map, "graph": node_map}


def declared_artifacts(node, state, sources=None):
    """Return (paths, source) for one node, fixed to that run.

    Order: the node's own declaration, the run's graph snapshot, the run's
    module snapshot. When none exists the answer is "unknown" — the current
    module library is deliberately not consulted, because editing a module
    must not change what an old run can read.
    """
    sources = sources if sources is not None else artifact_sources(state)

    def expand(values):
        rows = []
        for value in values or []:
            rel = expand_artifact_path(value, state)
            if rel and rel not in rows:
                rows.append(rel)
        return rows

    rows = expand(node.get("artifacts"))
    if rows:
        return rows, "node"
    graph_node = sources["graph"].get(str(node.get("id"))) or {}
    rows = expand(graph_node.get("artifacts"))
    if rows:
        return rows, "graph"
    module = sources["modules"].get(str(node.get("module"))) or {}
    rows = expand(module.get("artifacts"))
    if rows:
        return rows, "snapshot"
    return [], "unknown"


def artifact_kind(rel):
    ext = os.path.splitext(rel)[1].lower()
    if ext in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"):
        return "image"
    if ext in (".mp4", ".webm", ".mov", ".mkv", ".m4v"):
        return "video"
    if ext in (".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"):
        return "audio"
    if ext == ".pdf":
        return "pdf"
    if ext in TEXT_EXT:
        return "text"
    return "file"


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def directory_evidence(path):
    """Reuse the engine's tree hashing so directory artifacts can be re-verified."""
    try:
        from wf_artifacts import directory_evidence as scan
        return scan(path)
    except Exception:
        return None


def artifact_rows(state, node, sources=None):
    """List a node's artifacts with verified/legacy/changed/missing status.

    Recorded evidence is re-checked against the files on disk. Declared
    artifacts without recorded evidence are still listed as legacy so that
    runs created before evidence tracking remain inspectable, but they are
    never reported as verified.
    """
    sources = sources if sources is not None else artifact_sources(state)
    declared, declaration = declared_artifacts(node, state, sources)
    rows, seen, missing = [], set(), []
    if not isinstance(node, dict):
        return rows, missing, {key: 0 for key in ("verified", "legacy", "changed", "missing")}, "unknown"

    def add(rel, status, source, sha="", size=None):
        rel = as_rel(rel)
        if not rel or rel in seen:
            return
        try:
            full = contained_path(BASE, rel)
        except ValueError:
            return
        if not os.path.isfile(full):
            return
        seen.add(rel)
        rows.append({"path": rel, "name": os.path.basename(rel), "kind": artifact_kind(rel),
                     "bytes": int(size if size is not None else os.path.getsize(full)),
                     "status": status, "source": source, "sha256": sha})

    for item in node.get("artifact_evidence") or []:
        if not isinstance(item, dict):
            continue
        rel = as_rel(item.get("path"))
        if not rel:
            continue
        # A frozen file represents this logical output for this generation.
        # Never substitute a newer working copy, even if the snapshot is missing.
        logical = as_rel(item.get('logical_path'))
        if logical and logical != rel:
            seen.add(logical)
        try:
            full = contained_path(BASE, rel)
        except ValueError:
            continue
        if not os.path.exists(full):
            missing.append(rel)
            seen.add(rel)
            rows.append({"path": rel, "name": os.path.basename(rel), "kind": artifact_kind(rel),
                         "bytes": int(item.get("bytes") or 0), "status": "missing",
                         "source": "evidence", "sha256": str(item.get("sha256") or "")})
            continue
        if os.path.isdir(full):
            current = directory_evidence(full)
            recorded = str(item.get("sha256") or "")
            status = item.get('historical_status') or "verified"
            if not current or (recorded and str(current.get("sha256") or "") != recorded):
                status = "changed"
            for child in (current or {}).get("files", []):
                add(rel + "/" + as_rel(child.get("path")), status, "evidence-dir",
                    str(child.get("sha256") or ""), child.get("bytes"))
                if logical and rows:
                    rows[-1]['logical_path'] = logical + '/' + as_rel(child.get('path'))
            continue
        status = item.get('historical_status') or "verified"
        if item.get("sha256"):
            try:
                if sha256_file(full) != str(item.get("sha256")):
                    status = "changed"
            except OSError:
                status = "changed"
        elif item.get("bytes") and os.path.getsize(full) != int(item.get("bytes")):
            status = "changed"
        add(rel, status, "evidence", str(item.get("sha256") or ""))
        if rows and rows[-1]['path'] == rel and item.get('logical_path'):
            rows[-1]['logical_path'] = item['logical_path']

    for rel in declared:
        if any(rel == known or rel.startswith(known + "/") for known in seen):
            continue
        try:
            full = contained_path(BASE, rel)
        except ValueError:
            continue
        if not os.path.exists(full):
            missing.append(rel)
            rows.append({'path': rel, 'name': os.path.basename(rel), 'kind': artifact_kind(rel),
                         'bytes': 0, 'status': 'missing', 'source': 'declared', 'sha256': ''})
            continue
        if os.path.isdir(full):
            for child in (directory_evidence(full) or {}).get("files", []):
                add(rel + "/" + as_rel(child.get("path")), "legacy", "declared",
                    str(child.get("sha256") or ""), child.get("bytes"))
        else:
            add(rel, "legacy", "declared")

    order = {"verified": 0, "legacy": 1, "changed": 2, "missing": 3}
    rows.sort(key=lambda row: (order.get(row["status"], 9), row["path"]))
    for row in rows:
        # Chromium's dsh-app protocol exposes a zero-length seekable range even
        # when its fetch proxy returns 206. Use standard HTTP for native media.
        expires = int(time.time()) + 3600
        row['media_url'] = 'http://127.0.0.1:%d/api/wf/raw?' % API_PORT + urlencode({
            'run_id': state['run_id'], 'path': row['path'], 'expires': expires,
            'media_token': media_signature(state['run_id'], row['path'], expires)})
    counts = {key: sum(1 for row in rows if row["status"] == key)
              for key in ("verified", "legacy", "changed", "missing")}
    return rows, missing, counts, declaration


def allowed_artifact_paths(state):
    sources = artifact_sources(state)
    allowed = []
    for node in state.get("nodes", []) or []:
        for version in [node] + list(node.get('generation_history') or []):
            for item in version.get("artifact_evidence") or []:
                rel = as_rel((item or {}).get("path"))
                if rel and rel not in allowed:
                    allowed.append(rel)
        declared, _ = declared_artifacts(node, state, sources)
        for rel in declared:
            if rel not in allowed:
                allowed.append(rel)
    return allowed


def artifact_allowed(rel, allowed):
    """Scope check on the resolved path, not on a raw string prefix.

    A declared file must not become a springboard: ``episodes/report.md/../..``
    resolves outside its scope even though it shares the prefix.
    """
    rel = as_rel(rel)
    if not rel:
        return False
    if any(part in ("", ".", "..") for part in rel.split("/")):
        return False
    try:
        full = contained_path(BASE, rel)
        root = os.path.realpath(BASE)
        resolved = os.path.relpath(full, root).replace("\\", "/")
    except (ValueError, OSError):
        return False
    return any(resolved == base or resolved.startswith(base + "/") for base in allowed)


def start_wf(args):
    log = os.path.join(RUNS, "wf-last.log")
    os.makedirs(RUNS, exist_ok=True)
    with open(log, "a", encoding="utf-8") as f:
        subprocess.Popen([PY, WF] + args, cwd=APP_DIR, stdout=f, stderr=f,
                         creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    return log


def node_generation(node):
    return int(node.get('execution_generation') or node.get('generation') or 1)


def build_workspace_payload(state, node_id, generation=None):
    """One immutable viewing identity, not the globally latest execution."""
    current = next((n for n in state.get('nodes', []) if n.get('id') == node_id), None)
    if current is None:
        raise ValueError('该运行没有这个节点')
    versions = list(current.get('generation_history') or []) + [current]
    wanted = int(generation) if generation is not None else node_generation(current)
    original = next((v for v in reversed(versions) if node_generation(v) == wanted), None)
    if original is None:
        raise ValueError('找不到该执行版本')
    node = copy.deepcopy(original)
    rid = state['run_id']
    sources = artifact_sources(state)
    module = sources['modules'].get(str(node.get('module'))) or {}
    graph_node = sources['graph'].get(node_id) or {}
    checklist = node.get('checklist') or graph_node.get('checklist') or module.get('checklist') or []
    try:
        message_page = conversation_module().message_page(rid, node_id, generation=wanted, base=BASE, limit=200)
        messages = message_page['messages']
    except (ImportError, FileNotFoundError):
        messages = []
    source = 'persisted' if messages else 'none'
    if not messages:
        # Old snapshots contain genuine summaries/answered questions, not a full transcript.
        for index, pair in enumerate(node.get('questions') or []):
            for role, name in [('assistant', 'question'), ('user', 'answer')]:
                if pair.get(name):
                    messages.append({'id': 'legacy-%d-%s' % (index, name), 'role':role,
                                     'kind':'legacy_' + name, 'text':str(pair[name]),
                                     'at':pair.get('at'), 'source':'saved_question_answer'})
        report = node.get('agent_report') or {}
        if report.get('summary'):
            messages.append({'id':'legacy-summary', 'role':'assistant', 'kind':'legacy_summary',
                             'text':str(report['summary']), 'at':node.get('finished_at'),
                             'source':'saved_execution_report'})
        question = node.get('question') or report.get('question')
        if question:
            messages.append({'id':'legacy-question', 'role':'assistant', 'kind':'legacy_question',
                             'text':str(question), 'source':'saved_execution_report'})
        if messages:
            source = 'legacy_summary'
    events = []
    log_path = run_path(rid, 'events.jsonl')
    if os.path.isfile(log_path):
        with open(log_path, encoding='utf-8', errors='replace') as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if record.get('node') != node_id:
                    continue
                if record.get('generation') is not None and int(record['generation']) != wanted:
                    continue
                at = str(record.get('ts') or '')
                if record.get('generation') is None and at and node.get('started_at') and at < node['started_at']:
                    continue
                if wanted != node_generation(current) and at and node.get('finished_at') and at > node['finished_at']:
                    continue
                events.append(record)
    card = ''
    task_path = run_path(rid, 'tasks', safe_run_part(node_id) + '.md')
    if os.path.isfile(task_path):
        with open(task_path, encoding='utf-8', errors='replace') as handle:
            card = handle.read(60000)
    rows, missing, counts, declaration = artifact_rows(state, node, sources)
    session = node.get('agent_session_id') or ''
    binding_key = hashlib.sha256(json.dumps([rid, node_id], ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    binding = read_json(os.path.join(RUNS, '_host-bindings', binding_key + '.json'), {})
    if wanted == node_generation(current) and binding.get('cwd') and node.get('workspace') and os.path.normcase(os.path.realpath(binding['cwd'])) == os.path.normcase(os.path.realpath(node['workspace'])):
        session = binding.get('childId') or session
        node['host_parent_session'] = binding.get('parentSessionId') or node.get('host_parent_session')
    identity = {key:node.get(key) for key in ('id','module','name','status','attempts','started_at','finished_at','detail','stale_due_to','review_status','discussion_status')}
    identity.update(generation=wanted, session_id=session, question=node.get('question'), live=node.get('live'))
    executor = graph_node.get('executor') or module.get('executor') or ('agent' if session else '')
    return {'ok':True, 'run':{key:state.get(key) for key in ('run_id','episode','status','agent_mode','created_at','workflow_id','state_revision')},
            'node':identity, 'generation':wanted, 'session_id':session, 'executor':executor,
            'continuity_mode': binding.get('continuityMode') if session == binding.get('childId') else None,
            'continuity_reason': binding.get('continuityReason') if session == binding.get('childId') else None,
            'continuation_of': binding.get('continuationOf') if session == binding.get('childId') else None,
            'host_parent_session': node.get('host_parent_session') or '',
            'generations':[{'generation':node_generation(v), 'status':v.get('status'),
                            'session_id':v.get('agent_session_id'), 'finished_at':v.get('finished_at')} for v in versions],
            'messages':messages, 'message_page': locals().get('message_page', {}), 'conversation_source':source, 'task_card':card,
            'external_changes': workspace_changes(state, node) if wanted == node_generation(current) else [],
            'workspace_hashes': workspace_hashes(state, node) if wanted == node_generation(current) else {},
            'artifact_hashes': {e.get('logical_path') or e['path']:e['sha256'] for e in node.get('artifact_evidence') or [] if e.get('path') and e.get('sha256')},
            'workspace':node.get('workspace'), 'knowledge':{'scope':node.get('knowledge_scope'),
            'sha256':node.get('knowledge_context_sha256'), 'sources':node.get('knowledge_sources') or []},
            'upstream':node.get('upstream_inputs') or [], 'events':events[-100:],
            'declared_inputs':[expand_artifact_path(p,state) for p in graph_node.get('requires',module.get('requires',[]))],
            'artifacts':rows, 'counts':counts, 'evidence_missing':missing, 'declaration':declaration,
            'checks':node.get('checks') or [], 'checklist':checklist,
            'readonly':wanted != node_generation(current), 'current_generation':node_generation(current)}


def workspace_hashes(state, node):
    if not node.get('workspace') or node.get('status') == 'running':
        return {}
    from wf_external import workspace_root
    root = workspace_root(engine_module(), state, node)
    result = {}
    for item in node.get('artifact_evidence') or []:
        logical = item.get('logical_path') or item.get('path')
        if logical:
            current = engine_module()._file_evidence(contained_path(root, logical))
            result[logical] = current['sha256'] if current else None
    return result


def workspace_changes(state, node):
    try:
        hashes = workspace_hashes(state, node)
    except ValueError:
        return ['工作区路径不属于当前模块']
    return [e.get('logical_path') or e['path'] for e in node.get('artifact_evidence') or []
            if hashes and hashes.get(e.get('logical_path') or e['path']) != e.get('sha256')]


def build_results_payload(state):
    artifacts, checks, stale = [], [], []
    sources = artifact_sources(state)
    declarations = []
    for node in state.get('nodes', []):
        definition = dict(sources['modules'].get(node.get('module')) or {}, **(sources['graph'].get(node['id']) or {}))
        for path, policy in (definition.get('deliveries') or {}).items():
            declarations.append(dict(policy, node_id=node['id'], path=expand_artifact_path(path, state)))
    if 'delivery_declarations' in state:
        declarations = state['delivery_declarations']
    for node in state.get('nodes', []):
        rows, _, _, _ = artifact_rows(state, node)
        for row in rows:
            row.update(node_id=node['id'], node_name=node.get('name') or node['id'],
                       generation=node_generation(node), is_primary=False)
            policy = next((d for d in declarations if d['node_id'] == node['id'] and
                           ((row.get('logical_path') or row['path']) == d['path'] or
                            (row.get('logical_path') or row['path']).startswith(d['path'].rstrip('/') + '/'))), {})
            row['delivery_role'] = policy.get('role', 'attachment')
            row['delivery_required'] = policy.get('required', True)
            artifacts.append(row)
        checks.append({'node':node['id'], 'name':node.get('name') or node['id'],
                       'status':node.get('status'), 'checks':node.get('checks') or [], 'detail':node.get('detail')})
        if node.get('status') == 'stale' or node.get('stale_due_to'):
            stale.append(node['id'])
    def rank(row):
        logical = str(row.get('logical_path') or row['path']).lower()
        owner = row['node_id'].lower()
        score = 0
        if row['kind'] == 'video': score += 100
        if 'render' in owner or 'assemble' in owner: score += 100
        if 'deliver' in owner or 'report' in logical: score += 50
        if 'capture' in owner or 'operation' in logical or 'screen-record' in logical: score -= 200
        if row['kind'] in ('text','pdf'): score += 15
        if logical.endswith('.md'): score += 10
        if row['status'] == 'missing': score -= 1000
        return score
    available = [a for a in artifacts if a.get('status') != 'missing']
    if declarations:
        primary = [a for a in artifacts if a['delivery_role'] == 'primary']
    else:
        primary = sorted(available, key=rank, reverse=True)[:1]
    for item in primary: item['is_primary'] = True
    # Same output may be declared by several steps. Show one file with its latest owner.
    deduped = {}
    for item in artifacts:
        if item['path'] not in deduped or item['is_primary']:
            deduped[item['path']] = item
    primary_paths = {a['path'] for a in primary}
    artifacts = list(deduped.values())
    for item in artifacts: item['is_primary'] = item['path'] in primary_paths
    ready = bool(declarations) and bool(primary) and state.get('status') == 'completed' and not stale
    ready = ready and all(n.get('status') in ('passed','skipped') for n in state.get('nodes', []))
    ready = ready and all(a.get('status') == 'verified' for a in artifacts if a['delivery_role'] == 'primary' or (a['delivery_role'] != 'internal' and a['delivery_required']))
    external = [{'node_id': n['id'], 'paths': workspace_changes(state, n)} for n in state.get('nodes', [])]
    external = [row for row in external if row['paths']]
    def review_current(node):
        expected = {e.get('logical_path') or e['path']: e['sha256'] for e in node.get('artifact_evidence') or []}
        return bool(expected) and node.get('review_status') == 'approved' and any(
            record.get('generation') == node_generation(node) and record.get('hashes') == expected and record.get('decision') == 'approved'
            for record in (node.get('reviews') or {}).values())
    reviews = [n['id'] for n in state.get('nodes', []) if n.get('review_status') == 'pending' or
               (n.get('review_status') == 'approved' and not review_current(n))]
    for declaration in declarations:
        if declaration.get('review_required'):
            node = next(n for n in state['nodes'] if n['id'] == declaration['node_id'])
            if not review_current(node) and node['id'] not in reviews:
                reviews.append(node['id'])
    missing_required = [d for d in declarations if d.get('role') != 'internal' and d.get('required', True) and not any(
        a['node_id'] == d['node_id'] and ((a.get('logical_path') or a['path']) == d['path'] or
        (a.get('logical_path') or a['path']).startswith(d['path'].rstrip('/') + '/')) and a['status'] == 'verified'
        for a in artifacts)]
    ready = ready and not external and not reviews and not missing_required and state.get('run_kind') != 'workflow_proposal'
    warnings = (['外部修改待登记：' + '、'.join(row['node_id'] for row in external)] if external else [])
    if reviews: warnings.append('内容审阅待确认：' + '、'.join(reviews))
    if missing_required: warnings.append('必要交付缺失或未验证：' + '、'.join(d['path'] for d in missing_required))
    if not declarations: warnings.append('此运行未声明主成果；当前主成果按旧版规则推断，请核对交付范围')
    if state.get('run_kind') == 'workflow_proposal': warnings.append('这是流程建议；请在项目总览审阅并采用，再检查画布后执行。')
    return {'ok':True, 'run':{k:state.get(k) for k in ('run_id','episode','status','created_at','updated_at','state_revision')},
            'artifacts':artifacts, 'primary':primary, 'checks':checks, 'stale_nodes':stale,
            'deliverable':bool(ready), 'messages':[], 'external_changes': external, 'pending_reviews': reviews,
            'delivery_declarations': declarations, 'missing_required': missing_required, 'primary_source': 'declared' if declarations else 'legacy_inferred', 'warnings': warnings,
            'notice':'可交付表示主成果存在、文件证据通过且执行完成；内容质量仍按对应验收范围判断。' if ready else
                     '存在未完成、过期、缺失或历史证据不足的结果；仍可查看已有文件。'}


def submit_node_command(action, body):
    rid, nid = safe_run_part(body.get('run_id')), safe_run_part(body.get('node'))
    answer = str(body.get('answer') or '').strip()
    if not answer or len(answer) > 20000:
        raise ValueError('请输入 1 到 20000 字的内容')
    request_id = str(body.get('request_id') or '')[:120]
    with LOCK:
        state = read_json(run_path(rid, 'state.json'), {})
        node = next((n for n in state.get('nodes', []) if n.get('id') == nid), None)
        if node is None: raise ValueError('找不到该运行的节点')
        generation = node_generation(node)
        ledger_path = run_path(rid, 'api-commands.json')
        ledger = read_json(ledger_path, {})
        request_id = request_id or hashlib.sha256(json.dumps([action,nid,generation,node.get('attempts'),answer],ensure_ascii=False).encode()).hexdigest()
        fingerprint = hashlib.sha256(json.dumps([action,nid,answer],ensure_ascii=False).encode()).hexdigest()
        if request_id in ledger:
            previous = ledger[request_id]
            if previous.get('fingerprint') != fingerprint: raise ValueError('请求标识已被其他操作使用')
            return previous['response']
        # Queue admission commits before the compatibility response cache. If
        # interrupted between them, return the admitted command before checking
        # a state version which may already have advanced.
        admitted = plugin_module('wf_commands').lookup(BASE, request_id)
        if admitted:
            payload = json.loads(admitted['payload'])
            if admitted['action'] != action or admitted['run_id'] != rid or payload.get('node') != nid or payload.get('answer') != answer:
                raise ValueError('请求标识已被其他操作使用')
            return {'ok': True, 'run_id': rid, 'node': nid, 'status': admitted['status'],
                    'request_id': request_id, 'previous_generation': payload['expected_generation']}
        if body.get('expected_generation') is not None and int(body['expected_generation']) != generation:
            raise ValueError('该节点已有新版本，请刷新后再操作')
        if body.get('expected_state_revision') is not None and int(body['expected_state_revision']) != int(state.get('state_revision', 0)):
            raise ValueError('运行状态已变化，请刷新后再操作')
        if action == 'reply':
            if state.get('status') != 'awaiting_input' or node.get('status') != 'awaiting_input':
                raise ValueError('该节点当前没有等待回答，请刷新状态')
            args = ['canvas-reply','--run',rid,'--node',nid,'--answer',answer]
        else:
            if state.get('status') in ('running','stopping') or state.get('cleanup_pending'):
                raise ValueError('任务仍在执行，请先停止或等待完成，再提交修改')
            module = artifact_sources(state)['modules'].get(str(node.get('module'))) or {}
            if module.get('executor') != 'agent':
                raise ValueError('程序模块按配置执行，请修改输入资料后重跑；文字改稿仅适用于 AI 模块')
            if node.get('discussion_status') == 'running':
                raise ValueError('讨论尚在执行，请先同步会话确认结果')
            args = ['canvas-discuss' if action == 'discuss' else 'canvas-revise','--run',rid,'--node',nid,'--answer',answer,'--request-id',request_id]
            if action == 'discuss':
                args += ['--expected-generation', str(generation)]
        plugin_module('wf_commands').enqueue(BASE, action, {
            'request_id': request_id, 'project_id': body.get('project_id') or state.get('project_id'),
            'run_id': rid, 'node': nid, 'answer': answer, 'expected_generation': generation,
            'expected_state_revision': int(state.get('state_revision', 0))})
        response = {'ok':True,'run_id':rid,'node':nid,'status':'queued','request_id':request_id,
                    'previous_generation':generation}
        ledger[request_id] = {'fingerprint':fingerprint,'response':response}
        write_json(ledger_path, ledger)
        return response


def run_json_capture(args, timeout=120, allow_code=(0,)):
    try:
        r = subprocess.run([PY] + args, cwd=APP_DIR, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        out = r.stdout.strip()
        data = json.loads(out) if out.startswith(("{", "[")) else {"raw": out[:800]}
        return data, r.returncode
    except Exception as exc:
        return {"error": str(exc)}, 1


def wf_health():
    run = latest_run_state()
    bili, _ = run_json_capture(["bin/bili.exe", "auth", "status"], 60)
    logged = bool((bili.get("data") or {}).get("logged_in"))
    episode = run.get("episode") if run else None
    publish = None
    if episode:
        pre, _ = run_json_capture(["wf.py", "rehearse", episode], 120)
        publish = pre
    return {
        "run": {"id": run.get("run_id"), "status": run.get("status"), "episode": episode} if run else None,
        "bili": {"logged_in": logged,
                 "user": (bili.get("data") or {}).get("user", {}).get("name", ""),
                 "error": (bili.get("error") or {}).get("message", "")},
        "publish": {"ok": publish.get("ok") if publish else None,
                    "problems": publish.get("problems", []) if publish else [],
                    "command_file": publish.get("command_file") if publish else None},
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        text = re.sub(r'(media_token=)[^&\s\"]+', r'\1[REDACTED]', fmt % args)
        sys.stdout.write("[canvas-editor] %s\n" % text)
        sys.stdout.flush()

    def cors(self):
        origin = self.headers.get("Origin")
        if origin and trusted_origin(origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Canvas-Api-Token")

    def send_json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_html(self):
        try:
            body = open(CANVAS, encoding="utf-8").read().encode("utf-8")
        except Exception as exc:
            self.send_json(500, {"ok": False, "error": str(exc)})
            return
        self.send_response(200)
        self.cors()
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_download_json(self, value, filename):
        body = json.dumps(value, ensure_ascii=False, indent=2).encode('utf-8')
        self.send_response(200)
        self.cors()
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Disposition', 'attachment; filename="' + filename + '"')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        if self.command != 'HEAD':self.wfile.write(body)

    def send_file(self, full, rel, download=False):
        """Serve one artifact with byte-range support so video/audio can seek."""
        size = os.path.getsize(full)
        mime = MIME_BY_EXT.get(os.path.splitext(full)[1].lower(), "application/octet-stream")
        start, end, status = 0, size - 1, 200
        header = self.headers.get("Range") or ""
        if header.startswith("bytes=") and size > 0:
            piece = header[6:].split(",")[0].strip()
            first, _, last = piece.partition("-")
            try:
                if first:
                    start = int(first)
                    end = int(last) if last else size - 1
                elif last:
                    start = max(0, size - int(last))
                if start >= size or end < start:
                    self.send_response(416)
                    self.cors()
                    self.send_header("Content-Range", "bytes */" + str(size))
                    self.end_headers()
                    return
                end = min(end, size - 1)
                status = 206
            except ValueError:
                start, end, status = 0, size - 1, 200
        length = end - start + 1 if size else 0
        self.send_response(status)
        self.cors()
        self.send_header("Content-Type", mime)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-store")
        if download:
            ext = os.path.splitext(rel)[1].lower()
            name = os.path.basename(rel).replace('"', '').replace('\r', '').replace('\n', '')
            fallback = "artifact" + (ext if re.fullmatch(r"\.[a-z0-9]{1,5}", ext) else ".bin")
            self.send_header("Content-Disposition", 'attachment; filename="' + (name if name.isascii() else fallback) + '"; filename*=UTF-8\'\'' + quote(name))
        if status == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.send_header("Content-Length", str(length))
        self.end_headers()
        if self.command == "HEAD" or not length:
            return
        with open(full, "rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining > 0:
                chunk = handle.read(min(256 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def do_OPTIONS(self):
        if not trusted_origin(self.headers.get("Origin")):
            return self.send_json(403, {"ok": False, "error": "不接受来自外部网页的请求"})
        self.send_response(204)
        self.cors()
        self.end_headers()

    def do_GET(self):
        if not trusted_origin(self.headers.get("Origin")):
            return self.send_json(403, {"ok": False, "error": "不接受来自外部网页的请求"})
        if urlsplit(self.path).path != '/api/health' and not (
                hmac.compare_digest(str(self.headers.get('X-Canvas-Api-Token') or ''), API_TOKEN)
                or self.command in {'GET', 'HEAD'} and authorized_media(self.path)):
            return self.send_json(403, {'ok': False, 'error': '请通过Harness画布访问此实例'})
        try:
            return self.get_request()
        except (ValueError, OSError) as exc:
            return self.send_json(400, {"ok": False, "error": str(exc)})

    def do_HEAD(self):
        return self.do_GET()

    def get_request(self):
        path = self.path.split("?")[0]
        if path in ("/", "/canvas", "/workflow-canvas"):
            return self.send_html()
        q = query_params(self.path)
        if path.startswith('/api/wf/') and q.get('project_id') and q.get('run_id'):
            if projects_store().owner(q['run_id']) != q['project_id']:
                return self.send_json(409, {'ok': False, 'error': '运行不属于当前项目'})
        if path == '/api/wf/results' and q.get('project_id') and not q.get('run_id'):
            store = projects_store(); project = store.get(q['project_id'])
            runs = store.runs(q['project_id'])
            chosen = next((r for r in runs if r['run_id'] == project.get('delivery_run_id')), None) or store.current_run(q['project_id'])
            return self.send_json(200, build_results_payload(chosen) if chosen and not chosen.get('admission_only') else {'ok': True, 'run': chosen,
                'artifacts': [], 'primary': [], 'checks': [], 'stale_nodes': [], 'deliverable': False})
        if path == "/api/projects":
            store = projects_store()
            store.migrate()
            return self.send_json(200, {"ok": True, "projects": store.list()})
        if path == '/api/environment':
            return self.send_json(200, dict(plugin_module('wf_environment').inspect(BASE), runtime={
                'version': API_VERSION, 'build_id': BUILD_INFO.get('build_id'), 'schema': 2,
                'source_directory': os.path.realpath(APP_DIR), 'data_directory': os.path.realpath(BASE)}))
        if path == '/api/projects/export':
            package = plugin_module('wf_sharing').export_project(BASE, projects_store(), engine_module(),
                q.get('project_id'), materials=q.get('materials') == '1')
            return self.send_download_json(package, 'workflow-canvas-project.json')
        if path == '/api/diagnostics':
            data = plugin_module('wf_diagnostics').summary(BASE, dict(BUILD_INFO, version=API_VERSION))
            return self.send_download_json(data, 'workflow-canvas-diagnostics.json')
        if path == "/api/projects/overview":
            store = projects_store()
            payload = plugin_module('wf_overview').overview(store, q.get('project_id'), build_results_payload)
            if payload['current_run']:
                payload['current_run'] = annotate_run(payload['current_run'])
            payload['commands'] = [{k:v for k,v in command.items() if k != 'payload'} for command in
                plugin_module('wf_commands').pending(BASE, engine_module()._pid_alive, payload['project']['project_id'])]
            return self.send_json(200, payload)
        if path == '/api/projects/proposal':
            return self.send_json(200, plugin_module('wf_planning').proposal(BASE, projects_store(), engine_module(), q.get('project_id'), safe_run_part(q.get('run_id'))))
        if path == "/api/health":
            authorized = hmac.compare_digest(str(self.headers.get('X-Canvas-Api-Token') or ''), API_TOKEN)
            return self.send_json(200, {"ok": True, "service": "canvas-editor", "port": self.server.server_address[1], "version": API_VERSION,
                **({"data_home": os.path.realpath(BASE), "server_file": os.path.realpath(__file__), "pid": os.getpid()} if authorized else {}),
                "build_id": BUILD_INFO.get('build_id'), "schema": 2,
                "task_scope": "shared+current_task", "agent_mode": "auto"})
        if path == "/api/workflow":
            data = read_json(WORKFLOW, {"stages": []})
            return self.send_json(200, {"ok": True, "workflow": data, "path": WORKFLOW})
        if path == "/api/layout":
            return self.send_json(200, {"ok": True, "layout": read_json(LAYOUT, {})})
        if path == "/api/modules":
            return self.send_json(200, read_json(os.path.join(BASE, "kb", "modules.json"), {"modules": []}))
        if path == "/api/modules/history":
            q = query_params(self.path)
            mid = safe_run_part(q.get("id", ""))
            root = module_history_dir(mid)
            rows = []
            for name in sorted(os.listdir(root), reverse=True):
                if not re.fullmatch(r"v[0-9]{4}\.json", name):
                    continue
                item = read_json(os.path.join(root, name), {})
                rows.append({"version": int(item.get("version") or 0), "name": item.get("name"),
                             "saved_at": item.get("_saved_at") or None, "file": name})
            return self.send_json(200, {"ok": True, "id": mid, "versions": rows})

        if path == "/api/skills":
            registry = read_json(os.path.join(BASE, "kb", "skill-registry.json"), {"skills": []})
            rows = []
            for skill in registry.get("skills", []):
                row = {key: skill.get(key) for key in ("id", "name", "source_url", "commit", "license", "requirements")}
                row["installed"] = os.path.isfile(os.path.join(BASE, skill.get("root", ""), skill.get("entry", "SKILL.md")))
                rows.append(row)
            tools = read_json(os.path.join(BASE, "kb", "execution-tools.json"), {})
            return self.send_json(200, {"ok": True, "skills": rows,
                "tools": {key: os.path.isfile(tools.get(key, "")) for key in ("python", "ffmpeg", "ffprobe", "node", "chrome", "remotion_helper")}})
        if path == "/api/canvas/export":
            return self.send_json(200, {"ok": True, "graph": read_json(os.path.join(BASE, "kb", "canvas-workflow.json"), {})})
        if path == "/api/canvas/templates":
            return self.send_json(200, read_json(os.path.join(BASE, "kb", "canvas-templates.json"), {"templates": []}))
        if path == "/api/canvas/revisions":
            return self.send_json(200, {"ok": True, "revisions": list_revisions()})
        if path == "/api/workflows":
            sync_active_workflow_from_mirror()
            index = load_workflow_index()
            active = index.get("active", "")
            return self.send_json(200, {"ok": True, "active": active, "workflows": index.get("workflows", [])})
        if path == "/api/workflows/export":
            q = query_params(self.path)
            index = load_workflow_index()
            wid = workflow_id(q.get("id") or index.get("active"))
            data = read_json(workflow_file(wid), None)
            if not isinstance(data, dict):
                return self.send_json(404, {"ok": False, "error": "工作流不存在"})
            portable = {
                "schema": "dsh.workflow/v1",
                "name": data.get("name") or wid,
                "description": data.get("description") or "",
                "tier": data.get("tier") or "experimental",
                "graph": data.get("graph") or {"nodes": [], "edges": []},
                "dependencies": workflow_dependencies(data.get("graph") or {}),
                "exported_at": datetime.now().isoformat(timespec="seconds"),
                "source": "workflow-canvas-sidebar",
            }
            return self.send_json(200, {"ok": True, "workflow": portable})
        if path == "/api/canvas":
            ensure_workflows()
            sync_active_workflow_from_mirror()
            index = load_workflow_index()
            graph = read_json(os.path.join(BASE, "kb", "canvas-workflow.json"), {"nodes": [], "edges": []})
            active = next((w for w in index.get("workflows", []) if w.get("id") == index.get("active")), None)
            return self.send_json(200, {"ok": True, "graph": graph,
                                        "revision": canvas_revision(graph),
                                        "workflow": {"id": index.get("active", ""),
                                                     "name": (active or {}).get("name", index.get("active", "")),
                                                     "description": (active or {}).get("description", "")}})
        if path == "/api/kb/tasks":
            return self.send_json(200, {"ok": True, "tasks": knowledge_module().list_tasks()})
        if path == '/api/kb/revision':
            task, rel = q.get('task_id') or None, str(q.get('path') or '')
            if not rel.startswith('shared/') and not task:
                raise ValueError('需要当前项目分区')
            knowledge_module().safe_rel(rel, task=task)
            versions = plugin_module('wf_knowledge').history(BASE, rel)
            version = next((v for v in versions if v['revision'] == int(q.get('revision') or 0)), None)
            if not version:
                raise ValueError('知识版本不存在')
            original = contained_path(BASE, version['source_path'])
            with open(original, 'rb') as handle:
                if hashlib.file_digest(handle, 'sha256').hexdigest() != version['sha256']:
                    raise ValueError('知识历史版本证据已变化')
            return self.send_file(original, os.path.basename(rel), download=True)
        if path in ("/api/kb/list", "/api/kb/read", "/api/kb/search", "/api/kb/raw"):
            q = query_params(self.path)
            try:
                task = str(q.get('task_id') or '').strip()
                kb = knowledge_module()
                if path in ("/api/kb/read", "/api/kb/raw"):
                    rel = str(q.get('path') or '')
                    if not rel.startswith('shared/') and not task:
                        raise ValueError('请选择任务分区')
                    full = kb.safe_rel(rel) if rel.startswith('shared/') else kb.safe_rel(rel, task=task)
                    if not os.path.isfile(full):
                        return self.send_json(404, {'ok':False,'error':'知识文件不存在'})
                    if path.endswith('/raw'):
                        return self.send_file(full, rel, download=q.get('download') == '1')
                    info = kb.read_entry_content(rel, task=task or None)
                    info['revisions'] = plugin_module('wf_knowledge').history(BASE, rel)
                    if info.get('kind') == 'image' and os.path.getsize(full) <= 20 * 1024 * 1024:
                        with open(full, 'rb') as handle:
                            info['data'] = base64.b64encode(handle.read()).decode('ascii')
                    from urllib.parse import quote
                    info.update(ok=True, task_id=task,
                                url='/api/kb/raw?task_id=' + quote(task, safe='') + '&path=' + quote(rel, safe=''))
                    return self.send_json(200, info)
                needle = q.get('q') if path.endswith('search') else None
                entries = kb.list_entries(scope='shared', search=needle)
                if task:
                    entries += kb.list_entries(task=task, search=needle)
                return self.send_json(200, {'ok':True, 'task_id':task, 'entries':entries,
                                            'index':{'entries':entries}})
            except (ValueError, OSError) as exc:
                return self.send_json(400, {'ok':False,'error':str(exc)})
        if path == "/api/wf/run-state":
            q = query_params(self.path)
            rid = q.get("run_id", "")
            st = read_json(run_path(rid, "state.json"), None)
            return self.send_json(200, {"ok": bool(st), "run": annotate_run(st) if st else None,
                                        "graph": read_json(run_path(rid, 'graph.json'), {}) if st else {}})
        if path == "/api/wf/messages":
            q = query_params(self.path)
            rid = safe_run_part(q.get('run_id'))
            if q.get('project_id') and projects_store().owner(rid) != q['project_id']:
                return self.send_json(409, {'ok': False, 'error': '运行不属于当前项目'})
            return self.send_json(200, {'ok': True, **conversation_module().message_page(rid, safe_run_part(q.get('node')),
                generation=q.get('generation') or None, base=BASE, before_seq=q.get('before_seq'),
                after_seq=q.get('after_seq'), limit=q.get('limit') or 100)})
        if path == '/api/wf/diff':
            import difflib
            rid, nid = safe_run_part(q.get('run_id')), safe_run_part(q.get('node'))
            state = read_json(run_path(rid, 'state.json'), {})
            logical = str(q.get('path') or '')
            selected = []
            for generation in (int(q.get('from_generation') or 0), int(q.get('to_generation') or 0)):
                rows = build_workspace_payload(state, nid, generation)['artifacts']
                artifact = next((a for a in rows if (a.get('logical_path') or a['path']) == logical and a['kind'] == 'text' and a['status'] == 'verified'), None)
                if not artifact or artifact['bytes'] > 2 * 1024 * 1024: raise ValueError('该版本没有可比较的已验证文本成果（上限2MB）')
                with open(contained_path(BASE, artifact['path']), encoding='utf-8-sig') as handle:
                    selected.append(handle.read().splitlines(keepends=True))
            difference = ''.join(difflib.unified_diff(selected[0], selected[1], fromfile='第' + q['from_generation'] + '代/' + logical, tofile='第' + q['to_generation'] + '代/' + logical))
            return self.send_json(200, {'ok':True, 'kind':'text', 'path':logical + '.diff', 'text':difference[:200000] or '两个版本文本相同', 'truncated':len(difference) > 200000})
        if path == "/api/wf/workspace":
            q = query_params(self.path)
            state = read_json(run_path(q.get('run_id'), 'state.json'), {})
            if not state:
                return self.send_json(404, {'ok':False, 'error':'找不到该运行'})
            payload = build_workspace_payload(state, safe_run_part(q.get('node')), q.get('generation') or None)
            return self.send_json(200, payload)
        if path == '/api/wf/export':
            q = query_params(self.path)
            rid = safe_run_part(q.get('run_id'))
            if q.get('project_id') and projects_store().owner(rid) != q['project_id']:
                return self.send_json(409, {'ok': False, 'error': '运行不属于当前项目'})
            state = read_json(run_path(rid, 'state.json'), {})
            bundle = plugin_module('wf_delivery').export_bundle(BASE, state, build_results_payload(state))
            return self.send_file(str(bundle), rid + '-delivery.zip', download=True)
        if path == "/api/wf/results":
            q = query_params(self.path)
            rid = q.get('run_id')
            state = read_json(run_path(rid, 'state.json'), {}) if rid else latest_run_state()
            if not state:
                return self.send_json(200, {'ok':True, 'run':None, 'artifacts':[], 'primary':[], 'checks':[], 'stale_nodes':[], 'deliverable':False})
            return self.send_json(200, build_results_payload(state))
        if path == "/api/wf/task":
            q = query_params(self.path)
            rid, nid = q.get("run_id", ""), q.get("node", "")
            task_path = run_path(rid, "tasks", safe_run_part(nid) + ".md")
            content = open(task_path, encoding="utf-8").read() if os.path.exists(task_path) else ""
            return self.send_json(200, {"ok": True, "content": content, "path": task_path})
        if path == "/api/wf/artifacts":
            q = query_params(self.path)
            rid, nid = q.get("run_id", ""), q.get("node", "")
            st = read_json(run_path(rid, "state.json"), {})
            node = next((n for n in st.get("nodes", []) if n.get("id") == nid), None)
            if not node:
                return self.send_json(404, {"ok": False, "error": "节点不存在，或该运行没有这个节点"})
            rows, missing, counts, declaration = artifact_rows(st, node)
            return self.send_json(200, {"ok": True, "run_id": rid, "node": nid,
                                        "artifacts": rows, "evidence_missing": missing,
                                        "counts": counts, "declaration": declaration,
                                        "warning": "" if declaration != "unknown" else "历史声明无法确认，未采用当前模块库"})
        if path == "/api/wf/artifact":
            q = query_params(self.path)
            rid, rel = q.get("run_id", ""), as_rel(q.get("path", ""))
            st = read_json(run_path(rid, "state.json"), {})
            if not artifact_allowed(rel, allowed_artifact_paths(st)):
                return self.send_json(403, {"ok": False, "error": "产物不在该运行的声明范围内"})
            try:
                full = contained_path(BASE, rel)
            except ValueError:
                return self.send_json(400, {"ok": False, "error": "路径不合法"})
            if not os.path.isfile(full):
                return self.send_json(404, {"ok": False, "error": "文件不存在"})
            from urllib.parse import quote
            size = os.path.getsize(full)
            kind = artifact_kind(rel)
            mime = MIME_BY_EXT.get(os.path.splitext(full)[1].lower(), "application/octet-stream")
            base_url = "/api/wf/raw?run_id=" + quote(rid, safe="") + "&path=" + quote(rel, safe="")
            payload = {"ok": True, "path": rel, "kind": kind, "bytes": size, "mime": mime,
                       "url": base_url, "download": base_url + "&download=1"}
            if kind == "image" and size <= 20 * 1024 * 1024:
                with open(full, "rb") as handle:
                    payload["data"] = base64.b64encode(handle.read()).decode("ascii")
            elif kind == "text" and size <= 2 * 1024 * 1024:
                with open(full, encoding="utf-8", errors="replace") as handle:
                    payload["text"] = handle.read(200000)
                payload["truncated"] = size > 200000
            return self.send_json(200, payload)
        if path == "/api/wf/raw":
            q = query_params(self.path)
            rid, rel = q.get("run_id", ""), q.get("path", "")
            st = read_json(run_path(rid, "state.json"), {})
            if not artifact_allowed(rel, allowed_artifact_paths(st)):
                return self.send_json(403, {"ok": False, "error": "产物不在该运行的声明范围内"})
            full = contained_path(BASE, rel)
            if not os.path.isfile(full):
                return self.send_json(404, {"ok": False, "error": "文件不存在"})
            return self.send_file(full, as_rel(rel), download=q.get("download") == "1")
        if path == "/api/wf/status" and query_params(self.path).get('project_id'):
            current = projects_store().current_run(query_params(self.path)['project_id'])
            return self.send_json(200, {'ok': True, 'run': annotate_run(current) if current else None})
        if path == "/api/wf/history" and query_params(self.path).get('project_id'):
            runs = projects_store().runs(query_params(self.path)['project_id'])
            return self.send_json(200, {'ok': True, 'runs': list(reversed(runs[:30]))})
        if path == "/api/wf/history":
            rows = []
            runs_dir = os.path.join(BASE, ".runs")
            os.makedirs(runs_dir, exist_ok=True)
            for rid in sorted(os.listdir(runs_dir)):
                if rid.startswith("_") or not os.path.isdir(os.path.join(runs_dir, rid)):
                    continue
                try:
                    st = read_json(run_path(rid, "state.json"), {})
                except ValueError:
                    continue
                if not st.get('run_id') or not isinstance(st.get('nodes'), list):
                    continue
                rows.append({"run_id": rid, "episode": st.get("episode"), "status": st.get("status"),
                             "mode": st.get("mode", "linear"), "created_at": st.get("created_at")})
            return self.send_json(200, {"ok": True, "runs": rows[-30:]})
        if path == "/api/wf/logs":
            q = query_params(self.path)
            rid = q.get("run_id") or ""
            node = q.get("node") or ""
            if not rid:
                rid = ""
                try:
                    rid = open(os.path.join(BASE, ".runs", "LATEST"), encoding="utf-8").read().strip()
                except Exception:
                    pass
            if node:
                node = safe_run_part(node)
            if not rid:
                return self.send_json(200, {"ok": True, "events": []})
            path_log = run_path(rid, "events.jsonl")
            rows = []
            if os.path.exists(path_log):
                for line in open(path_log, encoding="utf-8", errors="replace"):
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    if node and rec.get("node") != node:
                        continue
                    rows.append(rec)
            return self.send_json(200, {"ok": True, "run_id": rid, "node": node, "events": rows[-80:]})
        if path == "/api/wf/health":
            return self.send_json(200, {"ok": True, **wf_health()})
        if path == "/api/publish/check":
            q = query_params(self.path)
            episode = q.get("episode") or "day1"
            data, code = run_json_capture(["wf.py", "rehearse", episode], 120)
            return self.send_json(200 if code == 0 else 200, {"ok": code == 0, "data": data})
        if path == "/api/wf/status":
            return self.send_json(200, {"ok": True, "run": annotate_run(latest_run_state())})
        if path == "/api/gates":
            gates = known_gates()
            return self.send_json(200, {"ok": True, "gates": [{"id": k, "label": v} for k, v in gates.items()]})
        if path == "/api/library":
            return self.send_json(200, {"ok": True, "library": read_json(LIBRARY, {"blocks": []}), "path": LIBRARY})
        if path == "/api/status":
            try:
                r = subprocess.run([PY, "workflow.py", "status", "--json"], cwd=APP_DIR,
                                   capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
                return self.send_json(200, {"ok": True, "status": json.loads(r.stdout)})
            except Exception as exc:
                return self.send_json(500, {"ok": False, "error": str(exc)})
        self.send_json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if not trusted_origin(self.headers.get("Origin")):
            return self.send_json(403, {"ok": False, "error": "不接受来自外部网页的请求"})
        if not hmac.compare_digest(str(self.headers.get('X-Canvas-Api-Token') or ''), API_TOKEN):
            return self.send_json(403, {'ok': False, 'error': '请通过Harness画布访问此实例'})
        try:
            return self.post_request()
        except (ValueError, OSError) as exc:
            return self.send_json(400, {"ok": False, "error": str(exc)})

    def post_request(self):
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            return self.send_json(400, {"ok": False, "error": "bad json: " + str(exc)})

        if not isinstance(body, dict):
            raise ValueError("请求内容必须为对象")
        if path == '/api/environment':
            return self.send_json(200, dict(plugin_module('wf_environment').configure(BASE, body), runtime={
                'version': API_VERSION, 'build_id': BUILD_INFO.get('build_id'), 'schema': 2,
                'source_directory': os.path.realpath(APP_DIR), 'data_directory': os.path.realpath(BASE)}))
        if path == '/api/projects/import':
            with LOCK:
                result = plugin_module('wf_sharing').import_project(BASE, projects_store(), engine_module(), body)
                knowledge_module().ensure_task(result['project']['project_id'])
            return self.send_json(200, result)
        if path == '/api/projects/suggest':
            command, fresh = plugin_module('wf_planning').suggest(projects_store(), engine_module(), body)
            return self.send_json(202, {k: v for k, v in command.items() if k != 'project'})
        if path == '/api/projects/apply-proposal':
            return self.send_json(200, plugin_module('wf_planning').apply(BASE, projects_store(), engine_module(), body))
        if path == '/api/wf/deliveries':
            rid = safe_run_part(body.get('run_id'))
            if body.get('project_id') and projects_store().owner(rid) != body['project_id']:
                return self.send_json(409, {'ok': False, 'error': '运行不属于当前项目'})
            return self.send_json(200, plugin_module('wf_delivery').configure(engine_module(), rid, body))
        if path in {'/api/wf/adopt-changes', '/api/wf/review'}:
            rid, nid = safe_run_part(body.get('run_id')), safe_run_part(body.get('node'))
            if body.get('project_id') and projects_store().owner(rid) != body['project_id']:
                return self.send_json(409, {'ok': False, 'error': '运行不属于当前项目'})
            request_id = str(body.get('request_id') or '')
            if not request_id or len(request_id) > 160:
                raise ValueError('缺少命令标识')
            engine = engine_module()
            external = plugin_module('wf_external')
            state = read_json(run_path(rid, 'state.json'), {})
            node = next((n for n in state.get('nodes', []) if n['id'] == nid), {})
            if node.get('agent_session_id') or node.get('continuation_of'):
                session = host_session_snapshot(rid, nid)
                if session.get('executing'):
                    return self.send_json(409, {'ok': False, 'error': '原生会话仍在执行，请等待完成后同步并审阅'})
            if path.endswith('adopt-changes'):
                result = external.adopt(engine, rid, nid, body.get('expected_generation'), body.get('hashes'), request_id)
            else:
                result = external.review(engine, rid, nid, body.get('expected_generation'), body.get('hashes'), request_id)
            return self.send_json(200, {'ok': True, 'run_id': rid, 'node': nid, **result})
        if path == '/api/wf/sync-session':
            rid, nid = safe_run_part(body.get('run_id')), safe_run_part(body.get('node'))
            state = read_json(run_path(rid, 'state.json'), {})
            node = next((n for n in state.get('nodes', []) if n['id'] == nid), None)
            if not node:
                raise ValueError('模块不存在')
            store = projects_store()
            if body.get('project_id') and store.owner(rid) != body['project_id']:
                return self.send_json(409, {'ok': False, 'error': '运行不属于当前项目'})
            if body.get('expected_generation') != node_generation(node):
                return self.send_json(409, {'ok': False, 'error': '模块版本已变化，请刷新后同步'})
            data = host_session_snapshot(rid, nid)
            binding = data['binding']
            sid = binding['childId']
            cursor = store.session_cursor(rid, nid, sid)
            imported = 0
            for event in data.get('events', []):
                if event['seq'] <= cursor:
                    continue
                kind = event['type']
                text = event.get('text') or ('工具：' + event['tool'] if event.get('tool') else '')
                # Engine task envelopes already exist in the workspace; preserve
                # native user messages and real public assistant/tool output.
                if not text or (kind == 'user/message' and text.startswith('You are DeepSeek executing ONE node')):
                    continue
                conversation_module().append_message(rid, nid, node_generation(node),
                    'user' if kind == 'user/message' else 'assistant', text,
                    kind='tool_summary' if kind.startswith('tool/') else 'message', session_id=sid,
                    source='host_public', key='host:' + sid + ':' + str(event['seq']), base=BASE)
                imported += 1
            store.session_cursor(rid, nid, sid, data['cursor'])
            payload = build_workspace_payload(state, nid)
            changed = payload.get('external_changes', []) + [a['path'] for a in payload['artifacts'] if a.get('status') in {'changed', 'missing'}]
            return self.send_json(200, {'ok': True, 'imported': imported, 'cursor': data['cursor'],
                'continuity_mode': binding.get('continuityMode'), 'executing': data.get('executing'), 'external_changes': changed,
                'hint': '发现成果变化，必须检查并创建修订后才能交付' if changed else '公开消息与文件证据已同步'})
        if path in {'/api/projects/create', '/api/projects/update', '/api/projects/archive',
                    '/api/projects/set-work-run', '/api/projects/set-delivery-run', '/api/projects/supersede-run'}:
            try:
                if path.endswith('/set-delivery-run'):
                    state = read_json(run_path(safe_run_part(body.get('run_id')), 'state.json'), {})
                    if not build_results_payload(state).get('deliverable'):
                        raise ValueError('该运行尚未通过完整交付检查，请补齐必要成果与审阅')
                result = projects_store().mutate(path.rsplit('/', 1)[1], body)
                if path.endswith('/create'):
                    knowledge_module().ensure_task(result['project']['project_id'])
                return self.send_json(200, result)
            except projects_store_conflict() as exc:
                return self.send_json(409, {'ok': False, 'error': str(exc)})
        if path.startswith('/api/wf/') and body.get('project_id') and body.get('run_id'):
            if projects_store().owner(body['run_id']) != body['project_id']:
                return self.send_json(409, {'ok': False, 'error': '运行不属于当前项目，请刷新后操作'})
        if path == "/api/modules/create":
            name = str(body.get("name") or "").strip()
            instructions = str(body.get("instructions") or "").strip()
            checks = body.get("checklist", [])
            if not name or len(name) > 80 or not instructions or len(instructions) > 20000:
                raise ValueError("请填写模块名称（最多80字）和任务说明（最多20000字）")
            if not isinstance(checks, list) or len(checks) > 30 or any(not isinstance(c, str) or not c.strip() or len(c) > 500 for c in checks):
                raise ValueError("验收要求应为最多30条文本，每条不超过500字")
            def module_paths(value, label):
                if value is None:
                    return []
                if not isinstance(value, list) or len(value) > 30:
                    raise ValueError(label + "应为最多30个相对路径")
                paths = []
                for item in value:
                    text = str(item or "").strip().replace("\\", "/")
                    if not text or len(text) > 300 or text.startswith("/") or ".." in text.split("/"):
                        raise ValueError(label + "只能使用工作区相对路径：" + repr(item))
                    if text not in paths:
                        paths.append(text)
                return paths
            outputs = module_paths(body.get("artifacts", body.get("outputs")), "产物路径")
            inputs = module_paths(body.get("requires", body.get("inputs")), "输入路径")
            try:
                timeout_seconds = int(body.get("timeout_seconds") or 0) or None
            except (TypeError, ValueError):
                raise ValueError("超时必须是整数秒")
            if timeout_seconds is not None and not 30 <= timeout_seconds <= 7200:
                raise ValueError("超时应在 30–7200 秒之间")
            mid = "custom-" + uuid.uuid4().hex[:12]
            module = {"id": mid, "name": name, "category": "自定义", "executor": "agent", "kind": "agent",
                      "version": 1, "description": instructions[:120], "agent_prompt": instructions,
                      "requires": inputs, "artifacts": outputs or ["episodes/{stem}-" + mid + ".md"],
                      "timeout_seconds": timeout_seconds, "retry": 0,
                      "checklist": checks or ["已按任务说明完成，并保存结果文档"]}
            with LOCK:
                data = read_json(os.path.join(BASE, "kb", "modules.json"), {"modules": []})
                data.setdefault("modules", []).append(module)
                save_modules(data)
            return self.send_json(200, {"ok": True, "module": module, "modules": data["modules"]})
        if path == "/api/modules/update":
            mid = safe_run_part(body.get("id"))
            name = str(body.get("name") or "").strip()
            instructions = str(body.get("instructions") or "").strip()
            checks = body.get("checklist", [])
            if not name or len(name) > 80 or not instructions or len(instructions) > 20000:
                raise ValueError("请填写模块名称（最多80字）和任务说明（最多20000字）")
            if not isinstance(checks, list) or len(checks) > 30 or any(not isinstance(c, str) or not c.strip() or len(c) > 500 for c in checks):
                raise ValueError("验收要求应为最多30条文本，每条不超过500字")
            def update_paths(value, label):
                if value is None:
                    return []
                if not isinstance(value, list) or len(value) > 30:
                    raise ValueError(label + "应为最多30个相对路径")
                paths = []
                for item in value:
                    text = str(item or "").strip().replace("\\", "/")
                    if not text or len(text) > 300 or text.startswith("/") or ".." in text.split("/"):
                        raise ValueError(label + "只能使用工作区相对路径")
                    if text not in paths:
                        paths.append(text)
                return paths
            outputs = update_paths(body.get("artifacts", body.get("outputs")), "产物路径")
            inputs = update_paths(body.get("requires", body.get("inputs")), "输入路径")
            try:
                timeout_seconds = int(body.get("timeout_seconds") or 0) or None
            except (TypeError, ValueError):
                raise ValueError("超时必须是整数秒")
            if timeout_seconds is not None and not 30 <= timeout_seconds <= 7200:
                raise ValueError("超时应在 30–7200 秒之间")
            with LOCK:
                data = read_json(os.path.join(BASE, "kb", "modules.json"), {"modules": []})
                module = next((m for m in data["modules"] if m.get("id") == mid), None)
                if module is None:
                    raise ValueError("模块不存在")
                if module.get("executor") not in (None, "agent"):
                    raise ValueError("只有 agent 模块可以在线编辑")
                snapshot_module(module)
                module.update({"name": name, "description": instructions[:120], "agent_prompt": instructions,
                               "requires": inputs, "artifacts": outputs or module.get("artifacts") or [],
                               "timeout_seconds": timeout_seconds, "version": int(module.get("version") or 1) + 1,
                               "checklist": checks or ["已按任务说明完成，并保存结果文档"]})
                save_modules(data)
                snapshot_module(module)
            return self.send_json(200, {"ok": True, "module": module, "modules": data["modules"]})
        if path == "/api/modules/rollback":
            mid = safe_run_part(body.get("id"))
            version = int(body.get("version") or 0)
            with LOCK:
                data = read_json(os.path.join(BASE, "kb", "modules.json"), {"modules": []})
                module = next((m for m in data["modules"] if m.get("id") == mid), None)
                if module is None:
                    raise ValueError("模块不存在")
                snapshot = read_json(os.path.join(module_history_dir(mid), "v%04d.json" % version), None)
                if not isinstance(snapshot, dict):
                    raise ValueError("版本不存在")
                snapshot_module(module)
                old_version = int(module.get("version") or 1)
                module.update(snapshot)
                module["version"] = old_version + 1
                save_modules(data)
                snapshot_module(module)
            return self.send_json(200, {"ok": True, "module": module, "modules": data["modules"]})
        if path == "/api/modules/delete":
            mid = safe_run_part(body.get("id"))
            with LOCK:
                data = read_json(os.path.join(BASE, "kb", "modules.json"), {"modules": []})
                module = next((m for m in data["modules"] if m.get("id") == mid), None)
                if module is None:
                    raise ValueError("模块不存在")
                # Keep definitions for existing canvas nodes, templates, and historical runs.
                module["archived"] = True
                save_modules(data)
            return self.send_json(200, {"ok": True, "modules": data["modules"]})
        if path in {"/api/wf/reply", "/api/wf/check", "/api/wf/agent-skip",
                    "/api/wf/agent-done", "/api/wf/rerun", "/api/wf/approve", "/api/wf/reject"}:
            body["run_id"] = safe_run_part(body.get("run_id"))
            run_path(body["run_id"], "state.json")
            if path in {"/api/wf/reply", "/api/wf/check", "/api/wf/agent-skip", "/api/wf/agent-done"}:
                body["node"] = safe_run_part(body.get("node"))
            if path == "/api/wf/rerun":
                body["from"] = safe_run_part(body.get("from"))

        if path == "/api/workflow":
            wf = body.get("workflow", body)
            if not isinstance(wf, dict) or not isinstance(wf.get("stages"), list) or not wf["stages"]:
                return self.send_json(400, {"ok": False, "error": "workflow.stages 必须是非空数组"})
            ids = set()
            for i, st in enumerate(wf["stages"]):
                if not isinstance(st, dict) or not st.get("id"):
                    return self.send_json(400, {"ok": False, "error": f"stage #{i} 缺 id"})
                if st["id"] in ids:
                    return self.send_json(400, {"ok": False, "error": f"stage id 重复：{st['id']}"})
                ids.add(st["id"])
                st.setdefault("requirements", [])
                st.setdefault("commands", [])
            gates = known_gates()
            for st in wf["stages"]:
                for req in st.get("requirements", []):
                    g = (req or {}).get("gate", "")
                    if g and gates and g not in gates:
                        return self.send_json(400, {"ok": False, "error": f"未知闸门 {g}；可用：{', '.join(gates)}"})
            with LOCK:
                backup = os.path.join(BASE, "kb", f"workflow.backup-{datetime.now():%Y%m%d-%H%M%S}.json")
                try:
                    with open(WORKFLOW, "rb") as src, open(backup, "wb") as dst:
                        dst.write(src.read())
                except Exception:
                    backup = None
                wf["updated"] = datetime.now().strftime("%Y-%m-%d")
                write_json(WORKFLOW, wf)
            return self.send_json(200, {"ok": True, "backup": backup, "stages": len(wf["stages"])})

        if path == "/api/kb/task":
            name = str(body.get("name") or "").strip()
            if not name:
                return self.send_json(400, {"ok": False, "error": "缺 name"})
            try:
                task = knowledge_module().ensure_task(name)
                return self.send_json(200, {"ok": True, "task": task})
            except (ValueError, OSError) as exc:
                return self.send_json(400, {"ok": False, "error": str(exc)})
        if path == "/api/kb/write":
            rel = str(body.get("path") or "")
            content = body.get("content")
            try:
                task = selected_task(body)
                if not rel.startswith("shared/") and task is None:
                    raise ValueError("需要任务分区")
                kb = knowledge_module()
                full = kb.safe_rel(rel) if rel.startswith("shared/") else kb.safe_rel(rel, task=task)
                if not rel or not isinstance(content, str) or len(content) > 2 * 1024 * 1024:
                    raise ValueError("缺 path/content 或内容过大")
                os.makedirs(os.path.dirname(full), exist_ok=True)
                with LOCK:
                    if os.path.isfile(full):
                        old = kb.read_entry_content(rel, task=task)
                        if body.get('expected_sha256') != old['file_sha256']:
                            raise ValueError('资料已存在或已变化；请读取当前版本后确认替换，旧版将保留')
                    temporary = full + ".tmp-" + uuid.uuid4().hex
                    try:
                        with open(temporary, "w", encoding="utf-8") as f:
                            f.write(content)
                        os.replace(temporary, full)
                    finally:
                        if os.path.exists(temporary):
                            os.remove(temporary)
                kb.list_entries(task=task)
                kb.read_entry_content(rel, task=task)
                return self.send_json(200, {"ok": True, "task_id": task, "path": rel})
            except (ValueError, OSError) as exc:
                return self.send_json(400, {"ok": False, "error": str(exc)})
        if path == "/api/kb/attach":
            try:
                shared = body.get('scope') == 'shared'
                task = None if shared else selected_task(body)
                name = str(body.get("name") or "").strip()
                data = body.get("data")
                if not re.fullmatch(r"[\w.\-\u4e00-\u9fff]{1,120}", name, re.UNICODE) or name in {".", ".."}:
                    raise ValueError("文件名无效")
                if not isinstance(data, str) or len(data) > 40 * 1024 * 1024:
                    raise ValueError("附件内容过大或无效")
                raw = base64.b64decode(data, validate=True)
                if len(raw) > 25 * 1024 * 1024:
                    raise ValueError("附件超过 25 MB")
                kb = knowledge_module()
                rel = "shared/files/" + name if shared else "tasks/" + task + "/files/" + name
                full = kb.safe_rel(rel, task=task)
                os.makedirs(os.path.dirname(full), exist_ok=True)
                with LOCK:
                    if os.path.isfile(full):
                        old = kb.read_entry_content(rel, task=task)
                        if body.get('expected_sha256') != old['file_sha256']:
                            raise ValueError('同名资料已存在；请查看当前资料并明确替换，或重命名后上传')
                    temporary = full + ".tmp-" + uuid.uuid4().hex
                    try:
                        with open(temporary, "wb") as f:
                            f.write(raw)
                        os.replace(temporary, full)
                    finally:
                        if os.path.exists(temporary):
                            os.remove(temporary)
                kb.list_entries(task=task)
                kb.read_entry_content(rel, task=task)
                return self.send_json(200, {"ok": True, "task_id": task, "path": rel, "bytes": len(raw)})
            except (ValueError, OSError) as exc:
                return self.send_json(400, {"ok": False, "error": str(exc)})
        if path == '/api/kb/decision':
            try:
                task = str(body.get('task_id') or '') or None
                rel = str(body.get('path') or '')
                if not rel.startswith('shared/') and not task:
                    raise ValueError('需要当前项目分区')
                item = knowledge_module().read_entry_content(rel, task=task)
                if body.get('expected_sha256') != item['file_sha256']:
                    raise ValueError('资料文件已变化，请刷新后确认')
                result = plugin_module('wf_knowledge').decide(BASE, rel, body.get('expected_revision'),
                    body.get('status'), body.get('request_id'), module=body.get('module'))
                return self.send_json(200, dict(ok=True, **result))
            except (ValueError, OSError) as exc:
                return self.send_json(409, {'ok':False, 'error':str(exc)})
        if path == '/api/kb/promote':
            try:
                return self.send_json(200, promote_artifact(body))
            except (ValueError, OSError) as exc:
                return self.send_json(409, {'ok':False, 'error':str(exc)})
        if path in {"/api/wf/reply", "/api/wf/revise", "/api/wf/discuss"}:
            try:
                response = submit_node_command(path.rsplit('/', 1)[1], body)
                return self.send_json(200, response)
            except ValueError as exc:
                return self.send_json(409, {'ok':False,'error':str(exc)})
        if path == "/api/wf/visual-review":
            rid, nid = safe_run_part(body.get("run_id")), safe_run_part(body.get("node"))
            state = read_json(run_path(rid, "state.json"), {})
            node = next((n for n in state.get("nodes", []) if n.get("id") == nid), {})
            if not node:
                return self.send_json(404, {"ok": False, "error": "节点不存在"})
            workspace = node.get("workspace")
            if not workspace:
                return self.send_json(409, {"ok": False, "error": "该节点没有可复核的工作区（旧运行或未执行）"})
            try:
                workspace_path = contained_path(run_path(rid), os.path.relpath(workspace, run_path(rid)))
            except ValueError:
                return self.send_json(400, {"ok": False, "error": "工作区路径不合法"})
            if not os.path.isdir(workspace_path):
                return self.send_json(404, {"ok": False, "error": "工作区不存在"})
            images = []
            for artifact in node.get("artifact_evidence", []) or []:
                full = contained_path(BASE, artifact.get("path", ""))
                if os.path.isdir(full):
                    for root, dirs, names in os.walk(full):
                        for name in names:
                            if name.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
                                images.append(os.path.relpath(os.path.join(root, name), BASE).replace("\\", "/"))
                elif os.path.isfile(full) and full.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
                    images.append(str(artifact.get("path")))
            review = {"status": "PASS", "reviewer": "user", "reviewed_at": datetime.now().isoformat(timespec="seconds"),
                      "frames": images[:30], "checked": ["用户在插件内目视复核"], "notes": str(body.get("notes") or "")[:1000]}
            with LOCK:
                write_json(os.path.join(workspace_path, "visual-review.json"), review)
                episode = str(state.get("episode") or "").strip()
                if episode:
                    project_review = os.path.join(BASE, "episodes", episode + "-visual-review.json")
                    os.makedirs(os.path.dirname(project_review), exist_ok=True)
                    write_json(project_review, review)
            return self.send_json(200, {"ok": True, "review": review})
        if path == "/api/wf/check":
            rid, nid = body.get("run_id"), body.get("node")
            if not rid or not nid:
                return self.send_json(400, {"ok": False, "error": "缺 run_id/node"})
            state = read_json(run_path(rid, "state.json"), {})
            if state.get("agent_mode") == "automatic":
                return self.send_json(409, {"ok": False, "error": "自动执行的验收由执行结果更新，无需手工勾选"})
            args = ["wf.py", "agent-check", "--run", str(rid), "--node", str(nid)]
            if body.get("all"):
                args.append("--all")
            else:
                args += ["--index", str(int(body.get("index", 0)))]
            r = subprocess.run([PY] + args, cwd=APP_DIR, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=30)
            return self.send_json(200 if r.returncode == 0 else 400,
                                  {"ok": r.returncode == 0, "output": (r.stdout or r.stderr)[:300]})
        if path == "/api/wf/agent-skip":
            rid, nid = body.get("run_id"), body.get("node")
            if not rid or not nid:
                return self.send_json(400, {"ok": False, "error": "缺 run_id/node"})
            state = read_json(run_path(rid, "state.json"), {})
            if state.get("agent_mode") == "automatic" or state.get("status") == "running":
                return self.send_json(409, {"ok": False, "error": "请回复当前问题后继续，自动任务不能手工跳过"})
            start_wf(["agent-skip", "--run", str(rid), "--node", str(nid)])
            return self.send_json(200, {"ok": True, "skipped": nid})
        if path == "/api/wf/agent-done":
            rid, nid = body.get("run_id"), body.get("node")
            if not rid or not nid:
                return self.send_json(400, {"ok": False, "error": "缺 run_id/node"})
            state = read_json(run_path(rid, "state.json"), {})
            if state.get("agent_mode") == "automatic" or state.get("status") == "running":
                return self.send_json(409, {"ok": False, "error": "自动任务以本轮执行证据为准，不能手工标记完成"})
            args = ["agent-done", "--run", str(rid), "--node", str(nid)]
            if body.get("accept_all"):
                args.append("--accept-all")
            start_wf(args)
            return self.send_json(200, {"ok": True, "run_id": rid, "node": nid})
        if path == "/api/wf/cancel":
            rid = body.get("run_id") or ""
            if not rid:
                try:
                    rid = open(os.path.join(BASE, ".runs", "LATEST"), encoding="utf-8").read().strip()
                except Exception:
                    rid = ""
            if not rid:
                return self.send_json(400, {"ok": False, "error": "没有运行记录"})
            state = read_json(run_path(rid, "state.json"), None)
            if not isinstance(state, dict):
                return self.send_json(404, {"ok": False, "error": "找不到这次运行：" + str(rid)})
            status = str(state.get("status") or "")
            if status in ("completed", "cancelled", "creation_ready"):
                return self.send_json(200, {"ok": True, "cancel": rid, "already": status})
            if state.get("cleanup_pending"):
                return self.send_json(409, {"ok": False,
                                            "error": "上次执行的进程清理还没有确认；请先在运行日志里确认清理，再停止"})
            flag = run_path(rid, ".cancel")
            with open(flag, "w", encoding="utf-8") as marker:
                marker.write("1")
            if live_worker(rid):
                append_run_event(rid, "stop_requested")
                return self.send_json(200, {"ok": True, "cancel": rid, "stopping": True,
                                            "hint": "执行进程会在当前步骤结束后停下"})
            # No live runner: the flag would never be read, so finish the run here.
            stamp = datetime.now().isoformat(timespec="seconds")
            changed = 0
            for node in state.get("nodes") or []:
                if node.get("status") in ("pending", "running", "awaiting_input", "awaiting_agent",
                                          "awaiting_approval", "stopping", "blocked"):
                    node["status"] = "cancelled"
                    node["detail"] = node.get("detail") or "任务已停止"
                    node["finished_at"] = stamp
                    changed += 1
            state["status"] = "cancelled"
            state["updated_at"] = stamp
            state["finished_at"] = stamp
            write_json(run_path(rid, "state.json"), state)
            append_run_event(rid, "run_cancelled")
            try:
                os.remove(flag)
            except OSError:
                pass
            return self.send_json(200, {"ok": True, "cancel": rid, "cancelled": True, "nodes": changed})
        if path == "/api/wf/pause":
            rid = body.get("run_id") or ""
            if not rid:
                return self.send_json(400, {"ok": False, "error": "缺 run_id"})
            state = read_json(run_path(rid, "state.json"), None)
            if not isinstance(state, dict):
                return self.send_json(404, {"ok": False, "error": "找不到这次运行：" + str(rid)})
            status = str(state.get("status") or "")
            if status in ("paused", "pausing"):
                return self.send_json(200, {"ok": True, "run_id": rid, "paused": status == "paused"})
            if status in ("completed", "cancelled", "creation_ready", "failed"):
                return self.send_json(409, {"ok": False, "error": "任务已经结束，不能暂停"})
            if status in ("awaiting_input", "awaiting_agent", "awaiting_approval"):
                return self.send_json(409, {"ok": False, "error": "任务正在等你处理；可以直接停止，或先回复再继续"})
            if not live_worker(rid):
                return self.send_json(409, {"ok": False, "error": "当前没有正在执行的进程；可以直接停止这次任务"})
            with open(run_path(rid, ".pause"), "w", encoding="utf-8") as marker:
                marker.write("1")
            append_run_event(rid, "pause_requested")
            return self.send_json(200, {"ok": True, "run_id": rid, "pausing": True,
                                        "hint": "当前步骤结束后暂停，进度会保留"})
        if path == "/api/wf/resume":
            rid = body.get("run_id") or ""
            if not rid:
                return self.send_json(400, {"ok": False, "error": "缺 run_id"})
            state = read_json(run_path(rid, "state.json"), None)
            if not isinstance(state, dict):
                return self.send_json(404, {"ok": False, "error": "找不到这次运行：" + str(rid)})
            if str(state.get("status") or "") != "paused":
                return self.send_json(409, {"ok": False, "error": "只有已暂停的任务可以继续"})
            try:
                os.remove(run_path(rid, ".pause"))
            except OSError:
                pass
            # Keep the state at "paused" until the runner really picks the next
            # step: a failed start must stay resumable instead of looking running.
            append_run_event(rid, "resume_requested")
            start_wf(["resume", str(rid)])
            return self.send_json(200, {"ok": True, "run_id": rid, "resumed": True})
        if path in {"/api/wf/rerun", "/api/wf/continue"}:
            rid, node = body.get("run_id"), body.get("from")
            if not rid or (path.endswith('/rerun') and not node):
                return self.send_json(400, {"ok": False, "error": "缺 run_id/from"})
            request_id = body.get('request_id') or uuid.uuid4().hex
            action = 'rerun' if node else 'continue'
            admitted = plugin_module('wf_commands').lookup(BASE, request_id)
            if admitted:
                saved = json.loads(admitted['payload'])
                if admitted['run_id'] != rid or admitted['action'] != action or saved.get('node') != node or saved.get('feedback') != body.get('feedback'):
                    raise ValueError('请求标识已被其他操作使用')
                return self.send_json(200, {'ok': True, 'rerun': rid, 'from': node, 'request_id': request_id, 'status': admitted['status']})
            state = read_json(run_path(rid, "state.json"), {})
            if body.get('expected_state_revision') is not None and int(body['expected_state_revision']) != int(state.get('state_revision', 0)):
                raise ValueError('运行状态已变化，请刷新后再操作')
            if state.get("status") == "running" or state.get("cleanup_pending"):
                return self.send_json(409, {"ok": False, "error": "请先停止当前执行并确认清理后再重跑"})
            if path.endswith('/continue') and not any(n.get('status') == 'stale' for n in state.get('nodes', [])):
                return self.send_json(409, {'ok':False,'error':'没有需要更新的下游节点'})
            plugin_module('wf_commands').enqueue(BASE, action, {
                'request_id': request_id, 'project_id': body.get('project_id') or state.get('project_id'),
                'run_id': rid, 'node': node, 'feedback': body.get('feedback'),
                'expected_generation': node_generation(next((n for n in state.get('nodes', []) if n['id'] == node), {})) if node else None,
                'expected_state_revision': int(state.get('state_revision', 0))})
            return self.send_json(200, {"ok": True, "rerun": rid, "from": node, 'request_id': request_id, 'status': 'queued'})
        if path == "/api/wf/watch":
            start_wf(["trigger", "--interval", str(int(body.get("interval") or 180))])
            return self.send_json(200, {"ok": True, "hint": "已启动自动监听（每 3 分钟扫描新 brief）"})
        if path == "/api/workflows/import":
            payload = body.get("workflow") or body
            graph = payload.get("graph") if isinstance(payload, dict) else None
            validate_import_graph(graph)
            name = str(payload.get("name") or "导入工作流").strip()[:80] or "导入工作流"
            base_id = workflow_id(name)
            index = load_workflow_index()
            existing = {w.get("id") for w in index.get("workflows", [])}
            wid = base_id
            n = 2
            while wid in existing:
                wid = base_id + "-" + str(n)
                n += 1
            data = {"id": wid, "name": name, "description": str(payload.get("description") or "")[:500],
                    "tier": str(payload.get("tier") or "experimental")[:40],
                    "created": datetime.now().isoformat(timespec="seconds"),
                    "updated": datetime.now().isoformat(timespec="seconds"), "graph": graph}
            write_json(workflow_file(wid), data)
            index.setdefault("workflows", []).append({"id": wid, "name": name,
                                                      "description": data["description"], "tier": data["tier"],
                                                      "updated": data["updated"]})
            index["active"] = wid
            save_workflow_index(index)
            write_json(os.path.join(BASE, "kb", "canvas-workflow.json"), graph)
            return self.send_json(200, {"ok": True, "workflow": data, "active": wid})
        if path == "/api/workflows/create":
            name = str(body.get("name") or "").strip() or "新工作流"
            base_id = workflow_id(name)
            index = load_workflow_index()
            existing = {w.get("id") for w in index.get("workflows", [])}
            wid = base_id
            n = 2
            while wid in existing:
                wid = base_id + "-" + str(n)
                n += 1
            if body.get("from"):
                source = read_json(workflow_file(body["from"]), None)
                graph = (source or {}).get("graph") or {"nodes": [], "edges": []}
            else:
                graph = {"nodes": [], "edges": []}
            data = {"id": wid, "name": name, "description": str(body.get("description") or ""),
                    "tier": str(body.get("tier") or "experimental")[:40],
                    "created": datetime.now().isoformat(timespec="seconds"),
                    "updated": datetime.now().isoformat(timespec="seconds"), "graph": graph}
            write_json(workflow_file(wid), data)
            index.setdefault("workflows", []).append({"id": wid, "name": name,
                                                     "description": data["description"], "tier": data["tier"],
                                                     "updated": data["updated"]})
            save_workflow_index(index)
            activate_workflow(wid)
            return self.send_json(200, {"ok": True, "workflow": data, "active": wid})
        if path == "/api/workflows/select":
            wid = workflow_id(body.get("id"))
            data = activate_workflow(wid)
            return self.send_json(200, {"ok": True, "active": wid, "workflow": data})
        if path == "/api/workflows/rename":
            wid = workflow_id(body.get("id"))
            name = str(body.get("name") or "").strip()
            if not name:
                raise ValueError("工作流名称不能为空")
            index = load_workflow_index()
            row = next((w for w in index.get("workflows", []) if w.get("id") == wid), None)
            if not row:
                raise ValueError("工作流不存在：" + wid)
            data = read_json(workflow_file(wid), {})
            data["name"] = name
            data["updated"] = datetime.now().isoformat(timespec="seconds")
            row["name"] = name; row["updated"] = data["updated"]
            write_json(workflow_file(wid), data)
            save_workflow_index(index)
            return self.send_json(200, {"ok": True, "active": index.get("active"), "workflow": data})
        if path == "/api/workflows/delete":
            wid = workflow_id(body.get("id"))
            index = load_workflow_index()
            if wid == index.get("active"):
                raise ValueError("不能删除当前工作流；请先切换到其他工作流")
            if len(index.get("workflows", [])) <= 1:
                raise ValueError("至少保留一个工作流")
            index["workflows"] = [w for w in index.get("workflows", []) if w.get("id") != wid]
            save_workflow_index(index)
            try:
                os.remove(workflow_file(wid))
            except FileNotFoundError:
                pass
            return self.send_json(200, {"ok": True, "active": index.get("active"), "workflows": index["workflows"]})
        if path == "/api/canvas":
            graph = body.get("graph") or body
            if not isinstance(graph, dict) or not isinstance(graph.get("nodes"), list) or not graph["nodes"]:
                return self.send_json(400, {"ok": False, "error": "canvas 需要非空 nodes"})
            canvas_path = os.path.join(BASE, "kb", "canvas-workflow.json")
            with LOCK:
                old = read_json(canvas_path, {})
                current = canvas_revision(old)
                base = body.get("revision") or body.get("base_revision")
                if base and base != current and not body.get("force"):
                    return self.send_json(409, {"ok": False, "error": "画布已被另一个窗口修改，请刷新后再保存",
                                                "revision": current})
                if old.get("nodes"):
                    save_revision(old)
                write_json(canvas_path, graph)
                r = subprocess.run([PY, "wf.py", "canvas", "sync"], cwd=APP_DIR, capture_output=True,
                                   text=True, encoding="utf-8", errors="replace", timeout=60)
                if r.returncode != 0:
                    write_json(canvas_path, old)
                    return self.send_json(400, {"ok": False, "error": (r.stderr or r.stdout)[:400]})
                try:
                    synced = json.loads(r.stdout)
                except Exception:
                    synced = {}
                save_active_workflow(graph)
            return self.send_json(200, {"ok": True, "graph": graph, "revision": canvas_revision(graph), "synced": synced})
        if path == "/api/canvas/template":
            tid = body.get("id")
            data = read_json(os.path.join(BASE, "kb", "canvas-templates.json"), {"templates": []})
            tpl = next((t for t in data.get("templates", []) if t.get("id") == tid), None)
            if not tpl:
                return self.send_json(400, {"ok": False, "error": "模板不存在：" + str(tid)})
            with LOCK:
                old = read_json(os.path.join(BASE, "kb", "canvas-workflow.json"), {})
                base = body.get("revision") or body.get("base_revision")
                if base and base != canvas_revision(old):
                    return self.send_json(409, {"ok": False, "error": "画布已被另一个窗口修改，请刷新后再载入模板",
                                                "revision": canvas_revision(old)})
                if old.get("nodes"):
                    save_revision(old)
                code, err = apply_canvas_graph(tpl["graph"])
                if code != 0:
                    return self.send_json(400, {"ok": False, "error": err[:400]})
                save_active_workflow(tpl["graph"])
            return self.send_json(200, {"ok": True, "graph": tpl["graph"], "revision": canvas_revision(tpl["graph"]), "template": tid})
        if path == "/api/canvas/restore":
            rid = str(body.get("id") or "")
            if not re.fullmatch(r"[\w\-]+\.json", rid, re.UNICODE):
                raise ValueError("无效的版本标识")
            path_rev = contained_path(canvas_dir(), rid)
            if not rid or not os.path.exists(path_rev):
                return self.send_json(400, {"ok": False, "error": "版本不存在"})
            graph = read_json(path_rev, {})
            with LOCK:
                old = read_json(os.path.join(BASE, "kb", "canvas-workflow.json"), {})
                base = body.get("revision") or body.get("base_revision")
                if base and base != canvas_revision(old):
                    return self.send_json(409, {"ok": False, "error": "画布已被另一个窗口修改，请刷新后再恢复",
                                                "revision": canvas_revision(old)})
                if old.get("nodes"):
                    save_revision(old)
                code, err = apply_canvas_graph(graph)
                if code != 0:
                    return self.send_json(400, {"ok": False, "error": err[:400]})
                save_active_workflow(graph)
            return self.send_json(200, {"ok": True, "graph": graph, "revision": canvas_revision(graph)})
        if path == "/api/canvas/import":
            graph = body.get("graph") or body
            canvas_path = os.path.join(BASE, "kb", "canvas-workflow.json")
            with LOCK:
                old = read_json(canvas_path, {})
                base = body.get("revision") or body.get("base_revision")
                if base and base != canvas_revision(old):
                    return self.send_json(409, {"ok": False, "error": "画布已被另一个窗口修改，请刷新后再导入",
                                                "revision": canvas_revision(old)})
                write_json(canvas_path, graph)
                r = subprocess.run([PY, "wf.py", "canvas", "sync"], cwd=APP_DIR, capture_output=True,
                                   text=True, encoding="utf-8", errors="replace", timeout=60)
                if r.returncode != 0:
                    write_json(canvas_path, old)
                    return self.send_json(400, {"ok": False, "error": (r.stderr or r.stdout)[:400]})
            graph_out = read_json(canvas_path, {})
            save_active_workflow(graph_out)
            return self.send_json(200, {"ok": True, "graph": graph_out, "revision": canvas_revision(graph_out)})
        if path == "/api/canvas/add":
            module = body.get("module")
            if not module:
                return self.send_json(400, {"ok": False, "error": "缺 module"})
            args = ["wf.py", "canvas", "add", "--module", str(module)]
            if body.get("after"):
                args += ["--after", str(body["after"])]
            r = subprocess.run([PY] + args, cwd=APP_DIR, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=60)
            if r.returncode != 0:
                return self.send_json(400, {"ok": False, "error": (r.stderr or r.stdout)[:400]})
            return self.send_json(200, {"ok": True, "graph": read_json(os.path.join(BASE, "kb", "canvas-workflow.json"), {})})
        if path == "/api/canvas/remove":
            nid = body.get("id")
            if not nid:
                return self.send_json(400, {"ok": False, "error": "缺 id"})
            r = subprocess.run([PY, "wf.py", "canvas", "remove", "--id", str(nid)], cwd=APP_DIR,
                               capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
            return self.send_json(200 if r.returncode == 0 else 400,
                                  {"ok": r.returncode == 0, "graph": read_json(os.path.join(BASE, "kb", "canvas-workflow.json"), {}),
                                   "error": (r.stderr or r.stdout)[:300] if r.returncode else None})
        if path == "/api/canvas/connect":
            args = ["wf.py", "canvas", "connect", "--from", str(body.get("from") or ""), "--to", str(body.get("to") or "")]
            r = subprocess.run([PY] + args, cwd=APP_DIR, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=60)
            return self.send_json(200 if r.returncode == 0 else 400,
                                  {"ok": r.returncode == 0, "graph": read_json(os.path.join(BASE, "kb", "canvas-workflow.json"), {}),
                                   "error": (r.stderr or r.stdout)[:300] if r.returncode else None})
        if path == "/api/create":
            topic = (body.get("topic") or "").strip()
            if not topic:
                return self.send_json(400, {"ok": False, "error": "缺 topic"})
            args = [PY, "wf.py", "create", "--topic", topic,
                    "--duration", str(int(body.get("duration") or 90)),
                    "--type", str(body.get("type") or "tool_test")]
            if body.get("episode"):
                args += ["--episode", str(body["episode"])]
            r = subprocess.run(args, cwd=APP_DIR, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=300)
            try:
                data = json.loads((r.stdout or "").strip())
            except Exception:
                try:
                    data = json.loads((r.stdout or "").strip().splitlines()[-1])
                except Exception:
                    return self.send_json(500, {"ok": False, "error": (r.stderr or r.stdout)[:400]})
            return self.send_json(200 if data.get("ok") else 400, data)

        if path == "/api/wf/run" and body.get('project_id'):
            try:
                command, fresh = projects_store().admit_run(body)
            except projects_store_conflict() as exc:
                return self.send_json(409, {'ok': False, 'error': str(exc)})
            return self.send_json(202, {k: v for k, v in command.items() if k != 'project'})
        if path == "/api/wf/run":
            episode = body.get("episode") or "day1"
            args = ["canvas-run", "--episode", str(episode)]
            if body.get("until"):
                args += ["--until", str(body["until"])]
            if body.get("fresh"):
                args += ["--fresh"]
            start_wf(args)
            return self.send_json(200, {"ok": True, "started": episode, "until": body.get("until"), "hint": "稍后刷新状态"})
        if path == "/api/wf/approve":
            rid = body.get("run_id")
            if not rid:
                return self.send_json(400, {"ok": False, "error": "缺 run_id"})
            with LOCK:
                r = subprocess.run([PY, WF, "approve", rid], cwd=APP_DIR, capture_output=True,
                                   text=True, encoding="utf-8", errors="replace", timeout=60)
            if r.returncode != 0:
                return self.send_json(400, {"ok": False, "error": (r.stderr or r.stdout)[:300]})
            start_wf(["resume", rid])
            return self.send_json(200, {"ok": True, "approved": rid, "hint": "已批准并继续运行"})
        if path == "/api/wf/reject":
            rid = body.get("run_id")
            if not rid:
                return self.send_json(400, {"ok": False, "error": "缺 run_id"})
            r = subprocess.run([PY, WF, "reject", rid], cwd=APP_DIR, capture_output=True,
                               text=True, encoding="utf-8", errors="replace", timeout=60)
            return self.send_json(200, {"ok": r.returncode == 0, "output": (r.stdout or r.stderr)[:300]})

        if path == "/api/library":
            library = body.get("library", body)
            if not isinstance(library, dict) or not isinstance(library.get("blocks"), list) or not library["blocks"]:
                return self.send_json(400, {"ok": False, "error": "library.blocks 必须是非空数组"})
            for b in library["blocks"]:
                if not b.get("id") or not isinstance(b.get("templates"), list) or not b["templates"]:
                    return self.send_json(400, {"ok": False, "error": "每个区块必须有 id 和非空 templates"})
                ids = set()
                for t in b["templates"]:
                    if not t.get("id") or not t.get("name") or not t.get("content"):
                        return self.send_json(400, {"ok": False, "error": f"区块 {b['id']} 有模板缺 id/name/content"})
                    if t["id"] in ids:
                        return self.send_json(400, {"ok": False, "error": f"模板 id 重复：{t['id']}"})
                    ids.add(t["id"])
            with LOCK:
                backup = os.path.join(BASE, "kb", f"spec-library.backup-{datetime.now():%Y%m%d-%H%M%S}.json")
                try:
                    with open(LIBRARY, "rb") as src, open(backup, "wb") as dst:
                        dst.write(src.read())
                except Exception:
                    backup = None
                write_json(LIBRARY, library)
            ok, audit, err = audit_library(library)
            if not ok:
                if backup:
                    try:
                        import shutil
                        shutil.copyfile(backup, LIBRARY)
                    except Exception:
                        pass
                return self.send_json(400, {"ok": False, "error": "spec_compose --audit 未通过，已回滚", "audit": audit, "detail": err})
            return self.send_json(200, {"ok": True, "backup": backup, "audit": audit})

        if path == "/api/layout":
            layout = body.get("layout", body)
            if not isinstance(layout, dict):
                return self.send_json(400, {"ok": False, "error": "layout 必须是对象"})
            with LOCK:
                write_json(LAYOUT, layout)
            return self.send_json(200, {"ok": True})

        self.send_json(404, {"ok": False, "error": "not found"})


class CanvasHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = False

    def server_bind(self):
        if os.name == 'nt':
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def main():
    global API_PORT
    try:
        server = CanvasHTTPServer(("127.0.0.1", API_PORT), Handler)
    except OSError as exc:
        raise SystemExit("API 端口 %d 已被占用（或权限不足）：%s" % (API_PORT, exc))
    API_PORT = server.server_address[1]
    if not os.path.isfile(workflows_index()) and os.path.isfile(os.path.join(BASE, 'kb', 'canvas-workflow.json')):
        ensure_workflows()
    package_root = Path(APP_DIR).parent
    seed_spec = importlib.util.spec_from_file_location('canvas_builtin_seed', package_root / 'scripts/seed.py')
    seed_module = importlib.util.module_from_spec(seed_spec)
    seed_spec.loader.exec_module(seed_module)
    seed_module.install(Path(BASE), package_root / 'seed')
    ensure_workflows()
    sync_active_workflow_from_mirror()
    port = API_PORT
    print(f"[canvas-editor] serving canvas + API at http://127.0.0.1:{port}/", flush=True)
    print(f"[canvas-editor] canvas file: {CANVAS}", flush=True)
    print(f"[canvas-editor] workflow file: {WORKFLOW}", flush=True)
    def dispatch_commands():
        while True:
            try:
                commands = plugin_module('wf_commands').pending(BASE, engine_module()._pid_alive)
                if any(c['status'] == 'queued' for c in commands):
                    # A detached worker owns the executor lease and survives an
                    # editor-server restart. Only one may acquire that lease.
                    with open(os.path.join(RUNS, 'wf-commands.log'), 'a', encoding='utf-8') as log:
                        subprocess.Popen([PY, os.path.join(APP_DIR, 'wf_commands.py')], cwd=APP_DIR,
                            stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            except Exception as exc:
                print('Command dispatcher: ' + str(exc), flush=True)
            threading.Event().wait(3)
    threading.Thread(target=dispatch_commands, daemon=True).start()
    server.serve_forever()


if __name__ == "__main__":
    main()



