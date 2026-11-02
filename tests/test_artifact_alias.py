import hashlib
import importlib.util
from pathlib import Path
import sys

SERVER=Path(__file__).resolve().parents[1]/'server'
sys.path.insert(0,str(SERVER))


def test_matching_legacy_logical_and_physical_path_is_not_hidden(tmp_path,monkeypatch):
    monkeypatch.setenv('CANVAS_HOME',str(tmp_path))
    spec=importlib.util.spec_from_file_location('legacy_alias_api',SERVER/'canvas_editor_server.py')
    api=importlib.util.module_from_spec(spec);spec.loader.exec_module(api)
    file=tmp_path/'episodes/note.md';file.parent.mkdir();file.write_bytes(b'Original legacy artifact')
    evidence={'path':'episodes/note.md','logical_path':'episodes/note.md',
        'sha256':hashlib.sha256(file.read_bytes()).hexdigest(),'bytes':file.stat().st_size}
    node={'id':'writer','module':'old-writer','status':'passed','artifact_evidence':[evidence]}
    rows,missing,counts,_=api.artifact_rows({'run_id':'run-old','episode':'note','nodes':[node]},node,
        sources={'graph':{},'modules':{}})
    assert len(rows)==1 and rows[0]['status']=='verified'
    assert counts['verified']==1 and not missing
