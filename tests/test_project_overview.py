import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from wf_projects import ProjectStore, Conflict
from wf_overview import overview


def fixture(tmp_path):
    store = ProjectStore(tmp_path)
    project = store.mutate('create', {'request_id': 'create', 'name': '项目', 'goal': '目标'})['project']
    return store, project


def run(store, project, rid, status='completed', kind=None):
    state = {'run_id': rid, 'created_at': rid, 'status': status,
             'nodes': [{'id': 'writer', 'status': 'awaiting_input' if status == 'awaiting_input' else 'passed'}]}
    if kind:
        state['run_kind'] = kind
    path = store.base / '.runs' / rid / 'state.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(state), encoding='utf-8')
    store.bind_run(project['project_id'], rid)
    return path


def results(state):
    return {'deliverable': state['status'] == 'completed', 'pending_reviews': [], 'external_changes': []}


def command(store, project, operation, rid, **extra):
    return store.mutate(operation, {'project_id': project['project_id'], 'expected_revision': project['revision'],
        'expected_work_revision':project.get('work_revision',0),
        'request_id': operation + rid, 'run_id': rid, **extra})['project']


def test_old_pending_is_visible_as_unresolved_history_not_current_blocker(tmp_path):
    store, project = fixture(tmp_path)
    old = run(store, project, 'run-a', 'awaiting_input')
    before = old.read_bytes()
    run(store, project, 'run-b')
    run(store, project, 'run-c', kind='workflow_proposal')
    data = overview(store, project['project_id'], results)
    assert data['current_run']['run_id'] == 'run-b'
    assert data['project_status'] == 'deliverable'
    assert not data['pending_actions']
    assert data['unresolved_history_count'] == 1
    assert data['historical_pending_actions'][0]['disposition'] == 'unresolved-history'
    assert old.read_bytes() == before


def test_work_and_delivery_remain_independent_with_audited_explicit_supersession(tmp_path):
    store, project = fixture(tmp_path)
    a = run(store, project, 'run-a', 'awaiting_input'); before = a.read_bytes()
    run(store, project, 'run-b')
    project = command(store, project, 'set-delivery-run', 'run-b')
    run(store, project, 'run-c', 'running')
    project = command(store, project, 'set-work-run', 'run-c')
    data = overview(store, project['project_id'], results)
    assert data['project_status'] == 'revising'
    assert data['delivery_run']['run_id'] == 'run-b' and data['delivery_valid']
    project = command(store, project, 'supersede-run', 'run-a', replacement_run_id='run-b', reason='本次交付替代早期草稿')
    data = overview(store, project['project_id'], results)
    assert data['historical_pending_actions'][0]['disposition'] == 'superseded'
    assert data['dispositions']['run-a']['reason'] == '本次交付替代早期草稿'
    assert a.read_bytes() == before


def test_cross_project_selection_stale_commands_and_proposals_are_rejected(tmp_path):
    store, project = fixture(tmp_path)
    other = store.mutate('create', {'request_id': 'other', 'name': '项目', 'goal': '另一个'})['project']
    run(store, other, 'run-foreign')
    with pytest.raises(Conflict):command(store, project, 'set-work-run', 'run-foreign')
    run(store, project, 'run-proposal', kind='workflow_proposal')
    with pytest.raises(ValueError):command(store, project, 'set-work-run', 'run-proposal')
    run(store, project, 'run-real')
    newer = command(store, project, 'set-work-run', 'run-real')
    assert command(store, project, 'set-work-run', 'run-real') == newer
    with pytest.raises(Conflict):
        store.mutate('set-work-run', {'project_id': project['project_id'], 'expected_revision': 1,
            'request_id': 'new-stale-command', 'run_id': 'run-real'})


def test_new_admission_is_current_before_workspace_creation_and_changes_selection_revision(tmp_path):
    store,project=fixture(tmp_path)
    run(store,project,'run-old')
    project=store.mutate('update',{'project_id':project['project_id'],'expected_revision':1,
        'request_id':'add-graph','graph':{'nodes':[{'id':'write','module':'writer'}],'edges':[]}})['project']
    accepted,_=store.admit_run({'project_id':project['project_id'],'expected_revision':project['revision'],'request_id':'new-run'})
    data=overview(store,project['project_id'],results)
    assert data['current_run']['run_id']==accepted['run_id']
    assert data['current_run']['status']=='queued' and data['project_status']=='preparing'
    with pytest.raises(Conflict):command(store,project,'set-work-run','run-old')
