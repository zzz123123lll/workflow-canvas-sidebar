import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'server'))
import wf


def test_three_original_starters_are_executable_definitions(monkeypatch):
    seed=ROOT/'seed/kb'
    monkeypatch.setattr(wf,'MODULES_PATH',str(seed/'modules.json'))
    index=json.loads((seed/'workflows/index.json').read_text(encoding='utf-8'))
    assert {t['id'] for t in index['workflows']}=={'wc17-article','wc17-motion-video','wc17-file-task'}
    for template in index['workflows']:
        definition=json.loads((seed/'workflows'/(template['id']+'.json')).read_text(encoding='utf-8'))
        wf._validate_canvas(definition['graph'])
    modules=wf.load_modules()
    assert all(m['executor']=='agent' and m['id'].startswith('wc17-') for m in modules)
    assert all(not m.get('skills') and isinstance(m.get('deliveries'),dict) for m in modules)
    assert all(m.get('capabilities')==['video.remotion'] for m in modules if m['id'] in {'wc17-motion-design','wc17-motion-render'})


def test_upgrading_starters_keeps_user_edits_and_is_reentrant(tmp_path):
    spec=importlib.util.spec_from_file_location('seed_upgrade',ROOT/'scripts/seed.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    home=tmp_path/'中文 空格'
    first=module.install(home,ROOT/'seed')
    assert len(first['added_modules'])==6 and len(first['added_templates'])==3
    path=home/'kb/modules.json';data=json.loads(path.read_text(encoding='utf-8'))
    data['modules'][0]['agent_prompt']='User revised this module'
    path.write_text(json.dumps(data),encoding='utf-8');before=path.read_bytes()
    second=module.install(home,ROOT/'seed')
    assert not second['added_modules'] and not second['added_templates']
    assert path.read_bytes()==before


@pytest.mark.parametrize('has_index', [True, False])
def test_api_startup_upgrades_existing_home_without_running_installer(tmp_path, monkeypatch, has_index):
    monkeypatch.setenv('CANVAS_HOME', str(tmp_path))
    source = ROOT / 'server/canvas_editor_server.py'
    spec = importlib.util.spec_from_file_location('market_startup_api', source)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    (tmp_path / 'kb/workflows').mkdir(parents=True)
    legacy = {'id': 'user-module', 'agent_prompt': 'Keep my instructions'}
    modules_path = tmp_path / 'kb/modules.json'
    modules_path.write_text(json.dumps({'modules': [legacy]}), encoding='utf-8')
    index_path = tmp_path / 'kb/workflows/index.json'
    if has_index:
        index_path.write_text(json.dumps({'active': 'user-template', 'workflows': [{'id': 'user-template'}]}), encoding='utf-8')
    else:
        (tmp_path / 'kb/canvas-workflow.json').write_text(json.dumps({'nodes': [{'id': 'user-node', 'module': 'user-module'}], 'edges': []}), encoding='utf-8')
    class Server:
        server_address = ('127.0.0.1', 39999)
        def __init__(self, *args): pass
        def serve_forever(self): raise StopIteration
    monkeypatch.setattr(module, 'CanvasHTTPServer', Server)
    monkeypatch.setattr(module.threading, 'Thread', lambda **kwargs: SimpleNamespace(start=lambda: None))
    with pytest.raises(StopIteration): module.main()
    modules = json.loads(modules_path.read_text(encoding='utf-8'))['modules']
    assert modules[0] == legacy
    assert len(modules) == 7
    index = json.loads(index_path.read_text(encoding='utf-8'))
    assert index['active'] == ('user-template' if has_index else 'video')
    assert len(index['workflows']) == 4
    article = json.loads((tmp_path / 'kb/workflows/wc17-article.json').read_text(encoding='utf-8'))
    original_article = json.loads((ROOT / 'seed/kb/workflows/wc17-article.json').read_text(encoding='utf-8'))
    assert article['graph'] == original_article['graph']
    before = modules_path.read_bytes(), index_path.read_bytes()
    with pytest.raises(StopIteration): module.main()
    assert (modules_path.read_bytes(), index_path.read_bytes()) == before
