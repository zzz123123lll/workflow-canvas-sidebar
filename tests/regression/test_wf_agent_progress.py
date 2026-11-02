"""Live execution telemetry uses isolated workers, never a model or media service."""
import json
from pathlib import Path
import sys
import time
import types

import pytest

sys.path.insert(0, str((Path(__file__).resolve().parents[2] / "server")))
import wf_agent as adapter


def task():
    return {"objective": "fixture", "task_id": "task", "node_id": "node",
            "expected_artifacts": [], "checklist": []}


def worker(tmp_path, monkeypatch, source):
    file = tmp_path / "worker.py"
    file.write_text(source, encoding="utf-8")
    monkeypatch.setattr(adapter, "resolve_api_key", lambda: "fixture-not-real-api-key")
    monkeypatch.setattr(adapter, "_runtime_python", lambda: Path(sys.executable))
    monkeypatch.setattr(adapter, "_worker_command", lambda python: [str(python), str(file)])


def test_parent_heartbeat_reflects_alive_worker_and_final_exit(tmp_path, monkeypatch):
    worker(tmp_path, monkeypatch, '''import json,sys,time,pathlib
p=json.load(sys.stdin)
pathlib.Path(p['progress_path']).write_text(json.dumps({'phase':'tool','event_count':3,'tool_calls':1,'tool_name':'pwsh','last_event_type':'tool/call','last_event_at':time.time(),'content':'SECRET_ARGS'}))
time.sleep(1.3)
print(json.dumps({'status':'completed','summary':'done','artifacts':[],'checks':[]}))
''')
    updates=[]
    result=adapter.run_agent(task(), workspace=tmp_path/'ws', runtime_home=tmp_path/'home', session_id='s', on_progress=updates.append)
    assert result['status']=='completed'
    alive=[x for x in updates if x['worker_alive']]
    assert len(alive)>=2
    assert any(x['phase']=='tool' and x['tool_calls']==1 for x in alive)
    assert updates[-1]['worker_alive'] is False
    assert updates[-1]['phase']=='worker_finished'
    assert all(x['heartbeat_at'] <= time.time() for x in updates)
    assert 'SECRET_ARGS' not in json.dumps(updates)


def test_progress_callback_failure_stops_actual_worker(tmp_path, monkeypatch):
    worker(tmp_path, monkeypatch, "import json,sys,time; json.load(sys.stdin); time.sleep(20)")
    stopped=[]
    original=adapter._stop_worker
    monkeypatch.setattr(adapter, '_stop_worker', lambda child: stopped.append(child.pid) or original(child))
    def callback(value):
        if value['worker_alive']:
            raise RuntimeError('observer failed')
    result=adapter.run_agent(task(), workspace=tmp_path/'ws', runtime_home=tmp_path/'home', session_id='s', on_progress=callback)
    assert result['status']=='failed'
    assert stopped and result['evidence']['cleanup_confirmed'] is True


def test_cancel_final_progress_never_claims_worker_alive(tmp_path, monkeypatch):
    worker(tmp_path, monkeypatch, "import json,sys,time; json.load(sys.stdin); time.sleep(20)")
    cancel=tmp_path/'cancel'
    updates=[]
    def callback(value):
        updates.append(value)
        if value['worker_alive']:
            cancel.write_text('1')
    result=adapter.run_agent(task(), workspace=tmp_path/'ws', runtime_home=tmp_path/'home', session_id='s', cancel_path=cancel, on_progress=callback)
    assert result['status']=='cancelled'
    assert updates[-1]['worker_alive'] is False
    assert updates[-1]['phase']=='cancelled'


def test_worker_notifications_expose_only_safe_activity_metadata(tmp_path, monkeypatch):
    progress=tmp_path/'live.json'
    reports=[]
    writer=adapter.ProgressWriter(progress, clock=lambda: 100.0)
    writer.notify(types.SimpleNamespace(method='session.event',payload={'event':{'type':'tool/call','data':{'name':'pwsh','arguments':{'secret':'NEVER_COPY'}}}}))
    writer.notify(types.SimpleNamespace(method='session.event',payload={'event':{'type':'assistant/message','data':{'message':{'content':'NEVER_COPY'}}}}))
    writer.flush(force=True)
    result=json.loads(progress.read_text())
    assert result['event_count']==2 and result['tool_calls']==1
    assert result['phase']=='model'
    assert result['tool_name']=='pwsh'
    assert result['last_event_type']=='assistant/message'
    assert 'NEVER_COPY' not in progress.read_text()
    assert not list(tmp_path.glob('*.tmp-*'))


def test_sidechannel_rejects_stale_malformed_or_sensitive_fields(tmp_path):
    file=tmp_path/'live.json'
    file.write_text(json.dumps({'phase':'tool','event_count':9,'tool_calls':2,'last_event_at':1,'tool_name':'sk-1234567890abcdef','last_event_type':'ARBITRARY_SECRET','api_key':'NEVER_COPY'}))
    assert adapter.read_progress(file, started_at=10)=={}
    file.write_text('{broken')
    assert adapter.read_progress(file, started_at=10)=={}


def test_progress_file_cannot_be_written_into_task_workspace(tmp_path, monkeypatch):
    worker(tmp_path, monkeypatch, "raise AssertionError('must not launch')")
    result=adapter.run_agent(task(), workspace=tmp_path/'ws', runtime_home=tmp_path/'home', session_id='s', progress_path=tmp_path/'ws/live.json')
    assert result['status']=='failed'


def test_missing_credential_reports_waiting_not_running(tmp_path, monkeypatch):
    monkeypatch.setattr(adapter,'resolve_api_key',lambda:None)
    updates=[]
    result=adapter.run_agent(task(),workspace=tmp_path/'ws',runtime_home=tmp_path/'home',session_id='s',on_progress=updates.append)
    assert result['status']=='needs_input'
    assert updates[-1]['phase']=='waiting_input'
    assert updates[-1]['worker_alive'] is False


def test_sdk_callback_is_wired_to_metadata_sidechannel(tmp_path, monkeypatch):
    class FakeHarness:
        def __init__(self, **kwargs):
            env=kwargs['env']
            Path(env['WF_WORKER_GUARD_READY']).write_text(json.dumps({'nonce':env['WF_WORKER_GUARD_NONCE'],'guard':True,'mode':'workspace-write'}))
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def run(self, prompt, *, session_id, on_notification):
            for event in [{'type':'tool/call','data':{'name':'pwsh','arguments':'NEVER_COPY'}},
                          {'type':'tool/result','data':{'message':{'content':'NEVER_COPY'}}}]:
                on_notification(types.SimpleNamespace(method='session.event',payload={'event':event}))
            return types.SimpleNamespace(finish_reason='completed',events=[],notifications=[],
                final_response=json.dumps({'status':'completed','summary':'done','artifacts':[],'checks':[]}))
    monkeypatch.setitem(sys.modules,'deepseek_harness',types.SimpleNamespace(DeepSeekHarness=FakeHarness))
    live=tmp_path/'live.json'
    result=adapter.invoke_sdk({'task':task(),'runtime_home':str(tmp_path/'home'),'workspace':str(tmp_path/'ws'),
                               'session_id':'s','resume':False,'progress_path':str(live)})
    assert result['status']=='completed'
    value=json.loads(live.read_text())
    assert value['tool_calls']==1 and value['event_count']==2
    assert value['phase']=='model' and value['last_event_type']=='tool/result'
    assert 'NEVER_COPY' not in live.read_text()


def test_bad_sidechannel_does_not_break_parent_heartbeat(tmp_path, monkeypatch):
    worker(tmp_path, monkeypatch, '''import json,sys,time,pathlib
p=json.load(sys.stdin); pathlib.Path(p['progress_path']).write_text('{broken')
time.sleep(1.1)
print(json.dumps({'status':'completed','summary':'done','artifacts':[],'checks':[]}))
''')
    updates=[]
    result=adapter.run_agent(task(),workspace=tmp_path/'ws',runtime_home=tmp_path/'home',session_id='s',on_progress=updates.append)
    assert result['status']=='completed'
    assert any(x['worker_alive'] for x in updates)
    assert updates[-1]['worker_alive'] is False


def test_progress_path_junction_is_rejected_before_worker_launch(tmp_path, monkeypatch):
    worker(tmp_path, monkeypatch, "raise AssertionError('must not launch')")
    home=tmp_path/'home';home.mkdir();target=home/'target';target.mkdir();link=home/'linked'
    try:
        link.symlink_to(target,target_is_directory=True)
    except OSError:
        if sys.platform!='win32': raise
        import subprocess
        subprocess.run(['cmd','/c','mklink','/J',str(link),str(target)],capture_output=True,check=True)
    result=adapter.run_agent(task(),workspace=tmp_path/'ws',runtime_home=home,session_id='s',progress_path=link/'live.json')
    assert result['status']=='failed'
    assert not (target/'live.json').exists()


def test_progress_atomic_replace_retries_windows_sharing_collision(tmp_path, monkeypatch):
    calls=[];waits=[];original=adapter.os.replace
    def replace(source,destination):
        calls.append((source,destination))
        if len(calls)<3:
            error=PermissionError('busy reader');error.winerror=5;raise error
        return original(source,destination)
    monkeypatch.setattr(adapter.os,'replace',replace)
    monkeypatch.setattr(adapter.time,'sleep',waits.append)
    writer=adapter.ProgressWriter(tmp_path/'live.json',clock=lambda:100)
    assert writer.flush(force=True) is True
    assert len(calls)==3 and len(waits)==2
    assert json.loads((tmp_path/'live.json').read_text())['phase']=='connecting'


def test_permanently_locked_progress_file_is_optional_and_bounded(tmp_path, monkeypatch):
    calls=[];waits=[]
    def replace(*args):
        calls.append(args);error=PermissionError('busy reader');error.winerror=32;raise error
    monkeypatch.setattr(adapter.os,'replace',replace)
    monkeypatch.setattr(adapter.time,'sleep',waits.append)
    writer=adapter.ProgressWriter(tmp_path/'live.json',clock=lambda:100)
    assert writer.flush(force=True) is False
    assert 1<len(calls)<=6 and sum(waits)<1
    assert not list(tmp_path.glob('*.tmp-*'))
    writer.notify(types.SimpleNamespace(method='session.event',payload={'event':{'type':'tool/call','data':{'name':'pwsh'}}}))
    assert len(calls)<=6  # Failure is throttled instead of retrying on every token/event.


def test_optional_progress_failure_does_not_abort_sdk_report(tmp_path, monkeypatch):
    def busy(*args):
        error=PermissionError('busy reader');error.winerror=32;raise error
    monkeypatch.setattr(adapter.os,'replace',busy)
    monkeypatch.setattr(adapter.time,'sleep',lambda seconds:None)
    class FakeHarness:
        def __init__(self, **kwargs):
            env=kwargs['env']
            Path(env['WF_WORKER_GUARD_READY']).write_text(json.dumps({'nonce':env['WF_WORKER_GUARD_NONCE'],'guard':True,'mode':'workspace-write'}))
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def run(self, prompt, *, session_id, on_notification):
            on_notification(types.SimpleNamespace(method='session.event',payload={'event':{'type':'assistant/message','data':{'message':'PRIVATE'}}}))
            return types.SimpleNamespace(finish_reason='completed',events=[],notifications=[],
                final_response=json.dumps({'status':'completed','summary':'done','artifacts':[],'checks':[]}))
    monkeypatch.setitem(sys.modules,'deepseek_harness',types.SimpleNamespace(DeepSeekHarness=FakeHarness))
    result=adapter.invoke_sdk({'task':task(),'runtime_home':str(tmp_path/'home'),'workspace':str(tmp_path/'ws'),
                               'session_id':'s','resume':False,'progress_path':str(tmp_path/'live.json')})
    assert result['status']=='completed'
