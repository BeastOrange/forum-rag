"""Score saved task runs against independent judgments, without executing tools.

Example run case: {"case_id":"1", "task":"Find X", "source_kind":"synthetic",
 "source_ref":"fixture:1", "answer":{"status":"answered","text":"X"}, "tool_calls":[{"call_id":"c", "tool":"search",
 "local":true, "error":null}], "evidence":[{"evidence_id":"e", "call_id":"c",
 "source_id":"post:1", "source_hash":"sha256:...", "verbatim":"excerpt"}]}.
Example judgment case: {"case_id":"1", "status":"machine", "source":"review",
 "reviewer":null, "reviewed_at":null, "answer_supported":true,
 "needs_online":null, "unsupported_claim":false, "reason":"Evidence supports X",
 "evidence_ids":["e"], "unsupported_quotes":[]}. Each input is an object with a nonempty "cases" list.

Run: python scripts/evaluate_task_evidence.py --runs runs.json --judgments judgments.json --output report.json
"""
from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path


RUN_FIELDS = {'case_id', 'task', 'source_kind', 'source_ref', 'answer', 'tool_calls', 'evidence'}
CALL_FIELDS = {'call_id', 'tool', 'local', 'error'}
EVIDENCE_FIELDS = {'evidence_id', 'call_id', 'source_id', 'source_hash', 'verbatim'}
JUDGMENT_FIELDS = {'case_id', 'status', 'source', 'reviewer', 'reviewed_at',
                   'answer_supported', 'needs_online', 'unsupported_claim', 'reason', 'evidence_ids', 'unsupported_quotes'}
METRICS = ('answer_supported', 'needs_online', 'unsupported_claim')


def _object(value, fields, label):
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError(f'{label} must contain exactly {sorted(fields)}')


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{label} must be a nonempty string')


def _cases(data, label):
    _object(data, {'cases'}, label)
    if not isinstance(data['cases'], list) or not data['cases']:
        raise ValueError(f'{label} requires a nonempty cases list')
    return data['cases']


def evaluate(runs, judgments):
    """Validate both inputs and compute explicitly denominated case-level metrics."""
    records = {}
    total_calls = 0
    for run in _cases(runs, 'runs'):
        _object(run, RUN_FIELDS, 'run')
        for name in ('case_id', 'task', 'source_ref'):
            _text(run[name], name)
        cid = run['case_id']
        if cid in records:
            raise ValueError('duplicate run case_id')
        if run['source_kind'] not in ('real_user', 'source_derived', 'synthetic'):
            raise ValueError('invalid source_kind')
        if not isinstance(run['tool_calls'], list) or not isinstance(run['evidence'], list):
            raise ValueError('tool_calls and evidence must be lists')
        answer = run['answer']
        _object(answer, {'status', 'text'}, 'answer')
        if answer['status'] not in ('answered', 'abstained', 'not_recorded'):
            raise ValueError('invalid answer status')
        if answer['status'] == 'not_recorded':
            if answer['text'] is not None:
                raise ValueError('unrecorded answer text must be null')
        else:
            _text(answer['text'], 'answer text')
            if len(answer['text']) > 20000:
                raise ValueError('answer exceeds 20000 characters')
        calls = {}
        for call in run['tool_calls']:
            _object(call, CALL_FIELDS, 'tool call')
            for name in ('call_id', 'tool'):
                _text(call[name], name)
            if call['call_id'] in calls or call['local'] is not True:
                raise ValueError('tool calls must be unique and local')
            if call['error'] is not None:
                _text(call['error'], 'tool error')
            calls[call['call_id']] = call
        evidence = {}
        for item in run['evidence']:
            _object(item, EVIDENCE_FIELDS, 'evidence')
            for name in EVIDENCE_FIELDS:
                _text(item[name], name)
            if len(item['verbatim']) > 20000:
                raise ValueError('verbatim evidence exceeds 20000 characters')
            if item['evidence_id'] in evidence or item['call_id'] not in calls or calls[item['call_id']]['error'] is not None:
                raise ValueError('evidence must be unique and linked to a successful local call')
            evidence[item['evidence_id']] = item
        total_calls += len(calls)
        records[cid] = {'run': run, 'calls': calls, 'evidence': evidence}

    reviews = {}
    for judgment in _cases(judgments, 'judgments'):
        _object(judgment, JUDGMENT_FIELDS, 'judgment')
        for name in ('case_id', 'source', 'reason'):
            _text(judgment[name], name)
        cid = judgment['case_id']
        if cid not in records or cid in reviews:
            raise ValueError('judgment case_id must identify exactly one run')
        if judgment['status'] not in ('pending', 'machine', 'human_reviewed'):
            raise ValueError('invalid judgment status')
        if judgment['status'] == 'human_reviewed':
            _text(judgment['reviewer'], 'reviewer')
            try:
                date.fromisoformat(judgment['reviewed_at'])
            except (TypeError, ValueError):
                raise ValueError('human review requires an ISO date') from None
        else:
            for name in ('reviewer', 'reviewed_at'):
                if judgment[name] is not None:
                    _text(judgment[name], name)
        for name in METRICS:
            if judgment[name] is not None and type(judgment[name]) is not bool:
                raise ValueError(f'{name} must be true, false or null')
            if judgment['status'] == 'pending' and judgment[name] is not None:
                raise ValueError('pending judgments must remain unknown')
        ids = judgment['evidence_ids']
        if not isinstance(ids, list) or any(not isinstance(i, str) or not i for i in ids):
            raise ValueError('evidence_ids must be a list of IDs')
        if len(set(ids)) != len(ids) or not set(ids).issubset(records[cid]['evidence']):
            raise ValueError('evidence references must be unique and belong to this case')
        answer = records[cid]['run']['answer']
        if answer['status'] == 'not_recorded' and any(
                judgment[name] is not None for name in ('answer_supported', 'unsupported_claim')):
            raise ValueError('answer-level judgments require a recorded answer')
        if judgment['answer_supported'] is True:
            if not ids or answer['status'] != 'answered':
                raise ValueError('supported answers require evidence and an answered status')
        quotes = judgment['unsupported_quotes']
        if not isinstance(quotes, list) or any(not isinstance(q, str) or not q.strip() for q in quotes):
            raise ValueError('unsupported_quotes must be a list of nonempty strings')
        if judgment['unsupported_claim'] is True:
            if not quotes or any(q not in (answer['text'] or '') for q in quotes):
                raise ValueError('unsupported claims require verbatim quotes from the recorded answer')
        elif quotes:
            raise ValueError('unsupported_quotes must be empty unless unsupported_claim is true')
        reviews[cid] = judgment
    if set(reviews) != set(records):
        raise ValueError('each run requires exactly one judgment')

    count = len(records)
    report = {'total_cases': count,
              'answered_cases': sum(x['run']['answer']['status'] == 'answered' for x in records.values()),
              'abstained_cases': sum(x['run']['answer']['status'] == 'abstained' for x in records.values()),
              'unrecorded_answer_cases': sum(x['run']['answer']['status'] == 'not_recorded' for x in records.values()),
              'tool_failure_cases': sum(any(c['error'] is not None for c in x['calls'].values()) for x in records.values()),
              'human_reviewed_cases': sum(j['status'] == 'human_reviewed' for j in reviews.values()),
              'unreviewed_cases': sum(j['status'] != 'human_reviewed' for j in reviews.values()),
              'machine_cases': sum(j['status'] == 'machine' for j in reviews.values()),
              'pending_cases': sum(j['status'] == 'pending' for j in reviews.values()),
              'source_kind_counts': {kind: sum(x['run']['source_kind'] == kind for x in records.values()) for kind in ('real_user', 'source_derived', 'synthetic')},
              'local_tool_calls': total_calls}
    for name in METRICS:
        values = [j[name] for j in reviews.values()]
        true_count = sum(v is True for v in values)
        judged = sum(v is not None for v in values)
        report[name] = {'true_count': true_count, 'false_count': judged - true_count,
                        'unknown_count': count - judged, 'judged_count': judged,
                        'all_cases_rate': true_count / count,
                        'judged_rate': true_count / judged if judged else None}
    report['limitations'] = [
        'All-case rates count confirmed true cases over all tasks; unknown cases are not judged false.',
        'answer_supported judges the saved substantive answer against cited local evidence; abstention is not a supported answer.',
        'needs_online is a reviewer inference, not measured online calls or savings.',
        'source_derived and synthetic tasks are not real-user logs; source claims are not independently verified.',
        'Machine judgments are not human accuracy; evidence text and source hashes are recorded, not independently verified.',
    ]
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runs', type=Path, required=True)
    parser.add_argument('--judgments', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    for path in (args.runs, args.judgments):
        if args.output.resolve() == path.resolve() or (args.output.exists() and path.exists() and args.output.samefile(path)):
            parser.error('output must differ from input files')
    report = evaluate(json.loads(args.runs.read_text()), json.loads(args.judgments.read_text()))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')


if __name__ == '__main__':
    main()
