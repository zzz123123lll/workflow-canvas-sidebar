import copy
import importlib.util
import json
from pathlib import Path
import sys

import pytest

SERVER = Path(__file__).resolve().parents[1] / 'server'
sys.path.insert(0, str(SERVER))
from wf_external import adopt, review


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('external_test_wf', SERVER / 'wf.py')
    wf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wf)
    for key, path in {'BASE': tmp_path, 'RUNS': tmp_path / '.runs', 'LOCKFILE': tmp_path / '.runs/.lock'}.items():
        monkeypatch.setattr(wf, key, str(path))
    run_id = 'test-run'
    workspace = tmp_path / '.runs/test-run/nodes/writer/generation-1/workspace'
    target = workspace / 'episodes/output.md'
    target.parent.mkdir(parents=True)
    target.write_text('original', encoding='utf-8')
    wf._copy_scoped_file(target, tmp_path / 'episodes/output.md')
    node = {'id': 'writer', 'module': 'writer', 'status': 'passed', 'execution_generation': 1,
            'checks': [True], 'workspace': str(workspace),
            'artifact_evidence': [{'path': 'episodes/output.md', **wf._file_evidence(target)}]}
    state = {'run_id': run_id, 'episode': 'test', 'status': 'completed', 'nodes': [node,
             {'id': 'dependent', 'module': 'copy', 'status': 'passed'},
             {'id': 'independent', 'module': 'other', 'status': 'passed'}]}
    node['artifact_evidence'] = wf._freeze_evidence(state, node)
    wf.write_json(tmp_path / '.runs/test-run/state.json', state)
    wf.write_json(tmp_path / '.runs/test-run/graph.json', {'nodes': [{'id': 'writer', 'module': 'writer'},
        {'id': 'dependent', 'module': 'copy'}, {'id': 'independent', 'module': 'other'}],
        'edges': [{'from': 'writer', 'to': 'dependent'}]})
    wf.write_json(tmp_path / '.runs/test-run/modules.json', {'modules': [{'id': 'writer', 'artifacts': ['episodes/output.md']}]})
    return wf, state, target


def test_native_changes_preserve_history_and_only_stale_dependents(fixture):
    wf, original, target = fixture
    old_evidence = copy.deepcopy(original['nodes'][0]['artifact_evidence'])
    target.write_text('original\nNATIVE-527', encoding='utf-8')
    hashes = {'episodes/output.md': wf._file_evidence(target)['sha256']}
    result = adopt(wf, 'test-run', 'writer', 1, hashes, 'adopt-once')
    assert result['generation'] == 2 and result['affected'] == ['dependent']
    assert adopt(wf, 'test-run', 'writer', 1, hashes, 'adopt-once') == result
    state = wf.read_json(Path(wf.run_dir('test-run')) / 'state.json')
    node = state['nodes'][0]
    assert node['review_status'] == 'pending' and node['checks'] == [False]
    assert len(node['generation_history']) == 1
    assert state['nodes'][1]['status'] == 'stale'
    assert state['nodes'][2]['status'] == 'passed'
    old_path = wf.contained_path(wf.BASE, old_evidence[0]['path'])
    assert Path(old_path).read_text(encoding='utf-8') == 'original'
    assert wf._file_evidence(old_path)['sha256'] == old_evidence[0]['sha256']
    with pytest.raises(ValueError, match='版本'):
        review(wf, 'test-run', 'writer', 1, hashes, 'bad-review')
    review(wf, 'test-run', 'writer', 2, hashes, 'explicit-user-review')
    reviewed = wf.read_json(Path(wf.run_dir('test-run')) / 'state.json')['nodes'][0]
    assert reviewed['review_status'] == 'approved'
    assert reviewed['reviews']['explicit-user-review']['hashes'] == hashes


def test_file_changed_after_preview_cannot_be_adopted(fixture):
    wf, state, target = fixture
    hashes = {'episodes/output.md': wf._file_evidence(target)['sha256']}
    target.write_text('changed-after-preview', encoding='utf-8')
    with pytest.raises(ValueError, match='预览'):
        adopt(wf, 'test-run', 'writer', 1, hashes, 'rejected')
    actual = wf.read_json(Path(wf.run_dir('test-run')) / 'state.json')
    assert actual['nodes'][0]['execution_generation'] == 1


def test_discussion_preserves_generation_evidence_and_deduplicates(fixture, monkeypatch):
    import types
    from wf_discussion import discuss
    wf, original, target = fixture
    calls = []
    def execute(task, **kwargs):
        calls.append(task)
        # Even if a model violates discussion-only instructions, no modified
        # working file is allowed to silently replace frozen deliverables.
        target.write_text('unreviewed change', encoding='utf-8')
        return {'status': 'completed', 'summary': 'Public explanation', 'session_id': 'same-child'}
    monkeypatch.setitem(sys.modules, 'wf_agent', types.SimpleNamespace(run_agent=execute))
    result = discuss(wf, 'test-run', 'writer', 'Explain the decision', 1, 'discussion-once')
    assert discuss(wf, 'test-run', 'writer', 'Explain the decision', 1, 'discussion-once') == result
    assert len(calls) == 1 and calls[0]['expected_artifacts'] == []
    state = wf.read_json(Path(wf.run_dir('test-run')) / 'state.json')
    assert state['status'] == original['status']
    assert state['nodes'][0]['execution_generation'] == 1
    assert state['nodes'][0]['artifact_evidence'] == original['nodes'][0]['artifact_evidence']
    assert state['state_revision'] == 2
    assert Path(wf.contained_path(wf.BASE, state['nodes'][0]['artifact_evidence'][0]['path'])).read_text() == 'original'
    with pytest.raises(ValueError, match='版本'):
        discuss(wf, 'test-run', 'writer', 'Another question', 0, 'stale-discussion')
    with pytest.raises(ValueError, match='其他讨论'):
        discuss(wf, 'test-run', 'writer', 'Different question', 1, 'discussion-once')


def test_explicit_binding_resolves_conflict_and_records_source_generation(fixture, tmp_path):
    wf, state, target = fixture
    graph = wf._canvas_graph_for('test-run')
    graph['edges'].append({'from': 'independent', 'to': 'dependent'})
    wf.write_json(Path(wf.run_dir('test-run')) / 'graph.json', graph)
    wf.write_json(Path(wf.run_dir('test-run')) / 'modules.json', {'modules': [
        {'id': 'writer', 'artifacts': ['episodes/output.md']}, {'id': 'other', 'artifacts': ['episodes/output.md']}]})
    alternate = Path(wf.BASE) / 'artifacts/alternate.md'
    alternate.parent.mkdir(parents=True, exist_ok=True)
    alternate.write_text('chosen branch', encoding='utf-8')
    state['nodes'][2].update(execution_generation=3, artifact_evidence=[
        {'path': 'artifacts/alternate.md', 'logical_path': 'episodes/output.md', **wf._file_evidence(alternate)}])
    node = {'id': 'dependent', 'module': 'copy', 'requires': ['episodes/output.md']}
    with pytest.raises(ValueError, match='绑定|多个上游'):
        wf._upstream_inputs(node, state, tmp_path / 'unbound')
    node['input_bindings'] = {'episodes/output.md': 'independent'}
    result = wf._upstream_inputs(node, state, tmp_path / 'bound')
    assert len(result) == 1 and result[0]['node_id'] == 'independent' and result[0]['generation'] == 3
    assert (tmp_path / 'bound/episodes/output.md').read_text() == 'chosen branch'
    node['input_bindings'] = {'episodes/output.md': 'dependent'}
    with pytest.raises(ValueError, match='上游'):
        wf._upstream_inputs(node, state, tmp_path / 'invalid')


def test_structured_contract_pinpoints_missing_and_wrong_fields(tmp_path):
    from wf_contracts import validate_inputs
    path = tmp_path / 'input.json'
    schema = {'type': 'object', 'required': ['count'], 'properties': {'count': {'type': 'integer'}}}
    node = {'input_schemas': {'input.json': schema}}
    rows = [{'path': 'input.json', 'sha256': 'verified-by-caller'}]
    path.write_text('{}')
    with pytest.raises(ValueError, match='input.json.count'):
        validate_inputs(node, rows, tmp_path)
    path.write_text('{"count":"three"}')
    with pytest.raises(ValueError, match='integer'):
        validate_inputs(node, rows, tmp_path)
    path.write_text('{"count":3}')
    validate_inputs(node, rows, tmp_path)
    assert rows[0]['schema_verified']


@pytest.mark.parametrize('mode', ['exception', 'cancel'])
def test_discussion_failure_restores_state_and_cancel_cannot_become_success(fixture, monkeypatch, mode):
    from wf_discussion import discuss
    import wf_agent
    wf, original, _ = fixture
    def interrupted(*args, **kwargs):
        if mode == 'exception':
            raise OSError('host disconnected')
        Path(kwargs['cancel_path']).write_text('1')
        return {'status': 'completed', 'summary': 'late result'}
    monkeypatch.setattr(wf_agent, 'run_agent', interrupted)
    result = discuss(wf, 'test-run', 'writer', 'Explain', 1, 'fault-' + mode)
    saved = wf.read_json(Path(wf.run_dir('test-run')) / 'state.json')
    assert result['status'] == ('uncertain' if mode == 'exception' else 'cancelled')
    assert saved['status'] == ('completed' if mode == 'exception' else 'cancelled')
    assert saved['nodes'][0]['artifact_evidence'] == original['nodes'][0]['artifact_evidence']
