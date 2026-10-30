import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "bench", Path(__file__).parents[1] / "scripts/benchmark_concurrency.py"
)
bench = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench)


def test_summary_keeps_failures_in_denominator_and_compares_same_query():
    rows = [
        {"query": "a", "seconds": 1, "ids": ["1"], "error": None, "timeout": False},
        {"query": "a", "seconds": 2, "ids": ["2"], "error": None, "timeout": False},
        {"query": "b", "seconds": 3, "ids": [], "error": "timeout", "timeout": True},
    ]
    result = bench.summarize(rows, 2, 4)
    assert result["requests"] == 3
    assert result["errors"] == 1 and result["timeouts"] == 1
    assert result["throughput_requests_per_second"] == 0.75
    assert result["latency_seconds"]["p95"] == 3
    assert result["consistency"]["compared_queries"] == 1
    assert result["consistency"]["inconsistent_queries"] == ["a"]


def test_concurrent_requests_record_errors_and_completed_ids():
    import asyncio

    active = 0
    peak = 0

    async def call(query):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.005)
        active -= 1
        if query == "bad":
            raise ValueError("bad")
        return {"results": [{"topic_id": "1"}]}

    result = asyncio.run(bench.benchmark(call, ["ok", "bad"], 2, 4, 1))
    assert peak == 2
    assert result["summary"]["requests"] == 4
    assert result["summary"]["errors"] == 2
    assert result["summary"]["consistency"]["compared_queries"] == 1
