"""Read-only search probes with explicit labels and reproducible latency samples.

Run with the intended source tree on PYTHONPATH. Labels identify known relevant
targets, not an exhaustive relevance judgment. First calls are not OS-cold runs.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import platform
import sqlite3
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def validate_cases(cases: list[dict[str, Any]]) -> None:
    seen = set()
    for case in cases:
        if not isinstance(case.get("id"), str) or case["id"] in seen:
            raise ValueError("Cases require unique string ids")
        seen.add(case["id"])
        if case.get("kind") not in {"forum", "exact", "docs"}:
            raise ValueError("kind must be forum, exact, or docs")
        if not isinstance(case.get("query"), str):
            raise ValueError("query must be a string")
        targets = case.get("expected_ids", [])
        if not isinstance(targets, list) or any(
            not isinstance(x, str) for x in targets
        ):
            raise ValueError("expected_ids must contain strings")
        empty = case.get("expect_empty", False)
        if not isinstance(empty, bool) or bool(targets) == empty:
            raise ValueError("Specify expected_ids or expect_empty=true, exclusively")


def _latencies(values: list[float]) -> dict[str, float | int | None]:
    ordered = sorted(values)
    return {
        "count": len(values),
        "p50": ordered[math.ceil(len(ordered) * 0.5) - 1] if ordered else None,
        "p95": ordered[math.ceil(len(ordered) * 0.95) - 1] if ordered else None,
        "max": ordered[-1] if ordered else None,
    }


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    positives = [r for r in rows if r["expected_ids"]]
    negatives = [r for r in rows if r["expect_empty"]]
    recalls, ranks = [], []
    for row in positives:
        expected = set(row["expected_ids"])
        found = row["result_ids"] if not row["error"] else []
        recalls.append(len(expected.intersection(found)) / len(expected))
        ranks.append(
            next((1 / i for i, item in enumerate(found, 1) if item in expected), 0)
        )
    return {
        "case_count": len(rows),
        "positive_case_count": len(positives),
        "negative_case_count": len(negatives),
        "known_target_recall_at_k": sum(recalls) / len(recalls) if recalls else None,
        "mean_reciprocal_rank": sum(ranks) / len(ranks) if ranks else None,
        "negative_return_rate": sum(bool(r["result_ids"]) for r in negatives)
        / len(negatives)
        if negatives
        else None,
        "negative_pass_count": sum(
            not r["error"] and not r["result_ids"] for r in negatives
        ),
        "error_count": sum(bool(r["error"]) for r in rows),
        "first_call_latency_seconds": _latencies(
            [r["seconds"][0] for r in rows if r["seconds"]]
        ),
        "repeated_latency_seconds": _latencies(
            [t for r in rows for t in r["seconds"][1:]]
        ),
    }


def _metadata(db: Path) -> dict[str, Any]:
    with sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True) as conn:
        names = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        counts = {
            name: conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            for name in (
                "topics",
                "chunks",
                "documents",
                "doc_chunks",
                "knowledge_pages",
            )
            if name in names
        }
        backends = (
            [
                dict(backend_id=r[0], count=r[1])
                for r in conn.execute(
                    "SELECT backend_id,COUNT(*) FROM embedding_cache GROUP BY backend_id"
                )
            ]
            if "embedding_cache" in names
            else []
        )
    import wq_forum_rag

    package = Path(wq_forum_rag.__file__).parent
    return {
        "time_utc": datetime.now(UTC).isoformat(),
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "source_directory": str(package),
        "database": str(db.resolve()),
        "database_size": db.stat().st_size,
        "database_mtime_ns": db.stat().st_mtime_ns,
        "counts": counts,
        "cached_backends": backends,
        "source_hashes": {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(package.glob("*.py"))
        },
    }


async def _run_cases(cases, call, repeats, top_k):
    rows = []
    for case in cases:
        row = {
            **case,
            "expected_ids": case.get("expected_ids", []),
            "expect_empty": case.get("expect_empty", False),
            "result_ids": [],
            "repeat_result_ids": [],
            "seconds": [],
            "error": None,
        }
        for _ in range(repeats):
            start = time.perf_counter()
            try:
                result = await call(case, top_k)
                if result.get("status") == "locked":
                    raise RuntimeError("database locked")
                ids = [str(r.get("topic_id", r.get("slug"))) for r in result["results"]]
                row["repeat_result_ids"].append(ids)
                if len(row["repeat_result_ids"]) == 1:
                    row["result_ids"] = ids
            except Exception as exc:
                row["error"] = f"{type(exc).__name__}: {exc}"
                break
            finally:
                row["seconds"].append(time.perf_counter() - start)
        rows.append(row)
        print(
            f"{case['id']}: {row['seconds'][0]:.3f}s, {len(row['result_ids'])} results"
            + (f", {row['error']}" if row["error"] else ""),
            file=sys.stderr,
            flush=True,
        )
    return rows


async def evaluate(db, cases, repeats, top_k, transport):
    if transport == "direct":
        from wq_forum_rag.indexer import ForumIndexService
        from wq_forum_rag.search_cache import CachedEmbeddingBackend
        from wq_forum_rag.search_records import search_doc_records

        service = ForumIndexService(db, auto_refresh=False)

        async def call(case, count):
            if case["kind"] == "forum":
                return {"results": service.search(case["query"], top_k=count)}
            if case["kind"] == "exact":
                return {"results": service.find_by_exact(case["query"], top_k=count)}
            with sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True) as conn:
                conn.row_factory = sqlite3.Row
                return {
                    "results": search_doc_records(
                        conn,
                        case["query"],
                        top_k=count,
                        embedding_backend=CachedEmbeddingBackend(db, writable=False),
                    )
                }

        return await _run_cases(cases, call, repeats, top_k)

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-c", "from wq_forum_rag.mcp_server import main; main()"],
        env={
            **os.environ,
            "FORUM_RAG_DB": str(db.resolve()),
            "WQ_FORUM_RAG_AUTO_REFRESH": "0",
        },
    )
    async with stdio_client(parameters) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()

            async def call(case, count):
                name, key = {
                    "forum": ("search_forum", "query"),
                    "exact": ("find_by_exact", "value"),
                    "docs": ("search_docs", "query"),
                }[case["kind"]]
                result = await session.call_tool(
                    name, {key: case["query"], "top_k": count}
                )
                if result.is_error:
                    raise RuntimeError(str(result.content))
                return result.structured_content or json.loads(result.content[0].text)

            return await _run_cases(cases, call, repeats, top_k)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument(
        "--cases",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "tests/fixtures/search_eval.json",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--transport", choices=["direct", "mcp"], default="mcp")
    args = parser.parse_args()
    if not args.db.is_file() or args.repeats < 1 or args.top_k < 1:
        parser.error("Require an existing database, repeats>=1 and top-k>=1")
    if args.output.resolve() in {args.db.resolve(), args.cases.resolve()}:
        parser.error("output must not overwrite database or cases")
    fixture = json.loads(args.cases.read_text(encoding="utf-8"))
    cases = fixture["cases"]
    validate_cases(cases)
    metadata = _metadata(args.db)
    rows = asyncio.run(
        evaluate(args.db, cases, args.repeats, args.top_k, args.transport)
    )
    report = {
        "metadata": metadata,
        "label_notes": fixture.get("notes"),
        "transport": args.transport,
        "top_k": args.top_k,
        "repeats": args.repeats,
        "limitations": [
            "Source-derived known targets are not exhaustive human relevance labels.",
            "First calls are not OS-cold measurements; small-sample p95 is descriptive.",
        ],
        "summary": summarize(rows),
        "by_kind": {
            kind: summarize([r for r in rows if r["kind"] == kind])
            for kind in sorted({r["kind"] for r in rows})
        },
        "cases": rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    return 1 if report["summary"]["error_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
