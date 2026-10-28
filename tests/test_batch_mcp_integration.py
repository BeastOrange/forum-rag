from __future__ import annotations

import asyncio
import sqlite3
import sys
import threading
from types import SimpleNamespace

from wq_forum_rag import mcp_server
from wq_forum_rag.cli import app
from typer.testing import CliRunner


def test_batch_mcp_dispatches_once_off_event_loop(monkeypatch, tmp_path):
    calls = []
    caller = threading.get_ident()
    def search(db_path, query, **kwargs):
        calls.append((threading.get_ident(), str(db_path), query, kwargs))
        return {'query': query, 'executed_queries': [query, *kwargs['query_variants']],
                'results': [{'topic_id': 'zh'}, {'topic_id': 'en'}]}
    monkeypatch.setitem(sys.modules, 'wq_forum_rag.batch_search', SimpleNamespace(search_forum_batch=search))
    result = asyncio.run(mcp_server.search_forum_batch('缺失值', query_variants=['missing values'],
                        db=str(tmp_path / 'source.sqlite3'), top_k=4, evidence_chars=300,
                        max_evidence_chars=1200))
    assert len(calls) == 1
    assert calls[0][0] != caller
    assert calls[0][3] == dict(query_variants=['missing values'], top_k=4,
                              evidence_chars=300, max_evidence_chars=1200)
    assert len(result['results']) == 2


def test_evidence_mcp_dispatches_hash_and_budget(monkeypatch, tmp_path):
    seen = {}
    def evidence(db_path, chunk_id, **kwargs):
        seen.update(chunk_id=chunk_id, **kwargs)
        return {'status': 'ready', 'chunks': []}
    monkeypatch.setitem(sys.modules, 'wq_forum_rag.batch_search', SimpleNamespace(get_forum_evidence=evidence))
    result = asyncio.run(mcp_server.get_forum_evidence('chunk', db=str(tmp_path / 'source.sqlite3'),
                         before=0, after=2, max_chars=2000, expected_content_hash='snapshot-hash'))
    assert result['status'] == 'ready'
    assert seen == dict(chunk_id='chunk', before=0, after=2, max_chars=2000,
                        expected_content_hash='snapshot-hash')


def test_batch_mcp_reports_database_lock(monkeypatch, tmp_path):
    def locked(*args, **kwargs):
        raise sqlite3.OperationalError('database is locked')
    monkeypatch.setitem(sys.modules, 'wq_forum_rag.batch_search', SimpleNamespace(search_forum_batch=locked))
    result = asyncio.run(mcp_server.search_forum_batch('query', db=str(tmp_path / 'source.sqlite3')))
    assert result['status'] == 'locked'
    assert result['results'] == []


def test_batch_cli_forwards_variants_without_model(monkeypatch, tmp_path):
    db = tmp_path / 'source.sqlite3'
    db.touch()
    seen = {}
    def search(db_path, query, **kwargs):
        seen.update(query=query, **kwargs)
        return {'results': []}
    monkeypatch.setitem(sys.modules, 'wq_forum_rag.batch_search', SimpleNamespace(search_forum_batch=search))
    result = CliRunner().invoke(app, ['search-batch', '缺失值', '--variant', 'missing values', '--db', str(db)])
    assert result.exit_code == 0, result.output
    assert seen['query_variants'] == ['missing values']


def test_batch_mcp_timeout_is_explicit_not_no_match(monkeypatch, tmp_path):
    def timed_out(*args, **kwargs):
        raise TimeoutError('SQLite request exceeded time budget')
    monkeypatch.setitem(sys.modules, 'wq_forum_rag.batch_search', SimpleNamespace(
        search_forum_batch=timed_out, get_forum_evidence=timed_out))
    result = asyncio.run(mcp_server.search_forum_batch('query', db=str(tmp_path / 'source.sqlite3')))
    assert result['status'] == 'timeout'
    assert result['results'] == []
    result = asyncio.run(mcp_server.get_forum_evidence('chunk', db=str(tmp_path / 'source.sqlite3')))
    assert result['status'] == 'timeout'
    assert result['evidence'] == []
