"""Append-only public workflow messages, isolated by run, node and generation.

This store contains public task messages and summaries, never model reasoning.
The process lock is also shared with readers, so a refresh cannot see half a row.
"""
import contextlib
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import uuid

_LOCK = threading.RLock()


def _component(value):
    value = str(value or '')
    if (not value.strip() or value != value.strip() or value in {'.','..'}
            or any(c in value for c in '/\\:\x00') or value.endswith(('.', ' '))
            or any(ord(c) < 32 for c in value)
            or re.match(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', value, re.I)):
        raise ValueError('Invalid conversation identity')
    return value


def _path(run_id, base):
    root = Path(base or os.environ.get('CANVAS_HOME') or Path(__file__).resolve().parent)
    target = root / '.runs' / _component(run_id) / 'conversation.jsonl'
    for part in (target, *target.parents):
        if part.exists() or part.is_symlink():
            st = part.lstat()
            if part.is_symlink() or getattr(st, 'st_file_attributes', 0) & 0x400:
                raise ValueError('Conversation paths cannot contain links')
        if part == root:
            break
    return target


@contextlib.contextmanager
def _locked(path):
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = path.with_suffix('.lock')
        if lock_path.is_symlink():
            raise ValueError('Conversation lock cannot be a link')
        with open(lock_path, 'a+b') as handle:
            handle.seek(0, 2)
            if handle.tell() == 0:
                handle.write(b'0'); handle.flush()
            handle.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == 'nt':
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _rows(path):
    if not path.is_file():
        return []
    result = []
    with path.open(encoding='utf-8') as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                # A corrupt history must not silently become a fabricated conversation.
                raise ValueError('Conversation log contains an incomplete or invalid row')
            if not isinstance(row, dict):
                raise ValueError('Invalid conversation row')
            result.append(row)
    return result


def append_message(run_id, node_id, generation, role, text, kind='message', session_id='',
                   source='workflow', key=None, base=None):
    run_id, node_id = _component(run_id), _component(node_id)
    generation = int(generation)
    if generation < 1 or role not in {'user','assistant','system'}:
        raise ValueError('Invalid conversation message')
    text = str(text or '').strip()
    if not text or len(text) > 200000:
        raise ValueError('Conversation text must be 1..200000 characters')
    path = _path(run_id, base)
    with _locked(path):
        rows = _rows(path)
        for previous in rows:
            if key is not None and previous.get('node_id') == node_id and previous.get('generation') == generation and previous.get('key') == str(key):
                return previous
        row = {'id':uuid.uuid4().hex, 'seq':max((int(r.get('seq',0)) for r in rows),default=0)+1,
               'run_id':run_id, 'node_id':node_id, 'generation':generation, 'role':role,
               'kind':str(kind), 'text':text, 'at':datetime.now(timezone.utc).isoformat(),
               'session_id':str(session_id or ''), 'source':str(source), 'key':str(key) if key is not None else None}
        with path.open('a',encoding='utf-8',newline='\n') as handle:
            handle.write(json.dumps(row,ensure_ascii=False)+'\n'); handle.flush(); os.fsync(handle.fileno())
        return row


def read_messages(run_id, node_id, generation=None, base=None, offset=0, limit=200):
    node_id = _component(node_id)
    path = _path(run_id, base)
    if not path.is_file():
        return []
    with _locked(path):
        rows = [r for r in _rows(path) if r.get('node_id') == node_id
                and (generation is None or r.get('generation') == int(generation))]
    if limit is None:
        return rows[max(0, int(offset)):]
    offset = max(0,int(offset)); limit = max(1,min(1000,int(limit)))
    return rows[offset:offset+limit]


def message_page(run_id, node_id, generation=None, base=None, before_seq=None, after_seq=None, limit=100):
    rows = read_messages(run_id, node_id, generation, base, limit=None)
    limit = max(1, min(200, int(limit)))
    eligible = [r for r in rows if (before_seq is None or r['seq'] < int(before_seq))
                and (after_seq is None or r['seq'] > int(after_seq))]
    page = eligible[:limit] if after_seq is not None else eligible[-limit:]
    return {'messages': page, 'total': len(rows),
            'has_older': bool(page and any(r['seq'] < page[0]['seq'] for r in rows)),
            'has_newer': bool(page and any(r['seq'] > page[-1]['seq'] for r in rows)),
            'first_seq': page[0]['seq'] if page else None, 'last_seq': page[-1]['seq'] if page else None}


def stage_context(run_id, node_id, workspace, base=None):
    """Stage the complete public history; no model summary substitutes for it."""
    rows = read_messages(run_id, node_id, base=base, limit=None)
    target = Path(workspace) / 'conversation' / 'history.jsonl'
    if target.parent.is_symlink() or target.is_symlink():
        raise ValueError('Conversation context cannot be a link')
    target.parent.mkdir(parents=True, exist_ok=True)
    raw = ''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in rows)
    temporary = target.with_name(target.name + '.tmp-' + uuid.uuid4().hex)
    temporary.write_text(raw, encoding='utf-8')
    os.replace(temporary, target)
    messages = [{k: r.get(k) for k in ('id', 'seq', 'role', 'text', 'kind')}
                for r in rows if r.get('kind') != 'task']
    return {'messages': messages, 'path': 'conversation/history.jsonl', 'count': len(rows),
            'sha256': hashlib.sha256(raw.encode()).hexdigest()}
