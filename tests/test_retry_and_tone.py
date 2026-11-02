import importlib.util
import json
from pathlib import Path
import sys
import wave
import pytest

SERVER = Path(__file__).resolve().parents[1] / 'server'
sys.path.insert(0, str(SERVER))
from wf_render_broker import RenderBroker
from wf_video_tools import _broker_request


def test_tone_broker_creates_real_audio_with_fixed_scope(tmp_path):
    broker = RenderBroker(tmp_path).start()
    try:
        cfg = (broker.url, broker.token)
        result = _broker_request(cfg, '/tone', {'output': 'project/public/tone.wav', 'duration': .2})
        assert result['ok'] and result['bytes'] > 8000
        with wave.open(str(tmp_path / result['output'])) as audio:
            assert audio.getnframes() == 4410 and audio.getframerate() == 22050
            assert any(audio.readframes(4410))
        for payload in ({'output': '../outside.wav'}, {'output': 'project/public/tone.wav'},
                        {'output': 'bad.wav', 'duration': 700}, {'output': 'bad.wav', 'workspace': '..'}):
            with pytest.raises(ValueError):
                _broker_request(cfg, '/tone', payload)
        assert not (tmp_path.parent / 'outside.wav').exists()
    finally:
        broker.stop()


@pytest.mark.parametrize('has_artifacts', [False, True])
def test_retry_keeps_generation_until_a_result_exists(tmp_path, monkeypatch, has_artifacts):
    spec = importlib.util.spec_from_file_location('retry_wf', SERVER / 'wf.py')
    wf = importlib.util.module_from_spec(spec); spec.loader.exec_module(wf)
    monkeypatch.setattr(wf, 'BASE', str(tmp_path))
    monkeypatch.setattr(wf, 'RUNS', str(tmp_path / '.runs'))
    monkeypatch.setattr(wf, 'LOCKFILE', str(tmp_path / '.runs/.lock'))
    evidence = [{'path': 'frozen.md', 'sha256': 'test'}] if has_artifacts else []
    node = {'id': 'writer', 'status': 'cancelled', 'attempts': 2, 'execution_generation': 1,
            'artifact_evidence': evidence, 'checks': [False], 'user_answers': ['early decision']}
    wf.write_json(tmp_path / '.runs/r/state.json', {'run_id': 'r', 'status': 'cancelled', 'nodes': [node]})
    wf.write_json(tmp_path / '.runs/r/graph.json', {'nodes': [{'id': 'writer'}], 'edges': []})
    monkeypatch.setattr(wf, '_freeze_evidence', lambda state, node: node['artifact_evidence'])
    monkeypatch.setattr(wf, '_message', lambda *a, **k: None)
    def resumed(**kwargs):
        wf.release_lock()
        return 0
    monkeypatch.setattr(wf, 'canvas_run', resumed)
    assert wf.canvas_rerun('r', 'writer', 'Use the scoped helper') == 0
    actual = wf.read_json(tmp_path / '.runs/r/state.json')['nodes'][0]
    assert actual['execution_generation'] == (2 if has_artifacts else 1)
    assert actual['attempts'] == (0 if has_artifacts else 2)
    assert actual['user_answers'][-1] == 'Use the scoped helper'
    assert bool(actual.get('generation_history')) == has_artifacts
    assert bool(actual.get('attempt_history')) != has_artifacts
