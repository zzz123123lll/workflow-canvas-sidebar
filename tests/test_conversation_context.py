import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from wf_conversation import append_message, message_page, stage_context
import importlib.util
_spec = importlib.util.spec_from_file_location('shipped_context_agent', Path(__file__).resolve().parents[1] / 'server/wf_agent.py')
_agent = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_agent)
build_prompt = _agent.build_prompt


def test_more_than_200_messages_keep_early_requirement_and_latest_turn(tmp_path):
    for i in range(250):
        append_message('run', 'node', 1, 'user' if i % 2 == 0 else 'assistant',
                       'EARLY-UNIQUE-OWL' if i == 0 else 'turn-' + str(i), base=tmp_path)
    context = stage_context('run', 'node', tmp_path / 'workspace', base=tmp_path)
    prompt = build_prompt({'objective': 'Continue', 'prior_messages': context['messages'],
                           'conversation_history': {k: v for k, v in context.items() if k != 'messages'}})
    assert 'EARLY-UNIQUE-OWL' in prompt
    assert 'turn-249' in prompt
    assert len((tmp_path / 'workspace/conversation/history.jsonl').read_text(encoding='utf-8').splitlines()) == 250
    newest = message_page('run', 'node', base=tmp_path, limit=100)
    assert newest['messages'][-1]['text'] == 'turn-249'
    assert newest['has_older'] and not newest['has_newer']
    previous = message_page('run', 'node', base=tmp_path, before_seq=newest['first_seq'])
    assert not ({m['id'] for m in previous['messages']} & {m['id'] for m in newest['messages']})
    incremental = message_page('run', 'node', base=tmp_path, after_seq=240)
    assert [m['seq'] for m in incremental['messages']] == list(range(241, 251))


def test_oversized_history_has_complete_source_and_explicit_omission(tmp_path):
    for i in range(15):
        append_message('run', 'n', 1, 'user', str(i) + ':' + 'x' * 10000, base=tmp_path)
    context = stage_context('run', 'n', tmp_path / 'workspace', base=tmp_path)
    prompt = build_prompt({'objective': 'Continue', 'prior_messages': context['messages'],
                           'conversation_history': {k: v for k, v in context.items() if k != 'messages'}})
    assert 'Omitted source IDs:' in prompt
    assert 'conversation/history.jsonl' in prompt
    rows = [json.loads(line) for line in (tmp_path / 'workspace/conversation/history.jsonl').read_text(encoding='utf-8').splitlines()]
    assert len(rows) == 15
    assert all(len(r['text']) > 10000 for r in rows)
