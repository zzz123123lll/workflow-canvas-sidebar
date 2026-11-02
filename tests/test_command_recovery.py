import importlib.util
import json
from pathlib import Path
import sys

import pytest

SERVER = Path(__file__).resolve().parents[1] / 'server'
sys.path.insert(0, str(SERVER))
import wf_commands as commands
from wf_projects import ProjectStore


@pytest.fixture
def engine(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('command_recovery_wf', SERVER / 'wf.py')
    wf = importlib.util.module_from_spec(spec); spec.loader.exec_module(wf)
    for key, rel in {'BASE': '', 'RUNS': '.runs', 'LOCKFILE': '.runs/.lock'}.items():
        monkeypatch.setattr(wf, key, str(tmp_path / rel))
    ProjectStore(tmp_path)
    wf.write_json(tmp_path / '.runs/r/state.json', {'run_id': 'r', 'state_revision': 4,
        'nodes': [{'id': 'n', 'execution_generation': 2}]})
    return wf


def payload(request='once'):
    return {'request_id': request, 'run_id': 'r', 'node': 'n', 'answer': 'continue',
            'expected_generation': 2, 'expected_state_revision': 4}


def test_admission_survives_server_restart_and_busy_executor(engine, monkeypatch):
    calls = []
    monkeypatch.setattr(engine, 'canvas_run', lambda **kw: calls.append(kw) or 0)
    assert commands.enqueue(engine.BASE, 'reply', payload())['status'] == 'queued'
    assert engine.acquire_lock('other')
    assert commands.execute_one(engine) is False
    engine.release_lock()
    assert commands.execute_one(engine) is True
    assert commands.execute_one(engine) is False
    assert len(calls) == 1 and calls[0]['_lock_owned']
    assert commands.enqueue(engine.BASE, 'reply', payload())['status'] == 'settled'
    with pytest.raises(ValueError, match='标识'):
        commands.enqueue(engine.BASE, 'reply', dict(payload(), answer='different'))


def test_stale_state_cannot_start_model(engine, monkeypatch):
    calls = []
    monkeypatch.setattr(engine, 'canvas_run', lambda **kw: calls.append(kw))
    commands.enqueue(engine.BASE, 'reply', dict(payload(), expected_state_revision=3))
    assert not commands.execute_one(engine)
    assert not calls
    assert commands.pending(engine.BASE, lambda pid: False)[0]['status'] == 'rejected'


def test_dead_worker_is_uncertain_and_never_replayed(engine, monkeypatch):
    commands.enqueue(engine.BASE, 'reply', payload())
    db = commands.connect(engine.BASE)
    db.execute("UPDATE execution_commands SET status='running',pid=1234"); db.commit(); db.close()
    assert commands.pending(engine.BASE, lambda pid: False)[0]['status'] == 'uncertain'
    assert not commands.execute_one(engine)


def test_project_run_and_command_commit_together(tmp_path):
    store = ProjectStore(tmp_path)
    project = store.mutate('create', {'name': 'P', 'goal': 'G', 'request_id': 'create',
        'graph': {'nodes': [{'id': 'n', 'module': 'm'}], 'edges': []}})['project']
    command, fresh = store.admit_run({'project_id': project['project_id'], 'request_id': 'start', 'expected_revision': 1})
    assert fresh
    rows = commands.pending(tmp_path, lambda pid: False)
    assert rows[0]['run_id'] == command['run_id'] and rows[0]['status'] == 'queued'
    assert store.admit_run({'project_id': project['project_id'], 'request_id': 'start', 'expected_revision': 1})[1] is False
    assert len(commands.pending(tmp_path, lambda pid: False)) == 1


def test_api_retries_find_durable_admission_even_without_json_cache(engine, monkeypatch):
    spec = importlib.util.spec_from_file_location('command_api_test', SERVER / 'canvas_editor_server.py')
    api = importlib.util.module_from_spec(spec); spec.loader.exec_module(api)
    monkeypatch.setattr(api, 'BASE', engine.BASE)
    monkeypatch.setattr(api, 'RUNS', engine.RUNS)
    saved = dict(payload(), answer='the answer')
    commands.enqueue(engine.BASE, 'reply', saved)
    # A newer state and absent response cache model the crash window after the
    # accepted command already started. This retry must never start another.
    engine.write_json(Path(engine.run_dir('r')) / 'state.json', {'run_id': 'r', 'state_revision': 6,
        'status': 'completed', 'nodes': [{'id': 'n', 'execution_generation': 3}]})
    result = api.submit_node_command('reply', saved)
    assert result['status'] == 'queued' and result['previous_generation'] == 2
    with pytest.raises(ValueError, match='标识'):
        api.submit_node_command('reply', dict(saved, answer='another answer'))
