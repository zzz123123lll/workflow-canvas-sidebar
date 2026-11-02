import json
from pathlib import Path
import wf_agent


def market_plugin(tmp_path, monkeypatch):
    plugin = tmp_path / 'plugin'
    plugin.mkdir()
    (plugin / 'dependencies.json').write_text(json.dumps({'agent_backend': 'host-subagent'}))
    monkeypatch.setattr(wf_agent, '__file__', str(plugin / 'server/wf_agent.py'))
    monkeypatch.setattr(wf_agent, 'BASE', tmp_path / 'data')
    return plugin


def test_market_install_without_host_does_not_request_sdk_or_credentials(tmp_path, monkeypatch):
    market_plugin(tmp_path, monkeypatch)
    def prohibited(): raise AssertionError('Market plugin must use the Harness account')
    monkeypatch.setattr(wf_agent, 'resolve_api_key', prohibited)
    monkeypatch.setattr(wf_agent, '_runtime_python', prohibited)
    result = wf_agent.run_agent({'task_id': 't', 'node_id': 'n', 'objective': 'Continue'},
        workspace=tmp_path/'ws', runtime_home=tmp_path/'home', session_id='kept-session')
    assert result['status'] == 'needs_input'
    assert result['session_id'] == 'kept-session'
    assert result['evidence']['backend'] == 'host-subagent'
    assert 'Harness' in result['question'] and '无需另配' in result['question']
    assert wf_agent.runtime_status()['installed'] is False


def test_market_host_executes_without_a_bundled_sdk(tmp_path, monkeypatch):
    market_plugin(tmp_path, monkeypatch)
    monkeypatch.setattr(wf_agent, 'host_agent_config', lambda: {'url': 'http://127.0.0.1:1', 'token': 'fixture'})
    monkeypatch.setattr(wf_agent, 'prepare_workspace_owner_access', lambda path: None)
    monkeypatch.setattr(wf_agent, 'run_via_host', lambda *args, **kwargs: {'status': 'completed', 'session_id': 'child'})
    def prohibited(): raise AssertionError('No SDK or separate credentials should be needed')
    monkeypatch.setattr(wf_agent, '_runtime_python', prohibited)
    monkeypatch.setattr(wf_agent, 'resolve_api_key', prohibited)
    result = wf_agent.run_agent({'task_id': 't', 'node_id': 'n', 'objective': 'Write'},
        workspace=tmp_path/'ws', runtime_home=tmp_path/'home', session_id='')
    assert result['status'] == 'completed'
    assert wf_agent.runtime_status() == {'installed': True, 'configured': True, 'backend': 'host-subagent'}


def test_disconnected_host_never_falls_back_to_api_key(tmp_path, monkeypatch):
    marker=tmp_path/'.runs/host-agent.json';marker.parent.mkdir()
    marker.write_text(json.dumps({'url':'','token':''}))
    monkeypatch.setattr(wf_agent,'BASE',tmp_path)
    def prohibited(): raise AssertionError('Disconnected host must not request an SDK key')
    monkeypatch.setattr(wf_agent,'resolve_api_key',prohibited)
    result=wf_agent.run_agent({'task_id':'t','node_id':'n','objective':'Continue'},
        workspace=tmp_path/'ws',runtime_home=tmp_path/'home',session_id='existing-child')
    assert result['status']=='needs_input'
    assert result['session_id']=='existing-child'
    assert result['evidence']['backend']=='host-subagent'
    assert '无需另配' in result['question']
    marker.write_text('{invalid')
    assert wf_agent.host_agent_config() == {'disconnected': True}


def test_sdk_token_limit_continues_once_in_same_session(tmp_path, monkeypatch):
    import sys,types
    calls=[]
    class Harness:
        def __init__(self,**kwargs):
            env=kwargs['env']
            Path(env['WF_WORKER_GUARD_READY']).write_text(json.dumps({'nonce':env['WF_WORKER_GUARD_NONCE'],'guard':True,'mode':'workspace-write'}))
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def resume_session(self,session): calls.append(('resume',session));return self
        def run(self,prompt,**kwargs):
            calls.append(('run',kwargs.get('session_id')))
            return types.SimpleNamespace(finish_reason='max-tokens',events=[],notifications=[],final_response='truncated')
    monkeypatch.setitem(sys.modules,'deepseek_harness',types.SimpleNamespace(DeepSeekHarness=Harness))
    result=wf_agent.invoke_sdk({'task':{'task_id':'t','node_id':'n','objective':'Write','expected_artifacts':[],'checklist':[]},
        'workspace':str(tmp_path/'ws'),'runtime_home':str(tmp_path/'home'),'session_id':'original','resume':False})
    assert calls==[('run','original'),('resume','original'),('run',None)]
    assert result['status']=='needs_input' and result['evidence']['max_tokens_retried']
