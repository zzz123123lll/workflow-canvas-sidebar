import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest

SERVER=Path(__file__).resolve().parents[1]/'server'
sys.path.insert(0,str(SERVER))
from wf_projects import ProjectStore, Conflict
from wf_sharing import export_project, import_project, checked


@pytest.fixture
def sharing(tmp_path, monkeypatch):
    monkeypatch.setenv('CANVAS_HOME',str(tmp_path))
    spec=importlib.util.spec_from_file_location('sharing_wf',SERVER/'wf.py')
    wf=importlib.util.module_from_spec(spec);spec.loader.exec_module(wf)
    monkeypatch.setattr(wf,'BASE',str(tmp_path));monkeypatch.setattr(wf,'MODULES_PATH',str(tmp_path/'kb/modules.json'))
    wf.write_json(wf.MODULES_PATH,{'modules':[{'id':'writer','name':'Writer','executor':'agent',
        'agent_prompt':'Write the project note','artifacts':['episodes/{stem}-note.md'],'requires':[],
        'private_runtime_config':{'token':'should-not-export'}}]})
    store=ProjectStore(tmp_path)
    project=store.mutate('create',{'request_id':'create','name':'Example','goal':'Write',
        'graph':{'nodes':[{'id':'write','module':'writer','host_session_id':'should-not-export'}],'edges':[],
                 'account':'should-not-export'}})['project']
    files=tmp_path/'kb/knowledge/tasks'/project['project_id']/'files';files.mkdir(parents=True)
    (files/'source.md').write_text('Original synthetic source',encoding='utf-8')
    return tmp_path,store,wf,project


def test_project_roundtrip_is_explicit_and_does_not_copy_private_state(sharing):
    base,store,wf,project=sharing
    package=export_project(base,store,wf,project['project_id'])
    assert package['materials']==[]
    assert 'should-not-export' not in json.dumps(package)
    package=export_project(base,store,wf,project['project_id'],materials=True)
    assert len(package['materials'])==1
    body={'request_id':'import-once','package':package}
    first=import_project(base,store,wf,body);second=import_project(base,store,wf,body)
    assert first['project']['project_id']==second['project']['project_id']!=project['project_id']
    assert first['project']['graph']['nodes'][0]['module'].startswith('shared-')
    assert not store.runs(first['project']['project_id'])
    copied=base/'kb/knowledge/tasks'/first['project']['project_id']/'files/source.md'
    assert copied.read_text(encoding='utf-8')=='Original synthetic source'
    changed=json.loads(json.dumps(package));changed['project']['goal']='Different content'
    with pytest.raises(Conflict):import_project(base,store,wf,dict(body,package=changed))


def test_imported_project_reads_its_own_material_references(sharing):
    base,store,wf,project=sharing
    graph=project['graph']
    graph['nodes'][0]['required_knowledge']=['tasks/'+project['project_id']+'/files/source.md']
    project=store.mutate('update',{'project_id':project['project_id'],'expected_revision':project['revision'],
        'request_id':'bind-source','graph':graph})['project']
    package=export_project(base,store,wf,project['project_id'],materials=True)
    assert package['project']['graph']['nodes'][0]['required_knowledge']==['files/source.md']
    imported=import_project(base,store,wf,{'request_id':'import-with-reference','package':package})['project']
    reference=wf._knowledge_requirement(imported['graph']['nodes'][0]['required_knowledge'][0],imported['project_id'])
    assert reference=='tasks/'+imported['project_id']+'/files/source.md'
    assert (base/'kb/knowledge'/reference).read_text(encoding='utf-8')=='Original synthetic source'


@pytest.mark.parametrize('name',['../outside.md','nested/../../outside.md','C:/outside.md','nested\\outside.md','a//b.md','CON.txt'])
def test_import_rejects_invalid_material_paths_before_creating_project(sharing,name):
    base,store,wf,project=sharing
    package=export_project(base,store,wf,project['project_id'])
    raw=b'synthetic';package['materials']=[{'path':name,'base64':base64.b64encode(raw).decode(),
        'sha256':hashlib.sha256(raw).hexdigest()}]
    before=len(store.list())
    with pytest.raises(ValueError):import_project(base,store,wf,{'request_id':'bad','package':package})
    assert len(store.list())==before


def test_sharing_does_not_traverse_a_linked_material_root(sharing):
    base,store,wf,project=sharing
    target=base/'outside';target.mkdir();(target/'data.md').write_text('private',encoding='utf-8')
    link=base/'linked'
    try:link.symlink_to(target,target_is_directory=True)
    except OSError:pytest.skip('Creating symlinks needs developer mode/privilege on this Windows runner')
    with pytest.raises(ValueError):checked(link,'data.md')
