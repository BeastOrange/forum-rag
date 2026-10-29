"""Response and exception contracts shared by the two asynchronous read tools."""
import asyncio
import sqlite3

import pytest

from wq_forum_rag import mcp_server
from wq_forum_rag.read_worker import ReadBusy


@pytest.mark.parametrize('tool,identifier,result_key', [
    ('search_forum_batch', 'query', 'results'),
    ('get_forum_evidence', 'chunk_id', 'evidence'),
])
@pytest.mark.parametrize('outcome', ['busy', 'timeout', 'locked', 'unknown_sql', 'cancelled', 'success'])
def test_read_response_contract(monkeypatch, tmp_path, tool, identifier, result_key, outcome):
    path = tmp_path / 'forum.sqlite3'
    errors = {
        'busy': ReadBusy('Forum read capacity is busy'),
        'timeout': TimeoutError('SQLite request exceeded time budget'),
        'locked': sqlite3.OperationalError('database is locked'),
        'unknown_sql': sqlite3.OperationalError('no such table: missing'),
        'cancelled': asyncio.CancelledError('client disconnected'),
    }
    payload = {result_key: [{'source': 'verbatim'}], 'status': 'ready', 'db': 'payload-db'}
    async def execute(*args, **kwargs):
        if outcome in errors:
            raise errors[outcome]
        return payload
    monkeypatch.setattr(mcp_server, 'run_read', execute)
    call = getattr(mcp_server, tool)('requested', db=str(path))
    if outcome in ('unknown_sql', 'cancelled'):
        with pytest.raises(type(errors[outcome])) as caught:
            asyncio.run(call)
        assert caught.value is errors[outcome]
    else:
        response = asyncio.run(call)
        if outcome == 'success':
            assert response == payload  # Preserve the successful payload's db precedence.
        else:
            assert response == {'status': outcome, identifier: 'requested', 'db': str(path),
                                result_key: [], 'reason': str(errors[outcome])}


@pytest.mark.parametrize('compact', ['false', 0, 1, None])
def test_compact_validation_precedes_worker_dispatch(monkeypatch, compact):
    async def unexpected(*args, **kwargs):
        pytest.fail('invalid compact value reached the read worker')
    monkeypatch.setattr(mcp_server, 'run_read', unexpected)
    with pytest.raises(TypeError, match='compact must be a boolean'):
        asyncio.run(mcp_server.search_forum_batch('query', compact=compact))
