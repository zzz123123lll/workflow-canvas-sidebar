"""The packaged knowledge system: attachment visibility and explainable use."""
import importlib.util
import json
from pathlib import Path
import pytest


@pytest.fixture
def kb(tmp_path, monkeypatch):
    source = (Path(__file__).resolve().parents[2] / "server") / 'kb.py'
    spec = importlib.util.spec_from_file_location('product_knowledge_fixture', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    knowledge = tmp_path / 'kb/knowledge'
    for key, value in {'BASE':tmp_path, 'KB':knowledge, 'SHARED':knowledge/'shared',
                       'TASKS':knowledge/'tasks', 'INDEX':knowledge/'index.json'}.items():
        monkeypatch.setattr(module, key, str(value))
    (knowledge/'shared').mkdir(parents=True)
    (knowledge/'tasks').mkdir()
    module.ensure_task('A')
    return module


def put(kb, rel, content):
    target = Path(kb.KB)/rel
    target.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes): target.write_bytes(content)
    else: target.write_text(content, encoding='utf-8')
    return target


def test_all_attachments_are_listed_with_truthful_processing_state(kb):
    put(kb,'tasks/A/notes.txt','中文资料')
    put(kb,'tasks/A/data.json','{"answer": 42}')
    put(kb,'tasks/A/table.csv','name,value\nA,1')
    put(kb,'tasks/A/picture.png',b'not-an-actual-image')
    put(kb,'tasks/A/archive.zip',b'PK\x03\x04')
    entries = {e['filename']:e for e in kb.list_entries(task='A')}
    assert {'notes.txt','data.json','table.csv','picture.png','archive.zip'} <= set(entries)
    assert 'manifest.json' not in entries
    assert entries['notes.txt']['parse_status']=='parsed'
    assert entries['notes.txt']['usable'] is True
    assert entries['picture.png']['parse_status']=='preview_only'
    assert entries['archive.zip']['parse_status']=='unsupported'
    assert entries['archive.zip']['usable'] is False
    assert all('bytes' in e and 'content_sha256' in e for e in entries.values())


def test_search_uses_extracted_text_and_filename_for_unsupported_files(kb):
    put(kb,'tasks/A/body.txt','正文中独特命中词')
    put(kb,'tasks/A/special-package.zip',b'PK\x03\x04')
    assert [e['filename'] for e in kb.list_entries(task='A',search='独特命中词')]==['body.txt']
    assert [e['filename'] for e in kb.list_entries(task='A',search='special-package')]==['special-package.zip']


def test_read_text_html_and_scoped_shared_content(kb):
    put(kb,'shared/policy.md','# 规范\n人工共享规范')
    put(kb,'tasks/A/page.html','<html><script>secret_script()</script><p>应显示的正文</p></html>')
    shared=kb.read_entry_content('shared/policy.md',task='A')
    assert shared['usable'] and '人工共享规范' in shared['content']
    page=kb.read_entry_content('tasks/A/page.html',task='A')
    assert '应显示的正文' in page['content'] and 'secret_script' not in page['content']
    kb.ensure_task('B');put(kb,'tasks/B/private.txt','other-task')
    with pytest.raises(ValueError): kb.read_entry_content('tasks/B/private.txt',task='A')


def test_context_deduplicates_and_records_selected_sources(kb):
    body='# 唯一资料\n同一资料不应消耗两次上下文'
    put(kb,'shared/policy.md',body)
    put(kb,'tasks/A/outputs/duplicate.md',body)
    put(kb,'tasks/A/metrics.json','{"fact": "真实数据"}')
    context=Path(kb.context_pack('A'))
    text=context.read_text(encoding='utf-8')
    assert text.count('同一资料不应消耗两次上下文')==1
    assert '真实数据' in text
    selection=json.loads(context.with_suffix('.sources.json').read_text(encoding='utf-8'))
    assert selection['duplicates']
    assert any(e['path']=='shared/policy.md' for e in selection['selected'])
    assert all(e['scope'] in ('shared','task') for e in selection['selected'])


def test_query_and_budget_prefer_relevant_content_and_explain_truncation(kb):
    put(kb,'shared/rule.md','# 全局规范\n必须标来源')
    put(kb,'tasks/A/aaa-unrelated.md','#  unrelated\n'+'无关填充 '*2000)
    put(kb,'tasks/A/zzz-relevant.md','# 配音\n语速与旁白 '+('有声文字 '*2000))
    context=Path(kb.context_pack('A',query='配音 旁白',budget_chars=2000))
    text=context.read_text(encoding='utf-8')
    selection=json.loads(context.with_suffix('.sources.json').read_text(encoding='utf-8'))
    assert len(text)<=2000
    assert '必须标来源' in text and '语速与旁白' in text
    assert selection['truncated'] is True
    assert selection['selected'][0]['scope']=='shared'
    assert any(e['path'].endswith('zzz-relevant.md') for e in selection['selected'])


def test_shared_listing_never_reads_other_task_partitions(kb, monkeypatch):
    kb.ensure_task('B');put(kb,'shared/s.md','共享');put(kb,'tasks/B/private.txt','私有')
    original=kb.read_entry_content; inspected=[]
    def observe(rel,*args,**kwargs):
        inspected.append(rel)
        return original(rel,*args,**kwargs)
    monkeypatch.setattr(kb,'read_entry_content',observe)
    assert all(e['scope']=='shared' for e in kb.list_entries(scope='shared'))
    assert inspected and all(p.startswith('shared/') for p in inspected)


def test_pdf_without_extractable_text_has_explicit_state(kb):
    pypdf = pytest.importorskip('pypdf')
    target = Path(kb.KB)/'tasks/A/blank.pdf'
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=72, height=72)
    with target.open('wb') as handle: writer.write(handle)
    result = kb.read_entry_content('tasks/A/blank.pdf', task='A')
    assert result['kind']=='pdf' and result['parse_status']=='no_text'
    assert result['usable'] is False and 'OCR' in result['parse_message']


def test_generated_drafts_are_distinct_from_shared_references(kb):
    put(kb,'shared/style.md','# Style\nReference')
    put(kb,'tasks/A/outputs/reply.txt','Generated response')
    shared=kb.read_entry_content('shared/style.md',task='A')
    draft=kb.read_entry_content('tasks/A/outputs/reply.txt',task='A')
    assert shared['source_type']=='shared_reference'
    assert draft['source_type']=='generated_draft' and draft['review_status']=='unreviewed'


def test_context_selection_never_imports_another_task(kb):
    kb.ensure_task('B')
    put(kb,'tasks/A/notes.txt','Own text')
    put(kb,'tasks/B/private.txt','SECRET-OTHER-PARTITION')
    context=Path(kb.context_pack('A', query='SECRET-OTHER-PARTITION'))
    assert 'SECRET-OTHER-PARTITION' not in context.read_text(encoding='utf-8')
    selection=json.loads(context.with_suffix('.sources.json').read_text(encoding='utf-8'))
    assert not any('/B/' in row['path'] for row in selection['selected'])


def test_same_basename_in_different_formats_has_distinct_identity(kb):
    put(kb,'tasks/A/notes.md','# Markdown')
    put(kb,'tasks/A/notes.txt','Plain text')
    entries=[e for e in kb.list_entries(task='A') if e['filename'] in ('notes.md','notes.txt')]
    assert len(entries)==2 and len({e['id'] for e in entries})==2
