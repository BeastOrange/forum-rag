"""Benchmark search on a caller-provided disposable database copy, via direct calls or MCP stdio."""
from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import json
import math
import os
import resource
import sys
import time
from pathlib import Path


def summarize(rows, workers, elapsed):
    values = sorted(row['seconds'] for row in rows)
    percentile = lambda p: values[math.ceil(len(values) * p) - 1] if values else None
    groups = {}
    for row in rows:
        if not row['error']:
            groups.setdefault(row['query'], []).append(tuple(row['ids']))
    compared = {q: v for q, v in groups.items() if len(v) > 1}
    return {'workers': workers, 'requests': len(rows), 'elapsed_seconds': elapsed,
            'throughput_requests_per_second': len(rows) / elapsed if elapsed else None,
            'errors': sum(bool(r['error']) for r in rows), 'timeouts': sum(r['timeout'] for r in rows),
            'latency_seconds': {'p50': percentile(.5), 'p95': percentile(.95), 'p99': percentile(.99)},
            'consistency': {'compared_queries': len(compared),
                            'inconsistent_queries': sorted(q for q, v in compared.items() if len(set(v)) > 1)}}


def normalize(result):
    if not isinstance(result, dict) or result.get('status') == 'locked' or not isinstance(result.get('results'), list):
        raise RuntimeError(f'invalid or locked response: {result}')
    ids = []
    for item in result['results']:
        if not isinstance(item, dict) or item.get('topic_id') is None:
            raise RuntimeError('response missing topic_id')
        ids.append(str(item['topic_id']))
    return ids


def direct_call(db, query, args):
    from wq_forum_rag.mcp_server import search_forum
    return search_forum(query, db=str(db), top_k=args.top_k)


async def benchmark(call, queries, workers, requests, timeout):
    semaphore = asyncio.Semaphore(workers)
    async def one(query):
        async with semaphore:
            start = time.perf_counter()
            row = {'query': query, 'ids': [], 'error': None, 'timeout': False}
            try:
                operation = call(query)
                result = await asyncio.wait_for(operation, timeout) if timeout else await operation
                row['ids'] = normalize(result)
            except asyncio.TimeoutError:
                row.update(error='timeout', timeout=True)
            except Exception as exc:
                row['error'] = f'{type(exc).__name__}: {exc}'
            row['seconds'] = time.perf_counter() - start
            return row
    start = time.perf_counter()
    rows = await asyncio.gather(*(one(queries[i % len(queries)]) for i in range(requests)))
    elapsed = time.perf_counter() - start
    return {'summary': summarize(rows, workers, elapsed), 'rows': rows}


async def run(args, db, queries):
    startup = time.perf_counter()
    if args.transport == 'direct':
        # Bound simultaneous calls and reuse process-local lexical caches.
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=max(args.concurrency))
        loop = asyncio.get_running_loop()
        async def call(query):
            return await loop.run_in_executor(pool, direct_call, db, query, args)
        try:
            return await runs(call, args, queries, time.perf_counter() - startup)
        finally:
            pool.shutdown(wait=True)
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    params = StdioServerParameters(command=sys.executable,
        args=['-c', 'from wq_forum_rag.mcp_server import main; main()'],
        env={**os.environ, 'FORUM_RAG_DB': str(db), 'WQ_FORUM_RAG_AUTO_REFRESH': '0'})
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            async def call(query):
                arguments = {'db': str(db), 'query': query, 'top_k': args.top_k}
                response = await session.call_tool('search_forum', arguments)
                if response.isError:
                    raise RuntimeError(str(response.content))
                return response.structuredContent or json.loads(response.content[0].text)
            return await runs(call, args, queries, time.perf_counter() - startup)


async def runs(call, args, queries, startup_seconds):
    cold = await benchmark(call, queries, 1, 1, args.timeout if args.transport == 'mcp' else None)
    warm = []
    for workers in args.concurrency:
        warm.append(await benchmark(call, queries, workers, args.requests,
                                    args.timeout if args.transport == 'mcp' else None))
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # macOS reports bytes; Linux reports KiB.
    rss_bytes = rss if sys.platform == 'darwin' else rss * 1024
    return {'startup_seconds': startup_seconds, 'first_call': cold, 'warm_runs': warm,
            'parent_peak_rss_bytes': rss_bytes,
            'limitations': ['First call is process-cold, not necessarily OS-cache-cold.',
                            'RSS is parent process peak only; it excludes MCP child.',
                            'Latency includes failures; throughput counts all completed attempts.',
                            'Direct calls have no enforced timeout; MCP timeouts cancel client waits, not necessarily server work.']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, required=True, help='Disposable SQLite database copy')
    parser.add_argument('--db-is-copy', action='store_true', required=True, help='Confirm --db is a disposable copy, not the original database')
    parser.add_argument('--queries', type=Path, required=True)
    parser.add_argument('--mode', choices=('lexical',), default='lexical')
    parser.add_argument('--transport', choices=('direct', 'mcp'), required=True)
    parser.add_argument('--concurrency', default='1,2,4,8')
    parser.add_argument('--requests', type=int, default=32)
    parser.add_argument('--top-k', type=int, default=5)
    parser.add_argument('--timeout', type=float, default=60)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        args.concurrency = [int(x) for x in args.concurrency.split(',')]
    except ValueError:
        parser.error('concurrency must be comma-separated positive integers')
    if not args.db.is_file() or args.requests < 1 or args.top_k < 1 or args.timeout <= 0 or not args.concurrency or any(x < 1 for x in args.concurrency):
        parser.error('invalid database, concurrency, requests, top-k or timeout')
    if args.output.resolve() in (args.db.resolve(), args.queries.resolve()):
        parser.error('output must differ from inputs')
    fixture = json.loads(args.queries.read_text())
    cases = fixture.get('cases', fixture) if isinstance(fixture, dict) else fixture
    queries = [c['query'] if isinstance(c, dict) else c for c in cases if not isinstance(c, dict) or c.get('kind', 'forum') == 'forum']
    queries = [q for q in queries if not isinstance(q, str) or q.strip()]
    if not queries or any(not isinstance(q, str) or not q.strip() for q in queries):
        parser.error('queries must contain nonempty forum queries')
    os.environ['WQ_FORUM_RAG_AUTO_REFRESH'] = '0'
    result = asyncio.run(run(args, args.db.resolve(), queries))
    result.update(mode=args.mode, transport=args.transport, requested_concurrency=args.concurrency,
                  requested_per_run=args.requests, database_copy=str(args.db.resolve()),
                  query_file=str(args.queries.resolve()), top_k=args.top_k)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    return int(bool(result['first_call']['summary']['errors'] or any(r['summary']['errors'] for r in result['warm_runs'])))


if __name__ == '__main__':
    raise SystemExit(main())
