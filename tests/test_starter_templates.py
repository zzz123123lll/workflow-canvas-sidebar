import importlib.util
import json
from pathlib import Path
import sys

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
