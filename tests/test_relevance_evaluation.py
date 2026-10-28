import importlib.util
from pathlib import Path
import pytest

spec = importlib.util.spec_from_file_location('relevance', Path(__file__).parents[1] / 'scripts/evaluate_relevance.py')
relevance = importlib.util.module_from_spec(spec)
spec.loader.exec_module(relevance)


def dataset(status='pending'):
    return {'label_status': status, 'source': 'test', 'annotator': None, 'reviewed_at': None,
            'cases': [{'id': 'p', 'query': 'x', 'split': 'test', 'negative': False,
                       'judgments': {'a': 2, 'b': 1, 'c': 0}},
                      {'id': 'n', 'query': 'z', 'split': 'test', 'negative': True,
                       'judgments': {'c': 0}}]}


def test_unknowns_are_reported_and_not_assumed_irrelevant():
    report = relevance.evaluate(dataset(), {'hybrid': {'p': ['unknown', 'a'], 'n': ['unknown', 'c']}}, 2)
    summary = report['modes']['hybrid']['test']
    assert report['accuracy_status'] == 'provisional'
    assert summary['recall_at_k'] == .5
    assert summary['mrr'] == .5
    assert summary['unjudged_result_rate'] == .5
    assert summary['negative_relevant_return_rate'] == 0
    assert summary['negative_unjudged_return_rate'] == 1
    assert summary['negative_judged_irrelevant_return_rate'] == 1
    assert 0 < summary['ndcg_at_k'] < 1


def test_human_review_requires_attestation_and_full_judgments():
    data = dataset('human_reviewed')
    with pytest.raises(ValueError):
        relevance.evaluate(data, {'lexical': {'p': ['a'], 'n': []}}, 5)
    data.update(annotator='Person', reviewed_at='2026-10-27', source='human review')
    data['cases'][0]['judgments']['a'] = None
    with pytest.raises(ValueError):
        relevance.evaluate(data, {'lexical': {'p': ['a'], 'n': []}}, 5)
