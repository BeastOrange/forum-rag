import copy
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / 'scripts' / 'evaluate_task_evidence.py'
spec = importlib.util.spec_from_file_location('task_evaluation', SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def fixture_data():
    runs = {'cases': [
        {'case_id': 'one', 'task': 'Find the answer', 'source_kind': 'real_user', 'source_ref': 'local-log:1',
         'answer': {'status': 'answered', 'text': 'A cited excerpt.'},
         'tool_calls': [{'call_id': 'c1', 'tool': 'search', 'local': True, 'error': None}],
         'evidence': [{'evidence_id': 'e1', 'call_id': 'c1', 'source_id': 'post:1', 'source_hash': 'sha256:123', 'verbatim': 'A cited excerpt.'}]},
        {'case_id': 'two', 'task': 'Find a second answer', 'source_kind': 'source_derived', 'source_ref': 'post:2',
         'answer': {'status': 'answered', 'text': 'An invented answer.'},
         'tool_calls': [{'call_id': 'c2', 'tool': 'search', 'local': True, 'error': 'timeout'}], 'evidence': []},
        {'case_id': 'three', 'task': 'Review a third question', 'source_kind': 'synthetic', 'source_ref': 'fixture:3',
         'answer': {'status': 'not_recorded', 'text': None}, 'tool_calls': [], 'evidence': []},
    ]}
    judgments = {'cases': [
        {'case_id': 'one', 'status': 'human_reviewed', 'source': 'manual', 'reviewer': 'Reviewer', 'reviewed_at': '2026-10-28', 'answer_supported': True, 'needs_online': False, 'unsupported_claim': False, 'reason': 'Evidence supports the answer.', 'evidence_ids': ['e1'], 'unsupported_quotes': []},
        {'case_id': 'two', 'status': 'machine', 'source': 'machine review', 'reviewer': None, 'reviewed_at': None, 'answer_supported': False, 'needs_online': True, 'unsupported_claim': True, 'reason': 'The recorded answer invents a result.', 'evidence_ids': [], 'unsupported_quotes': ['An invented answer.']},
        {'case_id': 'three', 'status': 'pending', 'source': 'unreviewed', 'reviewer': None, 'reviewed_at': None, 'answer_supported': None, 'needs_online': None, 'unsupported_claim': None, 'reason': 'Waiting.', 'evidence_ids': [], 'unsupported_quotes': []},
    ]}
    return runs, judgments


def test_unknown_and_failure_stay_in_full_denominator():
    runs, judgments = fixture_data()
    report = module.evaluate(runs, judgments)
    assert report['total_cases'] == 3
    assert report['tool_failure_cases'] == 1
    assert report['human_reviewed_cases'] == 1
    assert report['pending_cases'] == 1
    assert report['local_tool_calls'] == 2
    assert report['answer_supported']['true_count'] == 1
    assert report['answer_supported']['unknown_count'] == 1
    assert report['answer_supported']['all_cases_rate'] == pytest.approx(1/3)
    assert report['answer_supported']['judged_rate'] == pytest.approx(1/2)
    assert report['needs_online']['judged_rate'] == pytest.approx(1/2)
    assert report['unsupported_claim']['judged_rate'] == pytest.approx(1/2)


@pytest.mark.parametrize('change', ['human_attestation', 'bad_evidence', 'missing', 'duplicate', 'error_supported', 'pending_claim', 'source', 'bad_boolean', 'missing_field', 'empty_evidence'])
def test_invalid_or_inconsistent_records_are_rejected(change):
    runs, judgments = fixture_data()
    if change == 'human_attestation': judgments['cases'][0]['reviewer'] = None
    if change == 'bad_evidence': judgments['cases'][0]['evidence_ids'] = ['other']
    if change == 'missing': judgments['cases'].pop()
    if change == 'duplicate': runs['cases'].append(copy.deepcopy(runs['cases'][0]))
    if change == 'error_supported': judgments['cases'][1]['answer_supported'] = True
    if change == 'pending_claim': judgments['cases'][2]['needs_online'] = False
    if change == 'source': runs['cases'][0]['source_kind'] = 'user'
    if change == 'bad_boolean': judgments['cases'][0]['needs_online'] = 1
    if change == 'missing_field': del judgments['cases'][1]['reason']
    if change == 'empty_evidence': judgments['cases'][0]['evidence_ids'] = []
    with pytest.raises(ValueError): module.evaluate(runs, judgments)


def test_cli_output_and_refuses_to_overwrite_input(tmp_path):
    runs, judgments = fixture_data()
    r = tmp_path / 'runs.json'; j = tmp_path / 'judgments.json'; out = tmp_path / 'report.json'
    r.write_text(json.dumps(runs)); j.write_text(json.dumps(judgments))
    proc = subprocess.run([sys.executable, str(SCRIPT), '--runs', str(r), '--judgments', str(j), '--output', str(out)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(out.read_text())['total_cases'] == 3
    before = r.read_bytes()
    proc = subprocess.run([sys.executable, str(SCRIPT), '--runs', str(r), '--judgments', str(j), '--output', str(r)], capture_output=True, text=True)
    assert proc.returncode != 0 and r.read_bytes() == before


def test_all_unknown_produces_null_judged_rates():
    runs, judgments = fixture_data()
    for review in judgments['cases']:
        review.update(status='pending', reviewer=None, reviewed_at=None,
                      answer_supported=None, needs_online=None, unsupported_claim=None, unsupported_quotes=[])
    report = module.evaluate(runs, judgments)
    for metric in ('answer_supported', 'needs_online', 'unsupported_claim'):
        assert report[metric]['unknown_count'] == 3
        assert report[metric]['judged_rate'] is None
        assert report[metric]['all_cases_rate'] == 0


def test_failed_call_cannot_supply_evidence_or_support():
    runs, judgments = fixture_data()
    runs['cases'][0]['tool_calls'][0]['error'] = 'failed'
    with pytest.raises(ValueError, match='successful'):
        module.evaluate(runs, judgments)


def test_extra_or_duplicate_judgment_is_rejected():
    runs, judgments = fixture_data()
    judgments['cases'].append(copy.deepcopy(judgments['cases'][0]))
    with pytest.raises(ValueError, match='exactly one'):
        module.evaluate(runs, judgments)


def test_human_review_date_and_remote_calls_are_rejected():
    runs, judgments = fixture_data()
    judgments['cases'][0]['reviewed_at'] = '2026-02-30'
    with pytest.raises(ValueError, match='ISO date'):
        module.evaluate(runs, judgments)
    runs, judgments = fixture_data()
    runs['cases'][0]['tool_calls'][0]['local'] = False
    with pytest.raises(ValueError, match='local'):
        module.evaluate(runs, judgments)


def test_supported_answer_can_recover_from_another_failed_call():
    runs, judgments = fixture_data()
    runs['cases'][0]['tool_calls'].append({'call_id': 'c3', 'tool': 'lookup', 'local': True, 'error': 'timeout'})
    report = module.evaluate(runs, judgments)
    assert report['answer_supported']['true_count'] == 1
    assert report['tool_failure_cases'] == 2


def test_missing_answer_cannot_be_judged_and_claim_requires_verbatim_span():
    runs, judgments = fixture_data()
    judgments['cases'][2].update(status='machine', unsupported_claim=False)
    with pytest.raises(ValueError, match='recorded answer'):
        module.evaluate(runs, judgments)
    runs, judgments = fixture_data()
    judgments['cases'][1]['unsupported_quotes'] = ['Not in the answer']
    with pytest.raises(ValueError, match='verbatim'):
        module.evaluate(runs, judgments)


def test_recorded_abstention_is_distinct_from_missing_answer():
    runs, judgments = fixture_data()
    runs['cases'][1]['answer'] = {'status': 'abstained', 'text': 'I could not find sufficient evidence.'}
    judgments['cases'][1].update(unsupported_claim=False, unsupported_quotes=[])
    report = module.evaluate(runs, judgments)
    assert report['abstained_cases'] == 1
    assert report['unrecorded_answer_cases'] == 1
