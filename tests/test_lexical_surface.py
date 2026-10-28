"""The public retrieval surface must work without optional model libraries."""

import asyncio
import os
import subprocess
import sys

from wq_forum_rag.mcp_server import build_mcp_server


def test_tools_keep_lexical_batch_and_evidence_without_model_entry():
    tools = asyncio.run(build_mcp_server().list_tools())
    names = {tool.name for tool in tools}
    assert {"search_forum", "search_forum_batch", "get_forum_evidence"} <= names
    assert "search_semantic_forum" not in names


def test_cli_help_without_model_libraries():
    code = '''
import sys
def block(event, args):
    if event == 'import' and args[0].split('.')[0] in {'numpy', 'fastembed', 'onnxruntime', 'torch'}:
        raise ImportError('optional model dependency must not load')
sys.addaudithook(block)
from typer.testing import CliRunner
from wq_forum_rag.cli import app
result = CliRunner().invoke(app, ['--help'])
assert result.exit_code == 0, result.exception
assert 'search-batch' in result.stdout
assert 'forum-evidence' in result.stdout
assert 'search-semantic' not in result.stdout
'''
    result = subprocess.run(
        [sys.executable, "-B", "-c", code],
        capture_output=True, text=True, timeout=10, env=os.environ.copy(),
    )
    assert result.returncode == 0, result.stderr
