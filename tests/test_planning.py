import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import types

import pytest

SERVER = Path(__file__).resolve().parents[1] / 'server'
sys.path.insert(0, str(SERVER))
from wf_projects import ProjectStore, Conflict
from wf_planning import suggest, proposal, apply


def test_proposal_is_separate_until_validated_and_explicitly_applied(tmp_path, monkeypatch):
    monkeypatch.setenv('CANVAS_HOME', str(tmp_path))
    spec = importlib.util.spec_from_file_location('planning_wf', SERVER / 'wf.py')
    wf = importlib.util.module_from_spec(spec); spec.loader.exec_module(wf)
    for name, rel in {'BASE': '', 'RUNS': '.runs', 'LOCKFILE': '.runs/.lock',
                      'DEF_PATH': 'kb/workflow-def.json', 'MODULES_PATH': 'kb/modules.json',
                      'CANVAS_PATH': 'kb/canvas-workflow.json', 'LATEST': '.runs/LATEST'}.items():
        monkeypatch.setattr(wf, name, str(tmp_path / rel))
    import kb
    for name, rel in {'BASE': '', 'KB': 'kb/knowledge', 'SHARED': 'kb/knowledge/shared',
                      'TASKS': 'kb/knowledge/tasks', 'INDEX': 'kb/knowledge/index.json'}.items():
        monkeypatch.setattr(kb, name, str(tmp_path / rel))
    wf.write_json(wf.DEF_PATH, {'retention': {'auto': False}})
    wf.write_json(wf.MODULES_PATH, {'modules': [{'id': 'writer', 'name': 'Writer', 'executor': 'agent',
        'artifacts': ['episodes/{stem}-result.md'], 'agent_prompt': 'Write', 'checklist': []}]})
    store = ProjectStore(tmp_path)
    p = store.mutate('create', {'name': 'Goal', 'goal': 'Write a note', 'request_id': 'create',
                              'graph': {'nodes': [], 'edges': []}})['project']
    body = {'project_id': p['project_id'], 'expected_revision': 1, 'request_id': 'suggest-once'}
    command, fresh = suggest(store, wf, body)
    assert fresh and suggest(store, wf, body)[1] is False
    assert store.get(p['project_id'])['graph']['nodes'] == []
    graph = {'nodes': [{'id': 'draft', 'module': 'writer', 'name': 'Draft', 'x': 0, 'y': 0,
                        'artifacts': ['episodes/{stem}-result.md']}], 'edges': []}
    calls = []
    def execute(task, **kwargs):
        calls.append(task)
        output = Path(kwargs['workspace']) / task['expected_artifacts'][0]
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps({'graph': graph, 'explanation': 'One real writing step', 'capability_gaps': []}))
        return {'status': 'completed', 'summary': 'fixture suggestion', 'artifacts': task['expected_artifacts'], 'checks': [True, True]}
    monkeypatch.setitem(sys.modules, 'wf_agent', types.SimpleNamespace(run_agent=execute))
    assert wf.canvas_run(project_id=p['project_id'], request_id='suggest-once') == 0
    rid = command['run_id']
    preview = proposal(tmp_path, store, wf, p['project_id'], rid)
    assert preview['can_apply'] and len(calls) == 1
    assert store.get(p['project_id'])['graph']['nodes'] == []
    adoption = {'project_id': p['project_id'], 'run_id': rid, 'sha256': preview['sha256'],
                'expected_revision': 1, 'request_id': 'apply-once'}
    result = apply(tmp_path, store, wf, adoption)
    assert result['project']['graph']['nodes'] == graph['nodes']
    assert result['project']['graph']['proposal_source']['sha256'] == preview['sha256']
    assert apply(tmp_path, store, wf, adoption) == result
    assert len(calls) == 1
    with pytest.raises(Conflict):
        apply(tmp_path, store, wf, dict(adoption, request_id='stale-apply'))
    state = wf.read_json(Path(wf.run_dir(rid)) / 'state.json')
    evidence = state['nodes'][0]['artifact_evidence'][0]
    frozen = tmp_path / evidence['path']
    invalid = {'graph': {'nodes': [{'id': 'fake', 'module': 'invented-module'}], 'edges': []},
               'explanation': 'Bad proposal', 'capability_gaps': []}
    frozen.write_text(json.dumps(invalid))
    with pytest.raises(ValueError, match='变化'):
        proposal(tmp_path, store, wf, p['project_id'], rid)
    assert apply(tmp_path, store, wf, adoption) == result
    evidence['sha256'] = hashlib.sha256(frozen.read_bytes()).hexdigest()
    wf.write_json(Path(wf.run_dir(rid)) / 'state.json', state)
    invalid_preview = proposal(tmp_path, store, wf, p['project_id'], rid)
    assert not invalid_preview['can_apply'] and 'invented-module' in invalid_preview['errors'][0]


def test_api_socket_is_exclusive(tmp_path, monkeypatch):
    monkeypatch.setenv('CANVAS_HOME', str(tmp_path))
    spec = importlib.util.spec_from_file_location('exclusive_canvas_api', SERVER / 'canvas_editor_server.py')
    api = importlib.util.module_from_spec(spec); spec.loader.exec_module(api)
    first = api.CanvasHTTPServer(('127.0.0.1', 0), api.Handler)
    try:
        with pytest.raises(OSError):
            api.CanvasHTTPServer(first.server_address, api.Handler)
    finally:
        first.server_close()
