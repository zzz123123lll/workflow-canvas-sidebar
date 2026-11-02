"""Knowledge identities, immutable source revisions and explicit decisions."""
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import uuid
from datetime import datetime, timezone


def connect(base):
    directory = Path(base) / '.runs'
    directory.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(directory / 'knowledge.sqlite3', timeout=15)
    db.row_factory = sqlite3.Row
    if db.execute('PRAGMA user_version').fetchone()[0] > 1:
        db.close()
        raise ValueError('知识索引来自更新版本，拒绝以旧代码覆盖')
    db.execute('PRAGMA synchronous=FULL')
    db.executescript('''CREATE TABLE IF NOT EXISTS items(
        id TEXT PRIMARY KEY, path TEXT UNIQUE NOT NULL, revision INTEGER NOT NULL,
        sha256 TEXT NOT NULL, status TEXT NOT NULL, module TEXT, source TEXT, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS revisions(item_id TEXT, revision INTEGER, sha256 TEXT, source_path TEXT,
            parser TEXT, parsed_sha256 TEXT, PRIMARY KEY(item_id,revision));
        CREATE TABLE IF NOT EXISTS decisions(request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, result TEXT NOT NULL);''')
    db.execute('PRAGMA user_version=1')
    return db


def register(base, relative, full, parsed):
    db = connect(base)
    try:
        db.execute('BEGIN IMMEDIATE')
        previous = db.execute('SELECT * FROM items WHERE path=?', (relative,)).fetchone()
        identity = previous['id'] if previous else 'knowledge-' + uuid.uuid4().hex
        revision = previous['revision'] if previous else 0
        digest = parsed['file_sha256']
        if not previous or previous['sha256'] != digest:
            revision += 1
            destination = Path(base) / '.runs/knowledge-revisions' / identity / str(revision) / Path(full).name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(full, destination)
            if hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
                raise ValueError('知识原文件在建立版本时变化，请重新解析')
            status = 'draft'
            db.execute('INSERT OR REPLACE INTO items VALUES(?,?,?,?,?,?,?,?)',
                       (identity, relative, revision, digest, status,
                        previous['module'] if previous else None, previous['source'] if previous else None,
                        datetime.now(timezone.utc).isoformat()))
            db.execute('INSERT INTO revisions VALUES(?,?,?,?,?,?)', (identity, revision, digest,
                str(destination.relative_to(base)).replace('\\', '/'), 'canvas.local-parser/v2', parsed.get('content_sha256')))
        item = dict(db.execute('SELECT * FROM items WHERE id=?', (identity,)).fetchone())
        db.commit()
        return {'knowledge_id': identity, 'knowledge_revision': revision, 'knowledge_status': item['status'],
                'module_scope': item['module'], 'source_revision': json.loads(item['source']) if item['source'] else None,
                'parser': 'canvas.local-parser/v2'}
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def history(base, path):
    db = connect(base)
    try:
        item = db.execute('SELECT * FROM items WHERE path=?', (path,)).fetchone()
        if not item:
            return []
        return [dict(row) for row in db.execute('SELECT revision,sha256,source_path,parser,parsed_sha256 FROM revisions WHERE item_id=? ORDER BY revision', (item['id'],))]
    finally:
        db.close()


def decision(base, request_id):
    db = connect(base)
    try:
        row = db.execute('SELECT result FROM decisions WHERE request_id=?', (request_id,)).fetchone()
        return json.loads(row['result']) if row else None
    finally:
        db.close()


def decide(base, path, expected_revision, status, request_id, source=None, module=None):
    if status not in {'draft', 'confirmed', 'deprecated'} or not request_id:
        raise ValueError('请指定知识状态和命令标识')
    fingerprint = hashlib.sha256(json.dumps([path, expected_revision, status, source, module], sort_keys=True).encode()).hexdigest()
    db = connect(base)
    try:
        db.execute('BEGIN IMMEDIATE')
        old = db.execute('SELECT * FROM decisions WHERE request_id=?', (request_id,)).fetchone()
        if old:
            if old['fingerprint'] != fingerprint:
                raise ValueError('命令标识已用于其他知识决定')
            return json.loads(old['result'])
        item = db.execute('SELECT * FROM items WHERE path=?', (path,)).fetchone()
        if not item or item['revision'] != expected_revision:
            raise ValueError('知识版本已变化，请刷新并重新确认')
        effective_source = source if source is not None else (json.loads(item['source']) if item['source'] else None)
        effective_module = module if module is not None else item['module']
        db.execute('UPDATE items SET status=?,source=?,module=?,updated_at=? WHERE id=?',
                   (status, json.dumps(effective_source) if effective_source is not None else None, effective_module,
                    datetime.now(timezone.utc).isoformat(), item['id']))
        result = {'knowledge_id': item['id'], 'knowledge_revision': item['revision'], 'knowledge_status': status,
                  'path': path, 'source_revision': effective_source, 'module_scope': effective_module}
        db.execute('INSERT INTO decisions VALUES(?,?,?)', (request_id, fingerprint, json.dumps(result)))
        db.commit()
        return result
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def chunks(content, max_chars=1800):
    result, current, start, line = [], [], 1, 1
    for text in content.splitlines(keepends=True):
        # Split individual long lines too, keeping an honest line locator.
        for offset in range(0, len(text), max_chars):
            piece = text[offset:offset + max_chars]
            if current and sum(map(len, current)) + len(piece) > max_chars:
                result.append({'text': ''.join(current), 'line_start': start, 'line_end': line})
                current, start = [], line
            current.append(piece)
        line += 1
    if current:
        result.append({'text': ''.join(current), 'line_start': start, 'line_end': line - 1})
    return result
