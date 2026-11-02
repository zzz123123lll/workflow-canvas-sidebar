"""Durable command admission. Never replay a worker whose outcome is uncertain."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time


def schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS execution_commands(
        request_id TEXT PRIMARY KEY, project_id TEXT, run_id TEXT NOT NULL,
        action TEXT NOT NULL, payload TEXT NOT NULL, fingerprint TEXT NOT NULL,
        status TEXT NOT NULL, pid INTEGER, created REAL NOT NULL, updated REAL NOT NULL,
        result TEXT, error TEXT)''')


def connect(base):
    directory = Path(base) / '.runs'; directory.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(directory / 'projects.sqlite3', timeout=15)
    db.row_factory = sqlite3.Row
    if db.execute('PRAGMA user_version').fetchone()[0] > 2:
        db.close(); raise ValueError('项目数据库来自更新版本')
    db.execute('PRAGMA synchronous=FULL'); schema(db)
    return db


def enqueue(base, action, payload, db=None):
    own = db is None
    db = db or connect(base)
    try:
        if own: db.execute('BEGIN IMMEDIATE')
        request = str(payload.get('request_id') or '')
        if not request or len(request) > 160: raise ValueError('需要有效命令标识')
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        fingerprint = hashlib.sha256((action + ':' + encoded).encode()).hexdigest()
        previous = db.execute('SELECT * FROM execution_commands WHERE request_id=?', (request,)).fetchone()
        if previous:
            if previous['fingerprint'] != fingerprint: raise ValueError('命令标识已用于其他操作')
            return dict(previous)
        db.execute('INSERT INTO execution_commands VALUES(?,?,?,?,?,?,?,NULL,?,?,NULL,NULL)',
            (request, payload.get('project_id'), payload['run_id'], action, encoded, fingerprint, 'queued', time.time(), time.time()))
        result = dict(db.execute('SELECT * FROM execution_commands WHERE request_id=?', (request,)).fetchone())
        if own: db.commit()
        return result
    except BaseException:
        if own: db.rollback()
        raise
    finally:
        if own: db.close()


def pending(base, alive, project_id=None):
    db = connect(base)
    try:
        db.execute('BEGIN IMMEDIATE')
        for row in db.execute("SELECT * FROM execution_commands WHERE status='running'").fetchall():
            if not alive(row['pid']):
                db.execute("UPDATE execution_commands SET status='uncertain',error=?,updated=? WHERE request_id=?",
                    ('执行进程已退出，完成结果尚未确认；请同步会话并检查成果后恢复，系统不会自动重复运行模型', time.time(), row['request_id']))
        rows = db.execute("SELECT * FROM execution_commands WHERE status IN ('queued','running','uncertain','rejected')" +
                          (' AND project_id=?' if project_id else '') + ' ORDER BY created', (project_id,) if project_id else ()).fetchall()
        db.commit()
        return [dict(row) for row in rows]
    finally:
        db.close()


def lookup(base, request_id):
    db = connect(base)
    try:
        row = db.execute('SELECT * FROM execution_commands WHERE request_id=?', (request_id,)).fetchone()
        return dict(row) if row else None
    finally:
        db.close()


def execute_one(wf):
    db = connect(wf.BASE)
    try:
        first = db.execute("SELECT * FROM execution_commands WHERE status='queued' ORDER BY created LIMIT 1").fetchone()
        if not first or not wf.acquire_lock(first['run_id']): return False
        dispatched = False
        try:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM execution_commands WHERE request_id=?', (first['request_id'],)).fetchone()
            if row['status'] != 'queued': db.rollback(); return False
            db.execute("UPDATE execution_commands SET status='running',pid=?,updated=? WHERE request_id=?", (os.getpid(), time.time(), row['request_id']))
            db.commit()
            payload = json.loads(row['payload'])
            state = wf.read_json(Path(wf.run_dir(row['run_id'])) / 'state.json', {})
            # Check under the same global executor lock used for the mutation.
            if row['action'] != 'run':
                if payload.get('expected_state_revision') != int(state.get('state_revision', 0)):
                    raise ValueError('运行状态已变化，请刷新后重新提交')
                node = wf.node_state(state, payload.get('node')) if payload.get('node') else None
                if node and payload.get('expected_generation') != int(node.get('execution_generation', 1)):
                    raise ValueError('节点已有新版本，请刷新后重新提交')
            action = row['action']
            dispatched = True
            if action == 'run':
                result = wf.canvas_run(project_id=payload['project_id'], request_id=payload['request_id'], _lock_owned=True)
            elif action == 'reply':
                result = wf.canvas_run(resume_run=row['run_id'], reply={'node_id': payload['node'], 'answer': payload['answer']}, _lock_owned=True)
            elif action == 'revise':
                result = wf.canvas_revise(row['run_id'], payload['node'], payload['answer'], payload['request_id'], _lock_owned=True)
            elif action == 'discuss':
                from wf_discussion import discuss
                result = discuss(wf, row['run_id'], payload['node'], payload['answer'], payload['expected_generation'], payload['request_id'], _lock_owned=True)
            elif action in {'rerun', 'continue'}:
                result = wf.canvas_rerun(row['run_id'], payload.get('node'), payload.get('feedback'), _lock_owned=True)
            else:
                raise ValueError('未知命令')
            db.execute("UPDATE execution_commands SET status='settled',result=?,updated=? WHERE request_id=?", (json.dumps(result, ensure_ascii=False), time.time(), row['request_id']))
            db.commit()
            return True
        except Exception as exc:
            # Rejection before model dispatch is deterministic; otherwise leave
            # an explicit uncertainty for inspection rather than blind replay.
            db.execute("UPDATE execution_commands SET status=?,error=?,updated=? WHERE request_id=?", ('uncertain' if dispatched else 'rejected', str(exc)[:1200], time.time(), first['request_id']))
            db.commit()
            return False
        finally:
            wf.release_lock()
    finally:
        db.close()


if __name__ == '__main__':
    import wf
    execute_one(wf)
