"""Exercise the installed SDK and a real, model-free stdio server."""

from __future__ import annotations

import asyncio
from importlib.metadata import version
import json
import os
from pathlib import Path
import sys

import pytest

from wq_forum_rag.mcp_server import build_mcp_server
from wq_forum_rag.models import Topic
from wq_forum_rag.search_index import rebuild_search_index
from wq_forum_rag.storage import ForumStore


def test_sdk2_tool_registration_keeps_public_arguments():
    assert version("mcp").split(".")[0] == "2"
    tools = {tool.name: tool for tool in asyncio.run(build_mcp_server().list_tools())}
    assert len(tools) == 22
    assert "search_semantic_forum" not in tools
    schema = tools["search_forum_batch"].input_schema
    assert schema["required"] == ["query"]
    assert {"query", "query_variants", "db", "top_k", "evidence_chars",
            "max_evidence_chars", "compact"} == set(schema["properties"])
    assert tools["get_forum_evidence"].input_schema["required"] == ["chunk_id"]


@pytest.mark.parametrize("connect", ["initialize", "discover"])
def test_real_stdio_bilingual_evidence_and_errors(tmp_path, connect):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    db = tmp_path / "source.sqlite3"
    with ForumStore(db) as store:
        store.upsert_topics([
            Topic("1", "c", "Forum", "Neutralization", "Industry neutralization notes.", "url/1", ""),
            Topic("2", "c", "Forum", "中性化", "行业中性化笔记。", "url/2", ""),
        ])
    rebuild_search_index(db)
    original_state = db.stat().st_mtime_ns, db.stat().st_size
    server_code = '''
import sys
def block(event, args):
    if event == 'import' and args[0].split('.')[0] in {'numpy', 'fastembed', 'onnxruntime', 'torch'}:
        raise ImportError('model dependency must not load in the MCP server')
sys.addaudithook(block)
from wq_forum_rag.mcp_server import main
main()
'''
    params = StdioServerParameters(
        command=sys.executable, args=["-B", "-c", server_code],
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src"),
             "FORUM_RAG_DB": str(db), "WQ_FORUM_RAG_AUTO_REFRESH": "0"},
    )

    async def exercise():
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                # Exercise both the legacy handshake and the SDK 2 discovery path.
                await getattr(session, connect)()
                assert len((await session.list_tools()).tools) == 22
                result = await session.call_tool("search_forum_batch", {
                    "query": "neutralization", "query_variants": ["中性化"], "compact": True,
                })
                assert not result.is_error
                payload = result.structured_content
                assert json.loads(result.content[0].text) == payload
                assert {row["topic_id"] for row in payload["results"]} == {"1", "2"}
                assert all("snippet" not in row for row in payload["results"])
                anchor = payload["results"][0]["evidence"][0]
                response = await session.call_tool("get_forum_evidence", {
                    "chunk_id": anchor["chunk_id"],
                    "expected_content_hash": anchor["chunk_content_hash"],
                })
                assert not response.is_error
                assert response.structured_content["status"] == "ok"
                assert response.structured_content["evidence"]
                invalid = await session.call_tool("search_forum_batch", {
                    "query": "neutralization", "top_k": 99,
                })
                assert invalid.is_error
                # An invalid call must not break the transport or normal empty results.
                empty = await session.call_tool("search_forum_batch", {"query": "qzxvnotpresent"})
                assert not empty.is_error
                assert empty.structured_content["results"] == []

    asyncio.run(asyncio.wait_for(exercise(), timeout=15))
    assert (db.stat().st_mtime_ns, db.stat().st_size) == original_state
