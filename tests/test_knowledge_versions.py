import importlib.util
import json
from pathlib import Path
import sys

import pytest

SERVER = Path(__file__).resolve().parents[1] / 'server'
sys.path.insert(0, str(SERVER))


@pytest.fixture
def kb(tmp_path):
    spec = importlib.util.spec_from_file_location('knowledge_versions_test', SERVER / 'kb.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name, rel in {'BASE': '', 'KB': 'kb/knowledge', 'SHARED': 'kb/knowledge/shared',
                      'TASKS': 'kb/knowledge/tasks', 'INDEX': 'kb/knowledge/index.json'}.items():
        setattr(module, name, str(tmp_path / rel))
    module.ensure_task('A')
    return module


def test_knowledge_revisions_keep_original_bytes_and_reset_confirmation(kb):
    from wf_knowledge import decide
    path = Path(kb.KB) / 'tasks/A/rule.md'
    path.write_text('Version one', encoding='utf-8')
    first = kb.read_entry_content('tasks/A/rule.md', task='A')
    origin = {'run_id': 'source-run', 'generation': 2, 'sha256': first['file_sha256']}
    decide(kb.BASE, first['path'], 1, 'draft', 'source-first', source=origin, module='writer')
    confirmed = decide(kb.BASE, first['path'], 1, 'confirmed', 'confirm-first')
    assert confirmed['source_revision'] == origin and confirmed['module_scope'] == 'writer'
    assert kb.read_entry_content(first['path'], task='A')['knowledge_status'] == 'confirmed'
    path.write_text('Version two', encoding='utf-8')
    second = kb.read_entry_content(first['path'], task='A')
    assert second['knowledge_id'] == first['knowledge_id'] and second['knowledge_revision'] == 2
    assert second['knowledge_status'] == 'draft'
    old = Path(kb.BASE) / '.runs/knowledge-revisions' / first['knowledge_id'] / '1/rule.md'
    assert old.read_text() == 'Version one'
    with pytest.raises(ValueError, match='版本'):
        decide(kb.BASE, first['path'], 1, 'confirmed', 'stale-confirm')


def test_late_target_is_selected_with_locator_and_required_missing_is_explicit(kb):
    path = Path(kb.KB) / 'tasks/A/long.md'
    path.write_text(('ordinary filler line\n' * 500) + 'The unique target is TARGET-682 and value is cobalt.\n' + ('more filler\n' * 500), encoding='utf-8')
    context = Path(kb.context_pack('A', query='TARGET-682', budget_chars=1200, required_paths=['tasks/A/long.md']))
    assert 'TARGET-682' in context.read_text(encoding='utf-8')
    manifest = json.loads(context.with_suffix('.sources.json').read_text(encoding='utf-8'))
    assert manifest['selected'][0]['reason'] == 'required'
    assert manifest['selected'][0]['locations'][0]['line_end'] >= 501
    with pytest.raises(ValueError, match='必读资料'):
        kb.context_pack('A', required_paths=['tasks/A/missing.md'])


def test_deprecated_knowledge_is_excluded_with_a_reason(kb):
    from wf_knowledge import decide
    path = Path(kb.KB) / 'tasks/A/retired.md'
    path.write_text('DEPRECATED-917', encoding='utf-8')
    item = kb.read_entry_content('tasks/A/retired.md', task='A')
    decide(kb.BASE, item['path'], item['knowledge_revision'], 'deprecated', 'retire')
    context = Path(kb.context_pack('A'))
    assert 'DEPRECATED-917' not in context.read_text(encoding='utf-8')
    manifest = json.loads(context.with_suffix('.sources.json').read_text(encoding='utf-8'))
    assert any(x['reason'] == 'deprecated_or_other_module' for x in manifest['omitted'])


def test_promote_exact_generation_conflict_and_replay(tmp_path, monkeypatch):
    import hashlib
    monkeypatch.setenv('CANVAS_HOME', str(tmp_path))
    spec = importlib.util.spec_from_file_location('knowledge_promote_api', SERVER / 'canvas_editor_server.py')
    api = importlib.util.module_from_spec(spec); spec.loader.exec_module(api)
    api.knowledge_module().ensure_task('A')
    directory = tmp_path / '.runs/r'; directory.mkdir(parents=True)
    (directory / 'graph.json').write_text(json.dumps({'nodes': [{'id': 'n', 'module': 'm', 'artifacts': ['episodes/result.md']}], 'edges': []}))
    (directory / 'modules.json').write_text('{"modules":[]}')
    source = tmp_path / 'episodes/result.md'; source.parent.mkdir(); source.write_text('Verified source')
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    state = {'run_id': 'r', 'episode': 'research', 'task_id': 'A', 'status': 'completed', 'nodes': [
        {'id': 'n', 'module': 'm', 'status': 'passed', 'execution_generation': 1,
         'artifact_evidence': [{'path': 'episodes/result.md', 'sha256': digest, 'bytes': source.stat().st_size}]}]}
    (directory / 'state.json').write_text(json.dumps(state))
    body = {'run_id': 'r', 'node': 'n', 'generation': 1, 'artifact_path': 'episodes/result.md',
            'source_sha256': digest, 'path': 'tasks/A/confirmed/rule.md', 'request_id': 'promote-1'}
    result = api.promote_artifact(body)
    assert result['knowledge_status'] == 'confirmed'
    assert result['source']['sha256'] == digest
    assert api.promote_artifact(body)['knowledge_id'] == result['knowledge_id']
    with pytest.raises(ValueError, match='标识'):
        api.promote_artifact(dict(body, path='tasks/A/confirmed/other.md'))
    assert not (tmp_path / 'kb/knowledge/tasks/A/confirmed/other.md').exists()
    target = tmp_path / 'kb/knowledge/tasks/A/confirmed/rule.md'
    target.write_text('A more recent decision')
    api.knowledge_module().read_entry_content(body['path'], task='A')
    with pytest.raises(ValueError, match='同名'):
        api.promote_artifact(dict(body, request_id='promote-2'))
    assert target.read_text() == 'A more recent decision'
    old_hash = hashlib.sha256(target.read_bytes()).hexdigest()
    replaced = api.promote_artifact(dict(body, request_id='promote-3', expected_sha256=old_hash))
    assert replaced['knowledge_revision'] == 3
    versions = api.plugin_module('wf_knowledge').history(tmp_path, body['path'])
    assert len(versions) == 3
    assert (tmp_path / versions[1]['source_path']).read_text() == 'A more recent decision'
