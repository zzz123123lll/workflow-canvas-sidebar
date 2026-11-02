"""The installed plugin's durable conversation and revision contracts (no model)."""
import importlib.util
import hashlib
import json
import sys
from pathlib import Path

import pytest
import yaml

SERVER = (Path(__file__).resolve().parents[2] / "server")
sys.path.insert(0, str(SERVER))

def load(name):
    spec = importlib.util.spec_from_file_location('plugin_' + name, SERVER / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def test_question_answer_summary_remain_in_order_and_idempotent(tmp_path):
    c = load('wf_conversation')
    c.append_message('run-a','node-a',1,'assistant','Which audience?',kind='question',key='q1',base=tmp_path)
    c.append_message('run-a','node-a',1,'user','Beginners',key='a1',base=tmp_path)
    c.append_message('run-a','node-a',1,'assistant','Draft ready',kind='summary',key='s1',base=tmp_path)
    c.append_message('run-a','node-a',1,'assistant','Draft ready',kind='summary',key='s1',base=tmp_path)
    rows = c.read_messages('run-a','node-a',base=tmp_path)
    assert [r['text'] for r in rows] == ['Which audience?','Beginners','Draft ready']
    assert len({r['id'] for r in rows}) == 3
    assert [r['seq'] for r in rows] == [1,2,3]
    assert all(r['at'] for r in rows)

def test_generations_and_nodes_are_isolated(tmp_path):
    c = load('wf_conversation')
    for node, generation, text in [('a',1,'first'),('a',2,'revision'),('b',1,'other')]:
        c.append_message('run',node,generation,'assistant',text,base=tmp_path)
    assert [r['text'] for r in c.read_messages('run','a',generation=1,base=tmp_path)] == ['first']
    assert [r['text'] for r in c.read_messages('run','a',generation=2,base=tmp_path)] == ['revision']
    assert c.read_messages('missing','a',base=tmp_path) == []


def test_sdk_public_message_reads_actual_data_envelope_only():
    a=load('wf_agent')
    events=[{'type':'assistant/message','data':{'message':{'content':[
        {'type':'reasoning','text':'private reasoning'}, {'type':'text','text':'Public progress'}]}}}]
    rows=a.public_assistant_messages(events,'final JSON')
    assert [r['text'] for r in rows]==['Public progress']


def test_workspace_owner_acl_is_prepared_before_restricted_execution(tmp_path, monkeypatch):
    a=load('wf_agent')
    target=tmp_path/'node-workspace';target.mkdir()
    calls=[]
    monkeypatch.setattr(a.subprocess,'run',lambda args,**kw:calls.append((args,kw)))
    a.prepare_workspace_owner_access(target)
    assert calls
    assert calls[0][1]['env']['WF_ACL_WORKSPACE']==str(target.resolve())
    import base64
    command=base64.b64decode(calls[0][0][-1]).decode('utf-16-le')
    assert 'WindowsIdentity' in command and 'AddAccessRule' in command
    assert '-Recurse' not in command


def test_fresh_install_uses_bundled_sdk_python_without_developer_venv(tmp_path, monkeypatch):
    a=load('wf_agent')
    plugin=tmp_path/'plugin'
    python=plugin/'runtime/python/python.exe'
    python.parent.mkdir(parents=True)
    python.write_bytes(b'fixture')
    (python.parent/'Lib/site-packages/deepseek_harness').mkdir(parents=True)
    monkeypatch.setattr(a,'BASE',tmp_path/'data')
    monkeypatch.setattr(a,'__file__',str(plugin/'server/wf_agent.py'))
    monkeypatch.delenv('WF_AGENT_PYTHON',raising=False)
    assert a._runtime_python()==python.resolve()

@pytest.mark.parametrize('component', ['../outside','a/b','a\\b','..','CON'])
def test_conversation_rejects_unsafe_identity(tmp_path, component):
    c = load('wf_conversation')
    with pytest.raises(ValueError):
        c.append_message(component,'n',1,'assistant','x',base=tmp_path)

def test_installed_worker_defaults_to_guarded_explicit_workspace(tmp_path, monkeypatch):
    a = load('wf_agent')
    monkeypatch.delenv('WF_WORKER_SANDBOX', raising=False)
    patch = a.runtime_patch('nt', workspace=str(tmp_path), runtime_home=str(tmp_path / 'home'))
    assert 'dsh-workflow-worker-guard' in patch
    assert 'workspace-write' in patch
    parsed = yaml.safe_load(patch)
    policy = next(row for row in parsed if row.get('id') == 'sandbox-policy')
    assert policy['config']['workspaceRoot'] == str(tmp_path.resolve())
    assert "name: '@deepseek-ai/dsh-pwsh-local'" not in patch

def test_revision_preserves_old_evidence_marks_only_dependents_stale(tmp_path, monkeypatch):
    w = load('wf')
    monkeypatch.setattr(w, 'BASE', str(tmp_path)); monkeypatch.setattr(w, 'RUNS', str(tmp_path / '.runs'))
    monkeypatch.setattr(w,'LATEST',str(tmp_path / '.runs/LATEST'))
    run_dir = tmp_path / '.runs/run'; run_dir.mkdir(parents=True)
    episodes = tmp_path / 'episodes'; episodes.mkdir()
    (episodes / 'old.md').write_text('immutable original',encoding='utf8')
    state = {'run_id':'run','episode':'demo','task_id':'demo','status':'completed','nodes':[
        {'id':'a','module':'m','status':'passed','execution_generation':1,'artifact_evidence':[{'path':'episodes/old.md','sha256':hashlib.sha256(b'immutable original').hexdigest()}]},
        {'id':'b','module':'m','status':'passed'}, {'id':'c','module':'m','status':'passed'}, {'id':'d','module':'m','status':'passed'}]}
    (run_dir/'state.json').write_text(json.dumps(state),encoding='utf8')
    graph = {'nodes':[{'id':x,'module':'m'} for x in 'abcd'],'edges':[{'from':'a','to':'b'},{'from':'b','to':'c'}]}
    monkeypatch.setattr(w,'_canvas_graph_for',lambda rid:graph)
    monkeypatch.setattr(w,'acquire_lock',lambda rid:True); monkeypatch.setattr(w,'release_lock',lambda:None)
    calls=[]
    monkeypatch.setattr(w,'canvas_run',lambda **kw:calls.append(kw) or 0)
    assert w.canvas_revise('run','a','Make it shorter',request_id='request-1') == 0
    changed=json.loads((run_dir/'state.json').read_text(encoding='utf8'))
    byid={n['id']:n for n in changed['nodes']}
    assert byid['a']['execution_generation'] == 2
    history=byid['a']['generation_history'][0]
    assert history['generation'] == 1
    frozen=tmp_path / history['artifact_evidence'][0]['path']
    assert frozen.read_text(encoding='utf8') == 'immutable original'
    assert byid['b']['status'] == byid['c']['status'] == 'stale'
    assert byid['d']['status'] == 'passed'
    assert calls[0]['until'] == 'a'
    assert w.canvas_revise('run','a','Make it shorter',request_id='request-1') == 0
    again=json.loads((run_dir/'state.json').read_text(encoding='utf8'))
    assert again['nodes'][0]['execution_generation'] == 2


@pytest.mark.parametrize('extra, expected',[(None,'completed'),('stale','creation_ready')])
def test_revision_stop_is_completed_when_no_downstream_work_remains(tmp_path,monkeypatch,extra,expected):
    import types
    w=load('wf')
    run=tmp_path/'.runs/run';run.mkdir(parents=True)
    nodes=[{'id':'a','module':'m','status':'passed'}]
    if extra:nodes.append({'id':'b','module':'m','status':extra})
    state={'run_id':'run','episode':'demo','task_id':'demo','status':'running','nodes':nodes,'until':'a'}
    (run/'state.json').write_text(json.dumps(state),encoding='utf8')
    (run/'modules.json').write_text(json.dumps({'modules':[{'id':'m','executor':'agent'}]}))
    monkeypatch.setattr(w,'BASE',str(tmp_path));monkeypatch.setattr(w,'RUNS',str(tmp_path/'.runs'))
    monkeypatch.setattr(w,'acquire_lock',lambda rid:True);monkeypatch.setattr(w,'release_lock',lambda:None)
    monkeypatch.setattr(w,'touch_lock',lambda:None)
    monkeypatch.setattr(w,'_canvas_graph_for',lambda rid:{'nodes':[{'id':n['id'],'module':'m'} for n in nodes],'edges':[]})
    monkeypatch.setattr(w,'_validate_canvas',lambda graph:None)
    monkeypatch.setitem(sys.modules,'kb',types.SimpleNamespace(ensure_task=lambda t:t))
    assert w.canvas_run(resume_run='run')==0
    assert json.loads((run/'state.json').read_text(encoding='utf8'))['status']==expected


def test_refresh_rebuilds_all_stale_branches_and_clears_revision_stop(tmp_path,monkeypatch):
    w=load('wf')
    run=tmp_path/'.runs/run';run.mkdir(parents=True)
    state={'run_id':'run','episode':'demo','status':'creation_ready','until':'a','nodes':[
        {'id':n,'module':'m','status':status,'stale_due_to':{'node_id':'a'}}
        if status=='stale' else {'id':n,'module':'m','status':status}
        for n,status in [('a','passed'),('b','stale'),('c','stale'),('d','passed')]]}
    (run/'state.json').write_text(json.dumps(state),encoding='utf8')
    graph={'nodes':[{'id':n,'module':'m'} for n in 'abcd'],'edges':[{'from':'a','to':'b'},{'from':'a','to':'c'}]}
    monkeypatch.setattr(w,'BASE',str(tmp_path));monkeypatch.setattr(w,'RUNS',str(tmp_path/'.runs'))
    monkeypatch.setattr(w,'acquire_lock',lambda rid:True);monkeypatch.setattr(w,'release_lock',lambda:None)
    monkeypatch.setattr(w,'_canvas_graph_for',lambda rid:graph)
    calls=[]
    monkeypatch.setattr(w,'canvas_run',lambda **kw:calls.append(kw) or 0)
    assert w.canvas_rerun('run')==0
    changed=json.loads((run/'state.json').read_text(encoding='utf8'))
    assert changed['until'] is None
    assert [n['status'] for n in changed['nodes']]==['passed','pending','pending','passed']
    assert all(not n.get('stale_due_to') for n in changed['nodes'])
    assert calls[0]['resume_run']=='run'


def test_report_records_sources_it_really_used(tmp_path,monkeypatch):
    import types
    w=load('wf');monkeypatch.setattr(w,'BASE',str(tmp_path))
    (tmp_path/'episodes').mkdir()
    monkeypatch.setitem(sys.modules,'kb',types.SimpleNamespace(list_entries=lambda **kw:[{'path':'shared/a.txt','title':'A','scope':'shared'}] if kw.get('scope') else []))
    monkeypatch.setattr(w,'_kb_read',lambda entry,stem:'Actual material')
    state={'run_id':'run','episode':'demo','nodes':[{'id':'digest'}]}
    assert w.exec_report_digest({'id':'digest'},'demo','run',state)[0]=='passed'
    used=state['nodes'][0]['knowledge_sources']
    assert [s['path'] for s in used]==['shared/a.txt']
    assert used[0]['content_sha256']==hashlib.sha256(b'Actual material').hexdigest()
