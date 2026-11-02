"""Product contracts against the shipped API, isolated from user data."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

SERVER = (Path(__file__).resolve().parents[2] / "server")


def post_fixture(api, endpoint, body):
    import io
    request = object.__new__(api.Handler)
    raw = json.dumps(body).encode()
    request.path = endpoint
    request.headers = {'Content-Length':str(len(raw))}
    request.rfile = io.BytesIO(raw)
    request.send_json = lambda status,data:(status,data)
    return request.post_request()


@pytest.fixture
def product(tmp_path, monkeypatch):
    monkeypatch.setenv('CANVAS_HOME', str(tmp_path))
    monkeypatch.syspath_prepend(str(SERVER))
    spec = importlib.util.spec_from_file_location('product_api_' + tmp_path.name, SERVER / 'canvas_editor_server.py')
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    run = tmp_path / '.runs/run-1'
    run.mkdir(parents=True)
    (run / 'graph.json').write_text(json.dumps({'nodes':[{'id':'n','module':'writer'}],'edges':[], 'deliveries':[{'node_id':'n','path':'episodes/report.md','role':'primary','required':True}]}))
    (run / 'modules.json').write_text(json.dumps({'modules':[{'id':'writer','executor':'agent','checklist':['Readable']}]}))
    (tmp_path / 'episodes').mkdir()
    (tmp_path / 'episodes/report.md').write_text('finished report')
    state = {'run_id':'run-1','episode':'demo','status':'completed',
        'delivery_declarations':[{'node_id':'n','path':'episodes/report.md','role':'primary','required':True}], 'nodes':[{
        'id':'n','module':'writer','name':'Write','status':'passed','execution_generation':2,
        'agent_session_id':'session-real-2','checks':[True],
        'artifact_evidence':[{'path':'episodes/report.md','bytes':15}],
        'generation_history':[{'id':'n','module':'writer','name':'Write','generation':1,
          'execution_generation':1,'status':'passed','agent_session_id':'session-real-1','checks':[True]}],
    }]}
    (run / 'state.json').write_text(json.dumps(state))
    return api, tmp_path, state


def test_workspace_uses_chronological_durable_messages_and_session(product, monkeypatch):
    api, base, state = product
    messages = [{'id':'a','seq':1,'generation':2,'role':'assistant','kind':'question','text':'Which topic?'},
                {'id':'b','seq':2,'generation':2,'role':'user','kind':'answer','text':'Canvas'},
                {'id':'c','seq':3,'generation':2,'role':'assistant','kind':'summary','text':'Done'}]
    monkeypatch.setattr(api, 'conversation_module', lambda:SimpleNamespace(message_page=lambda *a, **kw:{'messages':messages}))
    data = api.build_workspace_payload(state, 'n')
    assert data['session_id'] == data['node']['session_id'] == 'session-real-2'
    assert [m['id'] for m in data['messages']] == ['a','b','c']
    assert data['node']['generation'] == 2
    assert [g['generation'] for g in data['generations']] == [1,2]


def test_old_generation_reads_old_identity_without_mutating_current(product, monkeypatch):
    api, base, state = product
    seen=[]
    monkeypatch.setattr(api, 'conversation_module', lambda:SimpleNamespace(message_page=lambda *a, **kw:seen.append(kw.get('generation')) or {'messages':[]}))
    data=api.build_workspace_payload(state,'n',generation=1)
    assert data['session_id']=='session-real-1'
    assert data['node']['generation']==1 and seen==[1]
    assert state['nodes'][0]['execution_generation']==2


def test_shared_upload_is_readable_without_creating_fake_task(product):
    import base64
    api, base, _ = product
    status, response = post_fixture(api, '/api/kb/attach', {
        'scope':'shared','name':'shared-note.txt','data':base64.b64encode('共享原文'.encode()).decode()})
    assert status == 200
    assert response['path']=='shared/files/shared-note.txt'
    assert api.knowledge_module().read_entry_content(response['path'])['content']=='共享原文'
    assert not (base/'kb/knowledge/tasks/__shared__').exists()


def test_continue_only_queues_stale_run(product,monkeypatch):
    api,base,state=product
    commands=[]
    monkeypatch.setattr(api,'start_wf',lambda args:commands.append(args))
    assert post_fixture(api,'/api/wf/continue',{'run_id':'run-1'})[0]==409
    state['nodes'][0]['status']='stale'
    (base/'.runs/run-1/state.json').write_text(json.dumps(state))
    assert post_fixture(api,'/api/wf/continue',{'run_id':'run-1'})[0]==200
    queued=api.plugin_module('wf_commands').pending(base, lambda pid: True)
    assert len(queued)==1 and queued[0]['action']=='continue'
    assert queued[0]['run_id']=='run-1' and not commands


def test_old_summary_is_labelled_as_legacy_not_full_transcript(product, monkeypatch):
    api, base, state = product
    monkeypatch.setattr(api, 'conversation_module', lambda:SimpleNamespace(message_page=lambda *a, **kw:{'messages':[]}))
    state['nodes'][0]['agent_report']={'summary':'Real saved summary'}
    data=api.build_workspace_payload(state,'n')
    assert data['messages'][0]['kind']=='legacy_summary'
    assert data['conversation_source']=='legacy_summary'


def test_results_points_to_actual_artifact_and_does_not_claim_missing_ready(product):
    api, base, state = product
    data=api.build_results_payload(state)
    assert data['artifacts'][0]['node_id']=='n'
    assert data['primary'][0]['path']=='episodes/report.md'
    assert data['primary'][0]['media_url'].startswith('http://127.0.0.1:8793/api/wf/raw?')
    assert 'run_id=run-1' in data['primary'][0]['media_url']
    assert data['deliverable'] is True
    (base/'episodes/report.md').unlink()
    assert api.build_results_payload(state)['deliverable'] is False


def test_history_evidence_remains_authorized(product):
    api, base, state=product
    old='artifacts/run-1/nodes/n/generation-1/report.md'
    state['nodes'][0]['generation_history'][0]['artifact_evidence']=[{'path':old}]
    assert api.artifact_allowed(old, api.allowed_artifact_paths(state))


@pytest.mark.parametrize('missing',[False,True])
def test_frozen_artifact_does_not_duplicate_or_fall_back_to_mutable_file(product,missing):
    api,base,state=product
    frozen='artifacts/run-1/nodes/n/generation-2/report.md'
    target=base/frozen;target.parent.mkdir(parents=True)
    if not missing: target.write_text('finished report')
    node=state['nodes'][0]
    node['artifacts']=['episodes/report.md']
    node['artifact_evidence']=[{'path':frozen,'logical_path':'episodes/report.md','bytes':15}]
    rows=api.artifact_rows(state,node)[0]
    assert len(rows)==1 and rows[0]['path']==frozen
    assert rows[0]['status']==('missing' if missing else 'verified')


def test_result_primary_prefers_finished_video_over_capture(product):
    api, base, state=product
    state.pop('delivery_declarations')  # Legacy inference remains viewable, never silently approved.
    for name in ('capture.mp4','final.mp4'):
        (base/'episodes'/name).write_bytes(b'video')
    state['nodes'] = [
        {'id':'video_capture','module':'capture','status':'passed','artifact_evidence':[{'path':'episodes/capture.mp4','bytes':5}]},
        {'id':'fast_assemble','module':'assemble','status':'passed','artifact_evidence':[{'path':'episodes/final.mp4','bytes':5}]},
    ]
    assert api.build_results_payload(state)['primary'][0]['path']=='episodes/final.mp4'


def test_reply_is_idempotent_and_bound_to_generation(product, monkeypatch):
    api, base, state=product
    state['status']='awaiting_input'; state['nodes'][0]['status']='awaiting_input'
    (base/'.runs/run-1/state.json').write_text(json.dumps(state))
    calls=[]
    monkeypatch.setattr(api,'start_wf',lambda args:calls.append(args))
    body={'run_id':'run-1','node':'n','answer':'Alice','expected_generation':2,'request_id':'once'}
    first=api.submit_node_command('reply',body)
    assert api.submit_node_command('reply',body)==first and not calls
    queued=api.plugin_module('wf_commands').pending(base, lambda pid: True)
    assert len(queued)==1 and queued[0]['action']=='reply'
    assert json.loads(queued[0]['payload'])['expected_generation']==2
    with pytest.raises(ValueError, match='新版本'):
        api.submit_node_command('reply',dict(body,expected_generation=1,request_id='stale'))


def test_program_module_cannot_silently_ignore_chat_revision(product):
    api,base,state=product
    (base/'.runs/run-1/modules.json').write_text(json.dumps({'modules':[{'id':'writer','executor':'report_digest'}]}))
    with pytest.raises(ValueError,match='程序模块'):
        api.submit_node_command('revise',{'run_id':'run-1','node':'n','answer':'Make it shorter'})


def test_program_workspace_shows_declared_inputs_without_inventing_execution_evidence(product):
    api,base,state=product
    (base/'.runs/run-1/modules.json').write_text(json.dumps({'modules':[{'id':'writer','executor':'report_qc','requires':['episodes/{stem}-digest.md']}]}))
    data=api.build_workspace_payload(state,'n')
    assert data['declared_inputs']==['episodes/demo-digest.md']
    assert data['upstream']==[]
