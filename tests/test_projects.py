"""Verify transactional identities against the shipped server, using isolated data."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from wf_projects import ProjectStore, Conflict


def create(store, key='create', name='Same name'):
    return store.mutate('create', {'request_id': key, 'name': name, 'goal': 'Unique goal',
                                  'graph': {'nodes': [{'id': 'a', 'module': 'writer'}], 'edges': []}})['project']


def test_same_names_have_independent_ids_and_concurrent_command_is_once(tmp_path):
    store = ProjectStore(tmp_path)
    with ThreadPoolExecutor(6) as pool:
        projects = list(pool.map(lambda _: create(ProjectStore(tmp_path)), range(12)))
    assert len({p['project_id'] for p in projects}) == 1
    other = create(store, 'other')
    assert projects[0]['project_id'] != other['project_id']
    assert len(ProjectStore(tmp_path).list()) == 2


def test_stale_update_and_reused_request_id_cannot_overwrite(tmp_path):
    store = ProjectStore(tmp_path)
    p = create(store)
    body = {'project_id': p['project_id'], 'request_id': 'edit', 'expected_revision': 1, 'goal': 'Updated'}
    assert store.mutate('update', body)['project']['revision'] == 2
    assert store.mutate('update', body)['project']['revision'] == 2
    with pytest.raises(Conflict):
        store.mutate('update', dict(body, request_id='stale'))
    with pytest.raises(Conflict):
        store.mutate('update', dict(body, goal='Different'))
    assert store.get(p['project_id'])['goal'] == 'Updated'


def test_migration_preserves_original_bytes_and_is_reentrant(tmp_path):
    target = tmp_path / '.runs' / 'old' / 'state.json'
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({'run_id': 'old', 'nodes': [], 'episode': 'Same name'}), encoding='utf-8')
    before = hashlib.sha256(target.read_bytes()).hexdigest()
    store = ProjectStore(tmp_path)
    assert store.migrate()['migrated'] == 1
    assert store.migrate()['migrated'] == 0
    assert store.owner('old') == 'project-history'
    assert hashlib.sha256(target.read_bytes()).hexdigest() == before
    backup = tmp_path / 'backup.sqlite3'
    store.backup(backup)
    assert backup.is_file()


def test_run_admission_is_once_and_freezes_project_graph(tmp_path):
    store = ProjectStore(tmp_path)
    p = create(store)
    body = {'project_id': p['project_id'], 'request_id': 'start', 'expected_revision': 1, 'episode': 'Draft'}
    with ThreadPoolExecutor(6) as pool:
        accepted = list(pool.map(lambda _: ProjectStore(tmp_path).admit_run(body), range(12)))
    assert sum(fresh for _, fresh in accepted) == 1
    assert len({c['run_id'] for c, _ in accepted}) == 1
    command = accepted[0][0]
    store.mutate('update', {'project_id': p['project_id'], 'request_id': 'edit', 'expected_revision': 1,
                            'graph': {'nodes': [], 'edges': []}})
    assert store.run_command('start')['project']['graph']['nodes'] == [{'id': 'a', 'module': 'writer'}]
    other = create(store, 'other')
    with pytest.raises(Conflict):
        store.bind_run(other['project_id'], command['run_id'])
    assert store.owner(command['run_id']) == p['project_id']


def test_engine_uses_admitted_graph_and_isolates_same_named_project_outputs(tmp_path, monkeypatch):
    import importlib.util
    import types
    server = Path(__file__).resolve().parents[1] / 'server'
    def load(name):
        spec = importlib.util.spec_from_file_location('project_test_' + name, server / (name + '.py'))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    wf, kb = load('wf'), load('kb')
    for name, value in {'BASE': str(tmp_path), 'RUNS': str(tmp_path / '.runs'),
                        'LATEST': str(tmp_path / '.runs/LATEST'), 'LOCKFILE': str(tmp_path / '.runs/.lock'),
                        'MODULES_PATH': str(tmp_path / 'kb/modules.json'), 'CANVAS_PATH': str(tmp_path / 'kb/canvas-workflow.json'),
                        'DEF_PATH': str(tmp_path / 'kb/workflow-def.json')}.items():
        monkeypatch.setattr(wf, name, value)
    for name, value in {'BASE': str(tmp_path), 'KB': str(tmp_path / 'kb/knowledge'),
                        'SHARED': str(tmp_path / 'kb/knowledge/shared'), 'TASKS': str(tmp_path / 'kb/knowledge/tasks'),
                        'INDEX': str(tmp_path / 'kb/knowledge/index.json')}.items():
        monkeypatch.setattr(kb, name, value)
    monkeypatch.setitem(sys.modules, 'kb', kb)
    calls = []
    def execute(task, **kwargs):
        calls.append(task)
        for rel in task['expected_artifacts']:
            path = Path(kwargs['workspace']) / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(task['task_id'], encoding='utf-8')
        return {'status': 'completed', 'summary': 'fixture', 'artifacts': task['expected_artifacts'], 'checks': [True]}
    monkeypatch.setitem(sys.modules, 'wf_agent', types.SimpleNamespace(run_agent=execute))
    wf.write_json(wf.DEF_PATH, {'retention': {'auto': False}})
    wf.write_json(wf.MODULES_PATH, {'modules': [{'id': 'writer', 'name': 'Writer', 'executor': 'agent',
        'agent_prompt': 'Write', 'retry': 0, 'artifacts': ['episodes/{stem}.md'], 'checklist': ['exists']}]})
    # A conflicting global graph must never become the project's graph.
    wf.write_json(wf.CANVAS_PATH, {'nodes': [{'id': 'wrong', 'module': 'missing'}], 'edges': []})
    store = ProjectStore(tmp_path)
    states = []
    for key in ['one', 'two']:
        p = create(store, key)
        command, _ = store.admit_run({'project_id': p['project_id'], 'request_id': key + '-run', 'expected_revision': 1, 'episode': 'Same title'})
        assert wf.canvas_run(project_id=p['project_id'], request_id=key + '-run') == 0
        assert wf.canvas_run(project_id=p['project_id'], request_id=key + '-run') == 0
        state = wf.read_json(tmp_path / '.runs' / command['run_id'] / 'state.json')
        assert state['status'] == 'completed'
        assert state['nodes'][0]['id'] == 'a'
        assert state['project_id'] == p['project_id']
        assert state['task_id'] == p['project_id']
        states.append(state)
    assert len(calls) == 2
    assert states[0]['episode'] != states[1]['episode']
    assert 'Unique goal' in calls[0]['objective']
