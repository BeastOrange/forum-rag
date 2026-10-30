from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _evaluation_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "evaluate_search.py"
    spec = importlib.util.spec_from_file_location("search_evaluation", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_evaluation_reports_target_recall_ranking_and_negative_returns():
    evaluate = _evaluation_module()
    rows = [
        {
            "kind": "forum",
            "expected_ids": ["a", "b"],
            "result_ids": ["x", "b"],
            "expect_empty": False,
            "seconds": [0.1, 0.2],
            "error": None,
        },
        {
            "kind": "forum",
            "expected_ids": [],
            "result_ids": ["x"],
            "expect_empty": True,
            "seconds": [0.3, 0.4],
            "error": None,
        },
        {
            "kind": "forum",
            "expected_ids": [],
            "result_ids": [],
            "expect_empty": True,
            "seconds": [0.5, 0.6],
            "error": "database locked",
        },
    ]
    metrics = evaluate.summarize(rows)
    assert metrics["known_target_recall_at_k"] == 0.5
    assert metrics["mean_reciprocal_rank"] == 0.5
    assert metrics["negative_return_rate"] == 0.5
    assert metrics["error_count"] == 1
    assert metrics["repeated_latency_seconds"]["p95"] == 0.6


def test_evaluation_does_not_count_errors_as_successful_empty_results():
    evaluate = _evaluation_module()
    metrics = evaluate.summarize(
        [
            {
                "kind": "forum",
                "expected_ids": [],
                "result_ids": [],
                "expect_empty": True,
                "seconds": [],
                "error": "broken index",
            },
        ]
    )
    assert metrics["negative_pass_count"] == 0
    assert metrics["error_count"] == 1


@pytest.mark.parametrize(
    "case",
    [
        {"id": "bad", "kind": "forum", "query": "hello"},
        {"id": "bad", "kind": "unsupported", "query": "hi", "expect_empty": True},
        {"id": "bad", "kind": "forum", "query": "hi", "expected_ids": [12]},
        {
            "id": "bad",
            "kind": "forum",
            "query": "hi",
            "expected_ids": ["a"],
            "expect_empty": True,
        },
    ],
)
def test_evaluation_rejects_ambiguous_or_invalid_labels(case):
    evaluate = _evaluation_module()
    with pytest.raises(ValueError):
        evaluate.validate_cases([case])
