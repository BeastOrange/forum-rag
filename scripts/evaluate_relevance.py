"""Evaluate saved rankings against explicitly graded judgments; unknown IDs stay unknown."""
from __future__ import annotations

import argparse
import json
import math
from datetime import date
from pathlib import Path


def evaluate(data, rankings, k):
    if not isinstance(k, int) or isinstance(k, bool) or k < 1:
        raise ValueError('k must be positive')
    status = data.get('label_status')
    if status not in ('pending', 'machine', 'human_reviewed') or not data.get('source'):
        raise ValueError('label_status and source are required')
    if status == 'human_reviewed':
        if not isinstance(data.get('annotator'), str) or not data['annotator'].strip():
            raise ValueError('human review requires annotator')
        try:
            date.fromisoformat(data.get('reviewed_at', '')[:10])
        except (ValueError, TypeError):
            raise ValueError('human review requires ISO reviewed_at') from None
    cases = data.get('cases')
    if not isinstance(cases, list) or not cases:
        raise ValueError('cases required')
    ids = set()
    for case in cases:
        cid = case.get('id')
        if not isinstance(cid, str) or not cid or cid in ids or case.get('split') not in ('dev', 'test') or not isinstance(case.get('negative'), bool) or not isinstance(case.get('query'), str):
            raise ValueError('each case requires unique id, query, dev/test split and negative flag')
        ids.add(cid)
        judgments = case.get('judgments')
        if not isinstance(judgments, dict) or any(not isinstance(i, str) or not i or (v is not None and (isinstance(v, bool) or not isinstance(v, int) or v < 0 or v > 3)) for i, v in judgments.items()):
            raise ValueError('judgments must map IDs to null or integer grades 0..3')
        if status == 'human_reviewed' and (not judgments or any(v is None for v in judgments.values())):
            raise ValueError('human review requires completed judgments')
        if case['negative'] and any(v is not None and v > 0 for v in judgments.values()):
            raise ValueError('negative case cannot have relevant judgments')
    if not isinstance(rankings, dict) or not rankings:
        raise ValueError('rankings must map modes to case rankings')
    out = {}
    for mode, results in rankings.items():
        if mode not in ('lexical', 'semantic', 'hybrid') or not isinstance(results, dict) or set(results) != ids:
            raise ValueError('each mode must contain rankings for every case')
        for ranking in results.values():
            if not isinstance(ranking, list) or any(not isinstance(i, str) or not i for i in ranking) or len(set(ranking)) != len(ranking):
                raise ValueError('rankings must be lists of unique string IDs')
        out[mode] = {}
        for split in ('dev', 'test'):
            subset = [c for c in cases if c['split'] == split]
            if not subset:
                continue
            recalls, reciprocal, ndcgs = [], [], []
            unknown = total = negative_count = negative_relevant = negative_unknown = negative_judged_return = negative_any = 0
            for case in subset:
                found = results[case['id']][:k]
                judgments = case['judgments']
                unknown_ids = [i for i in found if judgments.get(i) is None]
                unknown += len(unknown_ids)
                total += len(found)
                if case['negative']:
                    negative_count += 1
                    negative_any += bool(found)
                    negative_relevant += any(judgments.get(i, 0) not in (None, 0) for i in found)
                    negative_unknown += bool(unknown_ids)
                    negative_judged_return += any(judgments.get(i) == 0 for i in found)
                    continue
                relevant = {i for i, grade in judgments.items() if grade is not None and grade > 0}
                if not relevant:
                    continue
                recalls.append(len(relevant.intersection(found)) / len(relevant))
                reciprocal.append(next((1 / rank for rank, i in enumerate(found, 1) if i in relevant), 0))
                dcg = sum((2 ** judgments[i] - 1) / math.log2(rank + 1) for rank, i in enumerate(found, 1) if i in relevant)
                ideal = sorted((judgments[i] for i in relevant), reverse=True)[:k]
                idcg = sum((2 ** grade - 1) / math.log2(rank + 1) for rank, grade in enumerate(ideal, 1))
                ndcgs.append(dcg / idcg)
            mean = lambda values: sum(values) / len(values) if values else None
            out[mode][split] = {'cases': len(subset), 'positive_scored_cases': len(recalls),
                'recall_at_k': mean(recalls), 'mrr': mean(reciprocal), 'ndcg_at_k': mean(ndcgs),
                'negative_cases': negative_count,
                'negative_any_return_rate': negative_any / negative_count if negative_count else None,
                'negative_relevant_return_rate': negative_relevant / negative_count if negative_count else None,
                'negative_unjudged_return_rate': negative_unknown / negative_count if negative_count else None,
                'negative_judged_irrelevant_return_rate': negative_judged_return / negative_count if negative_count else None,
                'returned_results': total, 'unjudged_results': unknown,
                'unjudged_result_rate': unknown / total if total else None}
    return {'accuracy_status': 'human_reviewed' if status == 'human_reviewed' else 'provisional',
            'label_status': status, 'source': data['source'], 'annotator': data.get('annotator'),
            'reviewed_at': data.get('reviewed_at'), 'k': k, 'modes': out,
            'limitations': ['Metrics use known judgments only; unjudged results occupy ranks but are not classified as irrelevant.',
                            'Recall and nDCG denominators contain only judged relevant IDs; incomplete pools limit interpretation.',
                            'Positive cases without judged relevant IDs are excluded from relevance metrics.']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--labels', type=Path, required=True)
    parser.add_argument('--results', type=Path, required=True, help='JSON mapping mode -> case ID -> ordered topic ID list')
    parser.add_argument('--k', type=int, default=5)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.resolve() in (args.labels.resolve(), args.results.resolve()):
        parser.error('output must differ from inputs')
    report = evaluate(json.loads(args.labels.read_text()), json.loads(args.results.read_text()), args.k)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')


if __name__ == '__main__':
    main()
