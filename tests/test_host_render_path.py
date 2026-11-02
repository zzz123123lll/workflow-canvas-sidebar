"""Host-account path gets the same scoped render broker as the SDK path."""
import importlib.util
from pathlib import Path
import sys

SERVER = Path(__file__).resolve().parents[1] / 'server'
sys.path.insert(0, str(SERVER))


def test_host_receives_expiring_scoped_render_cli(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('host_render_adapter', SERVER / 'wf_agent.py')
    adapter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adapter)
    brokers = []
    class Broker:
        def __init__(self, workspace):
            self.workspace = workspace
            self.url, self.token, self.stopped = 'http://127.0.0.1:12345', 'ephemeral-test-token', False
            brokers.append(self)
        def start(self): return self
        def stop(self): self.stopped = True
    monkeypatch.setattr(adapter.wf_render_broker, 'RenderBroker', Broker)
    monkeypatch.setattr(adapter, 'host_agent_config', lambda: {'url': 'local-host'})
    def host(task, workspace, *_args, **kwargs):
        helper = Path(workspace) / task['execution_tools']['remotion_helper']
        text = helper.read_text()
        assert helper.is_relative_to(Path(workspace))
        assert brokers[0].url in text and brokers[0].token in text
        assert not brokers[0].stopped
        return {'status': 'completed', 'summary': 'rendered'}
    monkeypatch.setattr(adapter, 'run_via_host', host)
    result = adapter.run_agent({'task_id': 'project', 'node_id': 'render', 'objective': 'Render',
        'execution_tools': {'remotion_helper': 'stale-path.py'}},
        workspace=tmp_path / 'workspace', runtime_home=tmp_path / 'runtime', session_id='')
    assert result['status'] == 'completed' and brokers[0].stopped
