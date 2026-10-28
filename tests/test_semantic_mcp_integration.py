from __future__ import annotations

import asyncio
import threading

from wq_forum_rag import mcp_server


def test_semantic_mcp_reports_missing_configuration(monkeypatch):
    monkeypatch.delenv('FORUM_RAG_SEMANTIC_INDEX', raising=False)
    monkeypatch.delenv('FORUM_RAG_MODEL_DIR', raising=False)
    payload = asyncio.run(mcp_server.search_semantic_forum('same meaning'))
    assert payload['status'] == 'not_configured'
    assert payload['results'] == []


def test_semantic_mcp_dispatches_model_work_off_event_loop(monkeypatch, tmp_path):
    import pytest
    pytest.importorskip('numpy')
    import wq_forum_rag.semantic_retrieval as retrieval

    caller = threading.get_ident()
    observed = {}
    def fake(db_path, query, **kwargs):
        observed.update(thread=threading.get_ident(), db=str(db_path), query=query, **kwargs)
        return {'results': [{'topic_id': 'evidence'}], 'index_status': 'ready'}
    monkeypatch.setattr(retrieval, 'search_semantic_forum', fake)
    payload = asyncio.run(mcp_server.search_semantic_forum(
        'same meaning', index_dir=str(tmp_path), model_dir=str(tmp_path),
        db=str(tmp_path / 'source.sqlite3'), mode='hybrid'))
    assert payload['results'][0]['topic_id'] == 'evidence'
    assert observed['thread'] != caller
    assert observed['mode'] == 'hybrid'
