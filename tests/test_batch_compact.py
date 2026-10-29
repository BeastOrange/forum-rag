import asyncio
import json
import sqlite3

import pytest
from typer.testing import CliRunner

from wq_forum_rag.batch_search import search_forum_batch
from wq_forum_rag.cli import app
from wq_forum_rag.mcp_server import search_forum_batch as mcp_search


@pytest.fixture
def db(tmp_path):
    path=tmp_path/'forum.sqlite3'
    with sqlite3.connect(path) as conn:
        conn.executescript('''CREATE TABLE topics (topic_id TEXT PRIMARY KEY, title TEXT, community_title TEXT, url TEXT, content_hash TEXT);
        CREATE TABLE chunks (chunk_id TEXT PRIMARY KEY, topic_id TEXT, chunk_index INTEGER, chunk_level INTEGER, start_offset INTEGER, end_offset INTEGER, content TEXT, content_hash TEXT);''')
        conn.execute("INSERT INTO topics VALUES ('1','Missing values','Community','url','topic-hash')")
        conn.execute("INSERT INTO chunks VALUES ('one','1',0,1,0,27,'missing values and evidence','chunk-hash')")
    return path


def test_compact_only_removes_duplicate_snippet(db):
    regular=search_forum_batch(db, 'missing')
    compact=search_forum_batch(db, 'missing', compact=True)
    assert regular['results'] and regular['results'][0]['snippet'] == regular['results'][0]['evidence'][0]['excerpt']
    assert all('snippet' not in hit for hit in compact['results'])
    assert len(json.dumps(compact, ensure_ascii=False).encode()) < len(json.dumps(regular, ensure_ascii=False).encode())
    for hit in regular['results']:
        del hit['snippet']
    assert compact == regular


@pytest.mark.parametrize('value', ['true', 1, 0, None])
def test_compact_requires_bool(db, value):
    with pytest.raises(TypeError): search_forum_batch(db, 'missing', compact=value)


def test_mcp_and_cli_forward_compact(db):
    mcp_result=asyncio.run(mcp_search('missing', db=str(db), compact=True))
    assert mcp_result['results'] and 'snippet' not in mcp_result['results'][0]
    cli=CliRunner().invoke(app, ['search-batch','missing','--db',str(db),'--compact'])
    assert cli.exit_code == 0, cli.output
    result=json.loads(cli.stdout)
    assert result['results'] and 'snippet' not in result['results'][0]
    assert result['results'] == mcp_result['results']


@pytest.mark.parametrize('value', ['true', 1, 0, None])
def test_mcp_direct_call_rejects_non_boolean(db, value):
    with pytest.raises(TypeError):
        asyncio.run(mcp_search('missing', db=str(db), compact=value))


def test_cli_default_retains_snippet(db):
    result=CliRunner().invoke(app, ['search-batch', 'missing', '--db', str(db)])
    assert result.exit_code == 0, result.output
    hit=json.loads(result.stdout)['results'][0]
    assert hit['snippet'] == hit['evidence'][0]['excerpt']


def test_mcp_schema_exposes_optional_boolean():
    from wq_forum_rag.mcp_server import build_mcp_server
    tools = {tool.name: tool for tool in asyncio.run(build_mcp_server().list_tools())}
    schema = tools['search_forum_batch'].input_schema
    assert schema['properties']['compact']['type'] == 'boolean'
    assert schema['properties']['compact']['default'] is False
    assert 'compact' not in schema.get('required', [])
