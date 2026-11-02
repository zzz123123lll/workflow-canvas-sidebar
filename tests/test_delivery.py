import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import zipfile

import pytest

SERVER = Path(__file__).resolve().parents[1] / 'server'
sys.path.insert(0, str(SERVER))
from wf_delivery import export_bundle
from wf_delivery import configure


def test_declared_multiple_primary_required_review_and_export(tmp_path, monkeypatch):
    monkeypatch.setenv('CANVAS_HOME', str(tmp_path))
    spec = importlib.util.spec_from_file_location('delivery_api_test', SERVER / 'canvas_editor_server.py')
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    run = tmp_path / '.runs/run-test'
    run.mkdir(parents=True)
    (run / 'graph.json').write_text(json.dumps({'nodes': [{'id': 'write', 'module': 'writer',
        'artifacts': ['episodes/main.md', 'episodes/sources.txt'], 'deliveries': {
        'episodes/main.md': {'role': 'primary', 'required': True, 'review_required': True},
        'episodes/sources.txt': {'role': 'primary', 'required': True}}}], 'edges': []}))
    (run / 'modules.json').write_text('{"modules":[]}')
    (tmp_path / 'episodes').mkdir()
    evidence = []
    for name, text in [('main.md', 'Readable report'), ('sources.txt', 'Source A')]:
        (tmp_path / 'episodes' / name).write_text(text)
        evidence.append({'path': 'episodes/' + name, 'sha256': hashlib.sha256(text.encode()).hexdigest(), 'bytes': len(text)})
    state = {'run_id': 'run-test', 'episode': 'demo', 'status': 'completed', 'nodes': [
        {'id': 'write', 'module': 'writer', 'status': 'passed', 'artifact_evidence': evidence}]}
    result = api.build_results_payload(state)
    assert len(result['primary']) == 2 and not result['deliverable'] and result['pending_reviews'] == ['write']
    state['nodes'][0]['review_status'] = 'approved'
    state['nodes'][0]['reviews'] = {'review': {'generation': 1, 'hashes': {e['path']: e['sha256'] for e in evidence}, 'decision': 'approved'}}
    result = api.build_results_payload(state)
    assert result['deliverable']
    bundle = export_bundle(tmp_path, state, result)
    with zipfile.ZipFile(bundle) as archive:
        manifest = json.loads(archive.read('manifest.json'))
        for item in manifest['files']:
            assert hashlib.sha256(archive.read(item['path'])).hexdigest() == item['sha256']
        assert len(manifest['files']) == 2
        assert all('host' not in name for name in archive.namelist())
    (tmp_path / 'episodes/sources.txt').unlink()
    result = api.build_results_payload(state)
    assert not result['deliverable'] and result['missing_required']
    with pytest.raises(ValueError, match='缺失'):
        export_bundle(tmp_path, state, result)


def test_scope_confirmation_checks_state_and_retains_snapshot(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('delivery_scope_wf', SERVER / 'wf.py')
    wf = importlib.util.module_from_spec(spec); spec.loader.exec_module(wf)
    for key, rel in {'BASE': '', 'RUNS': '.runs', 'LOCKFILE': '.runs/.lock'}.items():
        monkeypatch.setattr(wf, key, str(tmp_path / rel))
    state_path = tmp_path / '.runs/r/state.json'
    wf.write_json(state_path, {'run_id': 'r', 'status': 'completed', 'state_revision': 7,
        'nodes': [{'id': 'n', 'status': 'passed', 'artifact_evidence': [
            {'path': 'frozen/a', 'logical_path': 'result.md', 'sha256': 'a'},
            {'path': 'frozen/dir', 'logical_path': 'source', 'kind': 'directory', 'files': [{'path': 'code.js'}]}]}]})
    rows = [{'node_id': 'n', 'path': 'result.md', 'role': 'primary', 'review_required': True},
            {'node_id': 'n', 'path': 'source/code.js', 'role': 'attachment', 'required': True}]
    body = {'request_id': 'scope', 'expected_state_revision': 6, 'declarations': rows}
    with pytest.raises(ValueError, match='变化'):
        configure(wf, 'r', body)
    body['expected_state_revision'] = 7
    assert configure(wf, 'r', body)['ok']
    saved = wf.read_json(state_path, {})
    assert saved['nodes'][0]['review_status'] == 'pending' and len(saved['delivery_declarations']) == 2
    assert saved['nodes'][0]['artifact_evidence'][0]['path'] == 'frozen/a'
    assert configure(wf, 'r', body)['ok']
    assert len(wf.read_json(state_path, {})['delivery_history']) == 1
    with pytest.raises(ValueError, match='标识'):
        configure(wf, 'r', dict(body, declarations=rows[:1]))
