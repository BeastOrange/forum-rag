import asyncio
import sqlite3
import sys
import threading
from types import SimpleNamespace

import pytest

from wq_forum_rag import batch_search, mcp_server


def test_shared_capacity_and_cancelled_thread_holds_slot(monkeypatch, tmp_path):
    entered = [threading.Event(), threading.Event()]
    release = threading.Event()
    def search(db, query, **kwargs):
        if query not in ('0', '1'):
            return {'status': 'unexpected_execution', 'results': []}
        entered[int(query)].set()
        assert release.wait(3)
        return {'results': []}
    monkeypatch.setitem(sys.modules, 'wq_forum_rag.batch_search', SimpleNamespace(search_forum_batch=search, get_forum_evidence=search))
    async def scenario():
        a = asyncio.create_task(mcp_server.search_forum_batch('0', db=str(tmp_path/'x')))
        b = asyncio.create_task(mcp_server.get_forum_evidence('1', db=str(tmp_path/'x')))
        assert await asyncio.to_thread(entered[0].wait, 2)
        assert await asyncio.to_thread(entered[1].wait, 2)
        assert (await mcp_server.search_forum_batch('third', db=str(tmp_path/'x')))['status'] == 'busy'
        a.cancel()
        with pytest.raises(asyncio.CancelledError): await a
        assert (await mcp_server.get_forum_evidence('third', db=str(tmp_path/'x')))['status'] == 'busy'
        release.set()
        await b
        # Await the worker's actual exit, not merely the cancelled awaiter's exit.
        for _ in range(30):
            result = await mcp_server.search_forum_batch('0', db=str(tmp_path/'x'))
            if result.get('status') != 'busy': break
            await asyncio.sleep(.01)
        assert result.get('status') != 'busy'
    try: asyncio.run(scenario())
    finally: release.set()


def test_cancelled_sql_interrupts_and_closes_connection(monkeypatch, tmp_path):
    path=tmp_path/'db.sqlite3'
    with sqlite3.connect(path) as conn:
        conn.executescript('CREATE TABLE topics(topic_id TEXT, title TEXT, url TEXT, content_hash TEXT); CREATE TABLE chunks(chunk_id TEXT, topic_id TEXT, chunk_index INTEGER, chunk_level INTEGER, start_offset INTEGER, end_offset INTEGER, content TEXT, content_hash TEXT);')
    started=threading.Event(); closed=threading.Event()
    real_connect=sqlite3.connect
    class TrackingConnection(sqlite3.Connection):
        def close(self):
            super().close()
            closed.set()
    def connect(*args, **kwargs):
        kwargs['factory']=TrackingConnection
        return real_connect(*args, **kwargs)
    monkeypatch.setattr(batch_search.sqlite3, 'connect', connect)
    def slow_candidates(conn, query, **kwargs):
        started.set()
        conn.execute('WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL SELECT x+1 FROM n WHERE x<100000000) SELECT sum(x) FROM n').fetchone()
        return []
    monkeypatch.setattr(batch_search, 'candidate_ids', slow_candidates)
    async def scenario():
        task=asyncio.create_task(mcp_server.search_forum_batch('needle', db=str(path)))
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        assert await asyncio.to_thread(closed.wait, .6)
        result=await mcp_server.get_forum_evidence('missing', db=str(path))
        assert result['status'] == 'not_found'
    asyncio.run(scenario())


def test_cancel_while_worker_queued_never_executes_and_releases_slot(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from wq_forum_rag import read_worker
    executor = ThreadPoolExecutor(max_workers=1)
    monkeypatch.setattr(read_worker, '_executor', executor)
    entered, release, called = threading.Event(), threading.Event(), threading.Event()
    def first():
        entered.set()
        assert release.wait(3)
    def second():
        called.set()
    async def scenario():
        first_task = asyncio.create_task(read_worker.run_read(first))
        assert await asyncio.to_thread(entered.wait, 2)
        second_task = asyncio.create_task(read_worker.run_read(second))
        await asyncio.sleep(0)  # Submit the second worker while the executor is occupied.
        second_task.cancel()
        with pytest.raises(asyncio.CancelledError): await second_task
        with pytest.raises(read_worker.ReadBusy): await read_worker.run_read(second)
        release.set()
        await first_task
        # A FIFO marker proves the cancelled queued worker has exited.
        marker = executor.submit(lambda: None)
        await asyncio.wrap_future(marker)
        assert not called.is_set()
        await read_worker.run_read(second)
        assert called.is_set()
    try:
        asyncio.run(scenario())
    finally:
        release.set()
        executor.shutdown(wait=True)
