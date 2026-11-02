#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Global knowledge base with per-task partitions.

Layout:
  kb/knowledge/
    index.json
    shared/                 global knowledge (account, audience, style, workflow)
    tasks/<task>/           one partition per task
      manifest.json
      brief.md, research.md, script-notes.md, lessons.md, ...

Usage:
  python kb.py list [--task T] [--scope shared|task] [--search Q]
  python kb.py read <path> [--task T]
  python kb.py write <path> [--task T] --text "..." | --file f.md
  python kb.py task-new <name>
  python kb.py sync-index
  python kb.py context <task>     # build episodes/<task>-kb-context.md
  python kb.py archive <task>     # copy task outputs into its partition
"""
import argparse
import hashlib
import json
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime
from html.parser import HTMLParser

BASE = os.environ.get("CANVAS_HOME") or os.path.dirname(os.path.abspath(__file__))
KB = os.path.join(BASE, "kb", "knowledge")
SHARED = os.path.join(KB, "shared")
TASKS = os.path.join(KB, "tasks")
INDEX = os.path.join(KB, "index.json")
TEXT_EXTENSIONS = {".md", ".txt", ".json", ".csv", ".tsv", ".srt", ".vtt", ".yaml", ".yml", ".log", ".jsonl", ".html", ".htm"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
MAX_TEXT_BYTES = 5 * 1024 * 1024
MAX_DOCUMENT_BYTES = 20 * 1024 * 1024
_CONTENT_CACHE = {}


class _VisibleHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        if tag in {"p", "div", "br", "li", "h1", "h2", "h3", "tr"} and not self.hidden:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self.hidden:
            self.hidden -= 1

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def _file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _extract_document(full, extension, size):
    """Extract local text only; never execute an uploaded document."""
    if extension in IMAGE_EXTENSIONS:
        if os.name != 'nt':
            return 'image', 'preview_only', '', '图片可预览；此环境没有Windows OCR适配'
        if size > MAX_DOCUMENT_BYTES:
            return 'image', 'too_large', '', '图片超过OCR大小限制'
        try:
            script = os.path.join(os.path.dirname(__file__), 'wf_ocr.ps1')
            result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                '-File', script, '-Path', os.path.abspath(full)], capture_output=True, text=True,
                encoding='utf-8', errors='replace', timeout=45, creationflags=subprocess.CREATE_NO_WINDOW)
            if result.returncode:
                return 'image', 'preview_only', '', 'OCR未成功：原图保留可预览，可能缺少语言包或图片无法解码'
            parsed = json.loads(result.stdout)
            lines = [re.sub(r'(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])', '', line) for line in parsed['lines']]
            content = '\n'.join('[图像行' + str(i + 1) + '] ' + line for i, line in enumerate(lines))
            return 'image', 'parsed' if content.strip() else 'no_text', content, '' if content.strip() else '图片没有识别到文字'
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return 'image', 'preview_only', '', 'OCR组件不可用或解析超时；原图仍可预览'
    if extension not in TEXT_EXTENSIONS and extension != ".pdf":
        return "file", "unsupported", "", "已保存原文件，当前未提供此格式的文本提取"
    if size > (MAX_DOCUMENT_BYTES if extension == ".pdf" else MAX_TEXT_BYTES):
        return "pdf" if extension == ".pdf" else "text", "too_large", "", "文件超过当前文本提取大小限制"
    if extension == ".pdf":
        try:
            try:
                from pypdf import PdfReader
            except ImportError:
                try:
                    import fitz
                except ImportError:
                    return "pdf", "unsupported", "", "当前 Python 未安装 PDF 文本提取组件"
                with fitz.open(full) as document:
                    pages = [page.get_text() for page in document]
            else:
                reader = PdfReader(full)
                pages = [page.extract_text() or "" for page in reader.pages]
            text = '\n\n'.join('[第' + str(i + 1) + '页]\n' + text for i, text in enumerate(pages) if text.strip())
            return "pdf", "parsed" if text.strip() else "no_text", text, "" if text.strip() else "未提取到文本；扫描件需要 OCR"
        except Exception as error:
            return "pdf", "failed", "", "PDF 提取失败：" + type(error).__name__
    try:
        with open(full, "rb") as handle:
            raw = handle.read()
        if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
            text = raw.decode("utf-16")
        else:
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                text = raw.decode("gb18030")
        if extension in {".html", ".htm"}:
            parser = _VisibleHTML()
            parser.feed(text)
            text = re.sub(r"\n[ \t]*\n+", "\n\n", "".join(parser.parts)).strip()
        return "text", "parsed" if text.strip() else "empty", text, "" if text.strip() else "文件没有可用正文"
    except (OSError, UnicodeError) as error:
        return "text", "failed", "", "文本提取失败：" + type(error).__name__


def read_entry_content(rel, task=None):
    """Read metadata and extracted text for shared or the selected task only."""
    relative = os.fspath(rel).replace("\\", "/")
    full = safe_rel(relative, task=None if relative.startswith("shared/") else task)
    info = os.stat(full)
    if not os.path.isfile(full) or _is_link(full):
        raise ValueError("知识条目不是普通文件：" + relative)
    extension = os.path.splitext(full)[1].lower()
    cache_key = (full, info.st_size, info.st_mtime_ns)
    parsed = _CONTENT_CACHE.get(cache_key)
    if parsed is None:
        kind, status, content, error = _extract_document(full, extension, info.st_size)
        normalized = content.replace("\r\n", "\n").strip()
        parsed = {"kind": kind, "parse_status": status, "content": content,
                  "usable": status == "parsed", "parse_message": error,
                  "file_sha256": _file_sha256(full),
                  "content_sha256": hashlib.sha256(normalized.encode("utf-8")).hexdigest() if normalized else ""}
        if len(_CONTENT_CACHE) >= 256:
            _CONTENT_CACHE.pop(next(iter(_CONTENT_CACHE)))
        _CONTENT_CACHE[cache_key] = parsed
    source_type = "shared_reference" if relative.startswith("shared/") else "generated_draft" if "/outputs/" in relative else "task_material"
    from wf_knowledge import register, chunks
    metadata = register(BASE, relative, full, parsed)
    return dict(parsed, **metadata, segments=chunks(parsed['content']), path=relative, filename=os.path.basename(full), extension=extension,
                mime=mimetypes.guess_type(full)[0] or "application/octet-stream", bytes=info.st_size,
                source_type=source_type, review_status="reference" if source_type == "shared_reference" else "unreviewed",
                updated=datetime.fromtimestamp(info.st_mtime).isoformat(timespec="seconds"))


def now():
    return datetime.now().isoformat(timespec="seconds")


def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _atomic_text(path, json.dumps(obj, ensure_ascii=False, indent=2))


def _atomic_text(path, content):
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".kb-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(content)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def _within(path, root):
    try:
        return os.path.normcase(os.path.commonpath([path, root])) == os.path.normcase(root)
    except ValueError:
        return False


def _is_link(path):
    try:
        return os.path.islink(path) or getattr(os.lstat(path), "st_reparse_tag", 0) == 0xA0000003
    except FileNotFoundError:
        return False


def _task_root(task, require_exists=False):
    slug = slugify(task)
    root = os.path.join(TASKS, slug)
    kb_root = os.path.realpath(KB)
    task_base = os.path.realpath(TASKS)
    if not _within(task_base, kb_root) or _is_link(root):
        raise ValueError("任务分区路径越界：" + slug)
    resolved = os.path.realpath(root)
    if not _within(resolved, task_base) or resolved == task_base:
        raise ValueError("任务分区路径越界：" + slug)
    if require_exists and not os.path.isdir(resolved):
        raise ValueError("任务分区不存在：" + slug)
    return slug, resolved


def safe_rel(path, task=None):
    """Resolve a KB-relative path, optionally confined to one task partition."""
    if not isinstance(path, (str, os.PathLike)):
        raise ValueError("需要知识库相对路径")
    relative = os.fspath(path).replace("\\", "/")
    if (not relative or relative.startswith("/") or ":" in relative or "\x00" in relative
            or any(part in {"", ".", ".."} for part in relative.split("/"))):
        raise ValueError("路径越界或无效：" + relative)
    root = os.path.realpath(KB)
    full = os.path.realpath(os.path.join(KB, relative))
    if not _within(full, root) or full == root:
        raise ValueError("路径越界：" + relative)
    if task is not None:
        _, task_root = _task_root(task)
        if not _within(full, task_root) or full == task_root:
            raise ValueError("只允许当前任务分区：" + relative)
    return full


def title_of(path):
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("#"):
                    return line.lstrip("#").strip()
                if line:
                    return line[:40]
    except Exception:
        pass
    return os.path.basename(path)


def summary_of(path):
    try:
        with open(path, encoding="utf-8") as f:
            lines = [x.strip() for x in f if x.strip()]
        for line in lines:
            if not line.startswith("#") and not line.startswith("-"):
                return line[:120]
    except Exception:
        pass
    return ""


def slugify(name):
    if not isinstance(name, str):
        raise ValueError("任务名称必须是非空文本")
    value = name.strip()
    if not value or value in {".", ".."} or any(c in value for c in "/\\:\x00"):
        raise ValueError("任务名称不能为空或包含路径")
    reserved = {"CON", "PRN", "AUX", "NUL"} | {"COM" + str(i) for i in range(1, 10)} | {"LPT" + str(i) for i in range(1, 10)}
    if value.split(".", 1)[0].upper() in reserved:
        raise ValueError("任务名称不能使用系统保留名称")
    slug = re.sub(r"[^a-zA-Z0-9_\-\u4e00-\u9fff]+", "-", value).strip("-")
    if not slug:
        raise ValueError("任务名称必须含文字或数字")
    return slug


def _scan_entries(scope, root, task=None):
    entries = []
    if not os.path.isdir(root) or _is_link(root):
        return entries
    for dirpath, directories, files in os.walk(root, followlinks=False):
        directories[:] = sorted(name for name in directories if not _is_link(os.path.join(dirpath, name)))
        for filename in sorted(files):
            candidate = os.path.join(dirpath, filename)
            if (filename.startswith(".") or filename == "manifest.json"
                    or _is_link(candidate)):
                continue
            rel = os.path.relpath(candidate, KB).replace("\\", "/")
            try:
                full = safe_rel(rel, task=task)
            except ValueError:
                continue
            entry_task = task or (os.path.relpath(full, TASKS).split(os.sep)[0] if scope == "task" else "")
            try:
                item = read_entry_content(rel, task=task)
            except (OSError, ValueError):
                continue
            content = item.get("content", "")
            title = next((line.strip().lstrip("#").strip()[:80] for line in content.splitlines() if line.strip()), filename)
            if filename.lower().endswith(".md"):
                title = title_of(full)
            elif filename.lower().endswith('.json'):
                try:
                    value = json.loads(content)
                    if isinstance(value, dict) and isinstance(value.get('title'), str):
                        title = value['title'][:80]
                except (ValueError, TypeError):
                    pass
            summary = next((line.strip()[:160] for line in content.splitlines() if line.strip() and not line.lstrip().startswith("#")), "")
            entries.append(dict({key: value for key, value in item.items() if key != "content"},
                id=rel.replace("/", "."), scope=scope, task=entry_task,
                title=title, path=rel, tags=[entry_task] if entry_task else [], summary=summary,
                excerpt=content[:900], chars=len(content)))
    return entries


def sync_index():
    """Administrative full-store index refresh, never used for scoped retrieval."""
    entries = _scan_entries("shared", SHARED) + _scan_entries("task", TASKS)
    write_json(INDEX, {"version": 1, "updated": now(), "entries": entries})
    return entries


def _update_task_index(slug, root):
    current = read_json(INDEX, {"entries": []})
    others = [e for e in current.get("entries", []) if e.get("task") != slug]
    write_json(INDEX, {"version": 1, "updated": now(), "entries": others + _scan_entries("task", root, slug)})


def ensure_task(name):
    slug, root = _task_root(name)
    os.makedirs(root, exist_ok=True)
    files = {
        "brief.md": "# 任务 brief\n\n- 选题：\n- 类型：\n- 时长：\n- 目标：\n- 约束：\n",
        "research.md": "# 调研与素材\n\n- 事实来源：\n- 关键数据：\n- 参考视频：\n- 风险点：\n",
        "script-notes.md": "# 脚本备注\n\n- 开场钩子：\n- 核心论点：\n- 反对意见：\n- 结尾行动：\n",
        "lessons.md": "# 复盘与经验\n\n- 这次做对了什么：\n- 下次要改什么：\n",
    }
    for fname, content in files.items():
        path = os.path.join(root, fname)
        if not os.path.exists(path):
            _atomic_text(path, content)
    manifest_path = safe_rel("tasks/" + slug + "/manifest.json", task=slug)
    manifest = read_json(manifest_path, {})
    if not manifest:
        manifest = {"name": name, "slug": slug, "created": now(), "updated": now()}
        write_json(manifest_path, manifest)
    _update_task_index(slug, root)
    return slug


def list_entries(task=None, scope=None, search=None):
    if task is not None:
        slug, root = _task_root(task, require_exists=True)
        entries = _scan_entries("task", root, slug)
    elif scope == "shared":
        entries = _scan_entries("shared", SHARED)
    else:
        entries = sync_index()
    if scope:
        entries = [e for e in entries if e["scope"] == scope]
    if search:
        q = str(search).casefold()
        matches = []
        for entry in entries:
            try:
                content = read_entry_content(entry["path"], task=task).get("content", "")
            except (OSError, ValueError):
                content = ""
            haystack = entry["title"] + " " + entry["path"] + " " + content
            if q in haystack.casefold():
                position = content.casefold().find(q)
                matches.append(dict(entry, snippet=content[max(0, position - 60):position + max(120, len(q))].replace("\n", " ") if position >= 0 else entry["summary"]))
        entries = matches
    return entries


def list_tasks():
    """List partition metadata without reading knowledge documents."""
    if not os.path.isdir(TASKS):
        return []
    rows = []
    for name in sorted(os.listdir(TASKS)):
        try:
            slug, root = _task_root(name, require_exists=True)
        except ValueError:
            continue
        try:
            manifest_path = safe_rel("tasks/" + slug + "/manifest.json", task=slug)
        except ValueError:
            continue
        manifest = read_json(manifest_path, {})
        rows.append({"task_id": slug, "slug": slug, "name": manifest.get("name", slug),
                     "created": manifest.get("created"), "updated": manifest.get("updated")})
    return rows


def context_pack(task, query=None, tags=None, module=None, budget_chars=60000, required_paths=None):
    slug, _ = _task_root(task, require_exists=True)
    shared_entries = [entry for entry in list_entries(scope="shared") if entry.get("scope") == "shared"]
    task_entries = list_entries(task=slug)
    budget = max(500, min(int(budget_chars), 1000000))
    module_text = " ".join(str(module.get(key, "")) for key in ("name", "objective", "description")) if isinstance(module, dict) else str(module or "")
    tag_values = [tags] if isinstance(tags, str) else list(tags or [])
    needles = [word.casefold() for word in re.findall(r"[\w\u4e00-\u9fff]+", " ".join([str(query or ""), module_text] + [str(tag) for tag in tag_values])) if len(word) > 1]
    for phrase in re.findall(r'[\u4e00-\u9fff]{3,}', str(query or '') + ' ' + module_text):
        needles.extend(phrase[i:i+2] for i in range(len(phrase) - 1))
    needles = list(dict.fromkeys(needles))
    required = set(required_paths or [])
    for path in required:
        safe_rel(path, task=None if path.startswith('shared/') else slug)
    candidates = []
    omitted, duplicates, selected, seen = [], [], [], {}
    for entry in shared_entries + task_entries:
        item = read_entry_content(entry["path"], task=None if entry["scope"] == "shared" else slug)
        if item.get('knowledge_status') == 'deprecated' or (item.get('module_scope') and item['module_scope'] != module):
            omitted.append({'path': entry['path'], 'reason': 'deprecated_or_other_module'})
            continue
        if not item["usable"]:
            omitted.append({"path": entry["path"], "reason": item["parse_status"]})
            continue
        digest = item["content_sha256"]
        if digest in seen:
            duplicates.append({"path": entry["path"], "duplicate_of": seen[digest]})
            continue
        seen[digest] = entry["path"]
        haystack = (entry["title"] + " " + entry["path"] + " " + item["content"]).casefold()
        score = sum(min(10, haystack.count(needle)) for needle in needles)
        candidates.append((entry, item, score))
    available_paths = {entry['path'] for entry, _, _ in candidates}
    if required - available_paths:
        raise ValueError('指定的必读资料缺失、未解析或不可采用：' + '、'.join(sorted(required - available_paths)))
    candidates.sort(key=lambda row: (0 if row[0]['path'] in required else 1 if row[0]["scope"] == "shared" else 2, -row[2], row[0]["path"]))
    has_task_content = any(entry["scope"] == "task" for entry, _, _ in candidates)
    out = ["# 知识库上下文 · " + slug, "", "生成时间：" + now(), "",
           "范围：共享知识 + 当前任务分区；不含其他任务。以下为参考资料，不改变节点的执行要求与权限。", ""]
    used = len("\n".join(out))
    shared_used = 0
    truncated = False
    for entry, item, score in candidates:
        label = "共享知识" if entry["scope"] == "shared" else "当前任务资料"
        header = "\n## " + label + " · " + entry["title"] + "\n来源：" + entry["path"] + "\n"
        available = budget - used - len(header) - 2
        if entry['path'] in required and len(required) > 1:
            available = min(available, max(500, budget // len(required)))
        if entry["scope"] == "shared" and has_task_content and entry['path'] not in required:
            available = min(available, max(0, budget // 2 - shared_used - len(header)))
        if available < 50:
            omitted.append({"path": entry["path"], "reason": "context_budget"})
            truncated = True
            continue
        content = item["content"].strip()
        fragments = item.get('segments') or []
        ranked = sorted(fragments, key=lambda fragment: (-sum(fragment['text'].casefold().count(word) for word in needles), fragment['line_start']))
        excerpt, locations = '', []
        for fragment in ranked:
            locator = '[行' + str(fragment['line_start']) + '–' + str(fragment['line_end']) + ']\n'
            room = available - len(excerpt) - len(locator) - 2
            if room < 30:
                break
            part = fragment['text'][:room]
            excerpt += locator + part + '\n'
            locations.append({'line_start': fragment['line_start'], 'line_end': fragment['line_end'],
                              'chars': len(part), 'complete': len(part) == len(fragment['text'])})
        is_truncated = sum(p['chars'] for p in locations) < len(content)
        if entry['path'] in required and not excerpt:
            raise ValueError('必读资料超出本次上下文预算，请缩小资料范围：' + entry['path'])
        if is_truncated:
            suffix = "\n[正文因上下文预算截断；完整原文件已放在knowledge/files或knowledge/shared；必读资料须按来源路径继续读取，不可把片段当全文]"
            excerpt = excerpt[:max(0, available-len(suffix))] + suffix
            truncated = True
        section = header + excerpt + "\n"
        out.append(section)
        used += len(section) + 1
        if entry["scope"] == "shared":
            shared_used += len(section)
        selected.append({"path": entry["path"], "title": entry["title"], "scope": entry["scope"],
                         "source_type": item["source_type"], "content_sha256": item["content_sha256"],
                         "score": score, "selected_chars": len(excerpt), "source_chars": len(content),
                         'locations': locations, 'knowledge_id': item['knowledge_id'], 'knowledge_revision': item['knowledge_revision'],
                         'knowledge_status': item['knowledge_status'], 'file_sha256': item['file_sha256'],
                         'source_revision': item.get('source_revision'),
                         "truncated": is_truncated, "reason": 'required' if entry['path'] in required else "shared_reference" if entry["scope"] == "shared" else "query_match" if score else "task_material"})
    path = os.path.join(BASE, "episodes", slug + "-kb-context.md")
    text = "\n".join(out)[:budget]
    _atomic_text(path, text)
    write_json(os.path.splitext(path)[0] + ".sources.json", {
        "schema": "canvas.knowledge-selection/v1", "task_id": slug, "generated_at": now(),
        "query": str(query or ""), "module": module_text, "budget_chars": budget, "used_chars": len(text),
        "selected": selected, "omitted": omitted, "duplicates": duplicates, "truncated": truncated})
    return path


def archive(task, paths=None):
    slug = ensure_task(task)
    _, root = _task_root(slug, require_exists=True)
    os.makedirs(os.path.join(root, "outputs"), exist_ok=True)
    if paths is None:
        episode_dir = os.path.join(BASE, "episodes")
        prefixes = {str(task), slug}
        paths = [os.path.join("episodes", name) for name in sorted(os.listdir(episode_dir))
                 if any(name.startswith(prefix + "-") for prefix in prefixes)
                 and name.lower().endswith((".md", ".json")) and not name.endswith("-kb-context.md")] if os.path.isdir(episode_dir) else []
    sources = []
    output_names = set()
    for value in paths:
        source = os.path.realpath(os.path.join(BASE, value))
        if not _within(source, os.path.realpath(BASE)) or source == os.path.realpath(BASE):
            raise ValueError("归档来源必须位于项目内")
        if _within(source, os.path.realpath(KB)):
            source = safe_rel(os.path.relpath(source, KB), task=slug)
        filename = os.path.basename(source)
        if os.path.normcase(filename) in output_names:
            raise ValueError("归档产物文件名重复：" + filename)
        output_names.add(os.path.normcase(filename))
        sources.append(source)
    copied = []
    for source in sources:
        filename = os.path.basename(source)
        destination = safe_rel("tasks/" + slug + "/outputs/" + filename, task=slug)
        if os.path.isfile(source):
            shutil.copyfile(source, destination)
            copied.append(filename)
    lessons = safe_rel("tasks/" + slug + "/lessons.md", task=slug)
    with open(lessons, "a", encoding="utf-8") as f:
        f.write("\n## " + now() + "\n\n- 已归档：" + (", ".join(copied) if copied else "无新产物") + "\n")
    manifest_path = safe_rel("tasks/" + slug + "/manifest.json", task=slug)
    manifest = read_json(manifest_path, {"name": task, "slug": slug, "created": now()})
    manifest["updated"] = now()
    write_json(manifest_path, manifest)
    _update_task_index(slug, root)
    return {"task": slug, "copied": copied}


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_list = sub.add_parser("list")
    p_list.add_argument("--task", default=None)
    p_list.add_argument("--scope", choices=["shared", "task"], default=None)
    p_list.add_argument("--search", default=None)
    p_read = sub.add_parser("read")
    p_read.add_argument("path")
    p_read.add_argument("--task", default=None)
    p_write = sub.add_parser("write")
    p_write.add_argument("path")
    p_write.add_argument("--text", default=None)
    p_write.add_argument("--file", default=None)
    p_write.add_argument("--task", default=None)
    p_task = sub.add_parser("task-new")
    p_task.add_argument("name")
    sub.add_parser("sync-index")
    p_ctx = sub.add_parser("context")
    p_ctx.add_argument("task")
    p_arc = sub.add_parser("archive")
    p_arc.add_argument("task")
    args = ap.parse_args()

    if args.cmd == "list":
        print(json.dumps(list_entries(args.task, args.scope, args.search), ensure_ascii=False, indent=2))
    elif args.cmd == "read":
        path = safe_rel(args.path, task=args.task)
        if not os.path.exists(path):
            raise SystemExit("不存在：" + args.path)
        with open(path, encoding="utf-8") as f:
            print(f.read())
    elif args.cmd == "write":
        path = safe_rel(args.path, task=args.task)
        content = args.text
        if args.file:
            source_path = os.path.realpath(args.file)
            if args.task is not None and _within(source_path, os.path.realpath(KB)):
                source_path = safe_rel(os.path.relpath(source_path, KB), task=args.task)
            with open(source_path, encoding="utf-8") as f:
                content = f.read()
        if content is None:
            raise SystemExit("需要 --text 或 --file")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        if args.task is None:
            sync_index()
        else:
            slug, root = _task_root(args.task, require_exists=True)
            _update_task_index(slug, root)
        print(json.dumps({"ok": True, "path": os.path.relpath(path, KB)}, ensure_ascii=False))
    elif args.cmd == "task-new":
        print(json.dumps({"ok": True, "task": ensure_task(args.name)}, ensure_ascii=False))
    elif args.cmd == "sync-index":
        print(json.dumps({"ok": True, "entries": len(sync_index())}, ensure_ascii=False))
    elif args.cmd == "context":
        path = context_pack(args.task)
        print(json.dumps({"ok": True, "context": os.path.relpath(path, BASE)}, ensure_ascii=False))
    elif args.cmd == "archive":
        print(json.dumps({"ok": True, **archive(args.task)}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
