"""Transactional project identities and run ownership; large files stay on disk.

Legacy run JSON remains the execution authority. This database owns only project
metadata, ownership and command admission; migration never rewrites old evidence.
"""
import contextlib
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid


class Conflict(ValueError):
    pass


def stamp():
    return datetime.now(timezone.utc).isoformat()


class ProjectStore:
    def __init__(self, base):
        self.base = Path(base).resolve()
        self.path = self.base / '.runs' / 'projects.sqlite3'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version > 2:
                raise ValueError('项目数据库版本较新，请使用对应插件版本；未修改数据')
            from wf_commands import schema
            schema(db)
            db.executescript('''
                CREATE TABLE IF NOT EXISTS projects(
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, goal TEXT NOT NULL,
                    delivery TEXT NOT NULL, revision INTEGER NOT NULL,
                    archived INTEGER NOT NULL DEFAULT 0, graph TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS run_projects(
                    run_id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                    original_sha256 TEXT, migrated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS commands(
                    request_id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                    operation TEXT NOT NULL, fingerprint TEXT NOT NULL, result TEXT NOT NULL,
                    created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS session_cursors(
                    run_id TEXT NOT NULL, node_id TEXT NOT NULL, session_id TEXT NOT NULL,
                    cursor INTEGER NOT NULL, PRIMARY KEY(run_id,node_id,session_id));
                CREATE TABLE IF NOT EXISTS project_work(
                    project_id TEXT PRIMARY KEY REFERENCES projects(id),
                    work_run_id TEXT, delivery_run_id TEXT, work_revision INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS run_dispositions(
                    run_id TEXT PRIMARY KEY REFERENCES run_projects(run_id),
                    replacement_run_id TEXT REFERENCES run_projects(run_id),
                    reason TEXT NOT NULL, decided_at TEXT NOT NULL);
                PRAGMA user_version=2;
            ''')
            if 'work_revision' not in {r['name'] for r in db.execute('PRAGMA table_info(project_work)')}:
                try:db.execute('ALTER TABLE project_work ADD COLUMN work_revision INTEGER NOT NULL DEFAULT 0')
                except sqlite3.OperationalError:
                    if 'work_revision' not in {r['name'] for r in db.execute('PRAGMA table_info(project_work)')}:raise

    @contextlib.contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA synchronous=FULL')
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def unpack(row):
        if row is None:
            raise ValueError('项目不存在')
        result = dict(row)
        result['project_id'] = result.pop('id')
        result['graph'] = json.loads(result['graph'])
        result['archived'] = bool(result['archived'])
        return result

    def get(self, project_id, db=None):
        if db is not None:
            project = self.unpack(db.execute('SELECT * FROM projects WHERE id=?', (project_id,)).fetchone())
            work = db.execute('SELECT work_run_id,delivery_run_id,work_revision FROM project_work WHERE project_id=?', (project_id,)).fetchone()
            project.update(dict(work) if work else {'work_run_id': None, 'delivery_run_id': None, 'work_revision': 0})
            return project
        with self.connect() as connection:
            return self.get(project_id, connection)

    def list(self):
        with self.connect() as db:
            return [self.unpack(r) for r in db.execute('SELECT * FROM projects ORDER BY created_at DESC,id')]

    def mutate(self, operation, body):
        request_id = str(body.get('request_id') or '').strip()
        if not request_id or len(request_id) > 160:
            raise ValueError('缺少有效 request_id')
        fingerprint = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM commands WHERE request_id=?', (request_id,)).fetchone()
            if old:
                if old['operation'] != operation or old['fingerprint'] != fingerprint:
                    raise Conflict('request_id 已用于另一条命令')
                return json.loads(old['result'])
            project_id = str(body.get('project_id') or '')
            if operation == 'create':
                project_id = 'project-' + uuid.uuid4().hex
                graph = body.get('graph') or {'nodes': [], 'edges': []}
                if not isinstance(graph, dict) or not isinstance(graph.get('nodes'), list) or not isinstance(graph.get('edges'), list):
                    raise ValueError('流程须包含 nodes 和 edges')
                name = str(body.get('name') or '').strip()
                goal = str(body.get('goal') or '').strip()
                delivery = str(body.get('delivery') or '').strip()
                if not name or len(name) > 80 or not goal or len(goal) > 20000 or len(delivery) > 5000:
                    raise ValueError('请填写项目名称（1–80字）、目标（1–20000字）和交付要求（最多5000字）')
                now = stamp()
                db.execute('INSERT INTO projects VALUES(?,?,?,?,1,0,?,?,?)',
                           (project_id, name, goal, delivery, json.dumps(graph, ensure_ascii=False), now, now))
            elif operation in {'set-work-run', 'set-delivery-run', 'supersede-run'}:
                project = self.get(project_id, db)
                if body.get('expected_revision') != project['revision']:
                    raise Conflict('项目已更新，请刷新后再操作')
                if body.get('expected_work_revision') != project['work_revision']:
                    raise Conflict('当前工作或交付选择已更新，请刷新项目后再操作')
                run_id = str(body.get('run_id') or '')
                owned = db.execute('SELECT project_id FROM run_projects WHERE run_id=?', (run_id,)).fetchone()
                if not owned or owned[0] != project_id:
                    raise Conflict('运行不属于当前项目')
                state_path = self.base / '.runs' / run_id / 'state.json'
                state = json.loads(state_path.read_text(encoding='utf-8'))
                if state.get('run_kind') == 'workflow_proposal':
                    raise ValueError('流程建议不是业务工作运行或交付版本')
                if operation == 'supersede-run':
                    replacement = str(body.get('replacement_run_id') or '')
                    owner = db.execute('SELECT project_id FROM run_projects WHERE run_id=?', (replacement,)).fetchone()
                    reason = str(body.get('reason') or '').strip()
                    if replacement == run_id or not owner or owner[0] != project_id or not reason:
                        raise ValueError('请选择本项目另一运行并说明替代理由')
                    replacing = json.loads((self.base / '.runs' / replacement / 'state.json').read_text(encoding='utf-8'))
                    if replacing.get('run_kind') == 'workflow_proposal':
                        raise ValueError('不能用流程建议替代业务运行')
                    if state.get('status') in {'running', 'stopping', 'pausing'}:
                        raise Conflict('请先停止活动运行，再登记替代关系')
                    db.execute('INSERT INTO run_dispositions VALUES(?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET replacement_run_id=excluded.replacement_run_id,reason=excluded.reason,decided_at=excluded.decided_at',
                               (run_id, replacement, reason, stamp()))
                else:
                    column = 'work_run_id' if operation == 'set-work-run' else 'delivery_run_id'
                    db.execute('INSERT OR IGNORE INTO project_work(project_id) VALUES(?)', (project_id,))
                    db.execute('UPDATE project_work SET '+column+'=? WHERE project_id=?', (run_id, project_id))
                    if operation == 'set-work-run':db.execute('DELETE FROM run_dispositions WHERE run_id=?', (run_id,))
                db.execute('INSERT OR IGNORE INTO project_work(project_id) VALUES(?)', (project_id,))
                db.execute('UPDATE project_work SET work_revision=work_revision+1 WHERE project_id=?', (project_id,))
                db.execute('UPDATE projects SET revision=revision+1,updated_at=? WHERE id=?', (stamp(), project_id))
            elif operation in {'update', 'archive'}:
                project = self.get(project_id, db)
                if body.get('expected_revision') != project['revision']:
                    raise Conflict('项目已更新，请刷新后再操作；当前版本 ' + str(project['revision']))
                name = str(body.get('name', project['name'])).strip()
                goal = str(body.get('goal', project['goal'])).strip()
                delivery = str(body.get('delivery', project['delivery'])).strip()
                graph = body.get('graph', project['graph'])
                if not name or len(name) > 80 or not goal or len(goal) > 20000 or len(delivery) > 5000:
                    raise ValueError('项目名称、目标或交付要求长度无效')
                if not isinstance(graph, dict) or not isinstance(graph.get('nodes'), list) or not isinstance(graph.get('edges'), list):
                    raise ValueError('流程须包含 nodes 和 edges')
                db.execute('UPDATE projects SET name=?,goal=?,delivery=?,graph=?,archived=?,revision=revision+1,updated_at=? WHERE id=?',
                           (name, goal, delivery, json.dumps(graph, ensure_ascii=False),
                            int(operation == 'archive' or project['archived']), stamp(), project_id))
            else:
                raise ValueError('未知项目命令')
            result = {'ok': True, 'project': self.get(project_id, db), 'request_id': request_id}
            if operation in {'set-work-run','set-delivery-run','supersede-run'}:
                result['decision'] = {key:body[key] for key in ['run_id','replacement_run_id','reason'] if key in body}
            db.execute('INSERT INTO commands VALUES(?,?,?,?,?,?)',
                       (request_id, project_id, operation, fingerprint, json.dumps(result, ensure_ascii=False), stamp()))
            return result

    def bind_run(self, project_id, run_id, original_sha256=None):
        if not run_id or Path(run_id).name != run_id or any(c in run_id for c in '/\\:'):
            raise ValueError('无效运行 ID')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self.get(project_id, db)
            old = db.execute('SELECT project_id FROM run_projects WHERE run_id=?', (run_id,)).fetchone()
            if old and old[0] != project_id:
                raise Conflict('运行已归属其他项目')
            db.execute('INSERT OR IGNORE INTO run_projects VALUES(?,?,?,?)', (run_id, project_id, original_sha256, stamp()))

    def owner(self, run_id):
        with self.connect() as db:
            row = db.execute('SELECT project_id FROM run_projects WHERE run_id=?', (run_id,)).fetchone()
            return row[0] if row else None

    def admit_run(self, body, *, graph_override=None, purpose=None):
        request_id = str(body.get('request_id') or '').strip()
        if not request_id or len(request_id) > 160:
            raise ValueError('缺少有效 request_id')
        fingerprint = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT * FROM commands WHERE request_id=?', (request_id,)).fetchone()
            if previous:
                if previous['operation'] != 'run' or previous['fingerprint'] != fingerprint:
                    raise Conflict('request_id 已用于另一条命令')
                return json.loads(previous['result']), False
            project = self.get(str(body.get('project_id') or ''), db)
            if project['archived'] or project['project_id'] == 'project-history':
                raise Conflict('此项目只供查看历史，请创建新项目执行')
            if body.get('expected_revision') != project['revision']:
                raise Conflict('项目已更新，请刷新并确认流程后运行')
            execution_project = dict(project, graph=graph_override) if graph_override is not None else project
            if not execution_project['graph']['nodes']:
                raise ValueError('请先在项目流程中添加模块并保存')
            run_id = 'run-' + uuid.uuid4().hex
            result = {'ok': True, 'project_id': project['project_id'], 'run_id': run_id,
                      'request_id': request_id, 'project_revision': project['revision'],
                      'project': execution_project, 'episode': str(body.get('episode') or project['name']),
                      'until': body.get('until'), 'status': 'accepted', 'purpose': purpose}
            db.execute('INSERT INTO commands VALUES(?,?,?,?,?,?)',
                       (request_id, project['project_id'], 'run', fingerprint, json.dumps(result, ensure_ascii=False), stamp()))
            db.execute('INSERT INTO run_projects VALUES(?,?,NULL,?)', (run_id, project['project_id'], stamp()))
            from wf_commands import enqueue
            enqueue(self.base, 'run', {key: result[key] for key in ('request_id', 'project_id', 'run_id')}, db=db)
            if purpose != 'workflow_proposal':
                db.execute('INSERT INTO project_work(project_id,work_run_id,work_revision) VALUES(?,?,1) ON CONFLICT(project_id) DO UPDATE SET work_run_id=excluded.work_run_id,work_revision=work_revision+1',
                           (project['project_id'], run_id))
                result['work_revision'] = self.get(project['project_id'], db)['work_revision']
                db.execute('UPDATE commands SET result=? WHERE request_id=?', (json.dumps(result,ensure_ascii=False),request_id))
            return result, True

    def current_run(self, project_id):
        project = self.get(project_id)
        runs = self.runs(project_id)
        rid = project.get('work_run_id')
        if not rid:
            return next((r for r in runs if r.get('run_kind') != 'workflow_proposal'), None)
        current = next((r for r in runs if r['run_id'] == rid), None)
        if current:return current
        with self.connect() as db:
            row = db.execute("SELECT c.result,e.status,e.error FROM commands c JOIN execution_commands e ON c.request_id=e.request_id WHERE e.run_id=? AND c.operation='run'", (rid,)).fetchone()
        if row:
            command = json.loads(row['result'])
            return {'run_id':rid,'project_id':project_id,'episode':command.get('episode') or project['name'],
                    'status':'queued' if row['status'] in {'queued','running'} else 'awaiting_input',
                    'nodes':[],'admission_only':True,'detail':row['error'] or '命令已受理，执行器正在建立工作区'}
        return {'run_id':rid,'project_id':project_id,'episode':project['name'],'status':'failed','nodes':[],
                'admission_only':True,'detail':'当前工作运行记录缺失，请从历史选择可恢复的运行'}

    def dispositions(self, project_id):
        with self.connect() as db:
            return {row['run_id']: dict(row) for row in db.execute(
                'SELECT d.* FROM run_dispositions d JOIN run_projects r ON r.run_id=d.run_id WHERE r.project_id=?', (project_id,))}

    def run_command(self, request_id):
        with self.connect() as db:
            row = db.execute("SELECT result FROM commands WHERE request_id=? AND operation='run'", (request_id,)).fetchone()
            if not row:
                raise ValueError('运行命令不存在')
            return json.loads(row[0])

    def session_cursor(self, run_id, node_id, session_id, cursor=None):
        with self.connect() as db:
            if cursor is not None:
                db.execute('INSERT INTO session_cursors VALUES(?,?,?,?) ON CONFLICT(run_id,node_id,session_id) DO UPDATE SET cursor=MAX(cursor,excluded.cursor)',
                           (run_id, node_id, session_id, int(cursor)))
            row = db.execute('SELECT cursor FROM session_cursors WHERE run_id=? AND node_id=? AND session_id=?',
                             (run_id, node_id, session_id)).fetchone()
            return row[0] if row else -1

    def runs(self, project_id):
        self.get(project_id)
        with self.connect() as db:
            ids = [r[0] for r in db.execute('SELECT run_id FROM run_projects WHERE project_id=? ORDER BY run_id DESC', (project_id,))]
        rows = []
        for run_id in ids:
            try:
                state = json.loads((self.base / '.runs' / run_id / 'state.json').read_text(encoding='utf-8'))
            except (OSError, ValueError):
                continue
            state['project_id'] = project_id
            rows.append(state)
        return sorted(rows, key=lambda r: (r.get('created_at') or '', r['run_id']), reverse=True)

    def migrate(self):
        """Reentrant, conservative migration. Unknown associations stay unclassified."""
        count = 0
        for path in sorted((self.base / '.runs').glob('*/state.json')):
            if path.parent.is_symlink() or path.is_symlink():
                continue
            raw = path.read_bytes()
            try:
                state = json.loads(raw)
            except ValueError:
                continue
            run_id = path.parent.name
            if state.get('run_id') != run_id or not isinstance(state.get('nodes'), list) or self.owner(run_id):
                continue
            if not any(p['project_id'] == 'project-history' for p in self.list()):
                with self.connect() as db:
                    now = stamp()
                    db.execute("INSERT OR IGNORE INTO projects VALUES('project-history',?,?,?,1,0,?,?,?)",
                               ('历史项目待归类', '保留原始运行，待核实归属', '', '{"nodes":[],"edges":[]}', now, now))
            self.bind_run('project-history', run_id, hashlib.sha256(raw).hexdigest())
            count += 1
        return {'migrated': count, 'total': sum(len(self.runs(p['project_id'])) for p in self.list())}

    def backup(self, destination):
        with self.connect() as db, sqlite3.connect(destination) as target:
            db.backup(target)
