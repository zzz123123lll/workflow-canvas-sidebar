import importlib.util
import json
from pathlib import Path
import sys

import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'server'))
from wf_projects import ProjectStore
spec=importlib.util.spec_from_file_location('data_migration',ROOT/'scripts/migrate.py')
migration=importlib.util.module_from_spec(spec);spec.loader.exec_module(migration)


def test_copy_migration_preserves_source_uses_sqlite_backup_and_excludes_machine_bridge(tmp_path):
    source=tmp_path/'source';source.mkdir();(source/'kb').mkdir()
    (source/'kb/modules.json').write_text('{"modules":[]}',encoding='utf-8')
    (source/'kb/execution-tools.json').write_text('{"node":"machine-specific"}',encoding='utf-8')
    store=ProjectStore(source);project=store.mutate('create',{'request_id':'create','name':'Test','goal':'Synthetic'})['project']
    (source/'.runs/host-agent.json').write_text('{"token":"ephemeral-test"}',encoding='utf-8')
    before=(source/'kb/modules.json').read_bytes()
    destination=tmp_path/'中文 空格'/'data'
    result=migration.migrate(source,destination)
    assert result['sqlite_consistent_backup'] and result['source_preserved']
    assert ProjectStore(destination).get(project['project_id'])['goal']=='Synthetic'
    assert (source/'kb/modules.json').read_bytes()==before
    assert not (destination/'.runs/host-agent.json').exists()
    assert not (destination/'kb/execution-tools.json').exists()
    marker=json.loads((destination/'.workflow-canvas-home.json').read_text(encoding='utf-8'))
    assert marker['exclusive'] and marker['data_home']==str(destination)


def test_nonempty_target_and_nested_destination_are_not_moved_or_modified(tmp_path):
    source=tmp_path/'source';(source/'kb').mkdir(parents=True)
    (source/'kb/modules.json').write_text('{"modules":[]}',encoding='utf-8')
    target=tmp_path/'unrelated';target.mkdir();sentinel=target/'keep.txt';sentinel.write_text('Keep',encoding='utf-8')
    with pytest.raises(ValueError):migration.migrate(source,target)
    assert sentinel.read_text(encoding='utf-8')=='Keep'
    with pytest.raises(ValueError):migration.migrate(source,source/'nested')
    assert not (source/'nested').exists()
