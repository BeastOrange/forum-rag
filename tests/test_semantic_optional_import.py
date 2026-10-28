from __future__ import annotations

import os
import subprocess
import sys


def test_lexical_imports_without_numpy():
    code = '''
import builtins
original = builtins.__import__
def no_numpy(name, *args, **kwargs):
    if name == "numpy" or name.startswith("numpy."):
        raise ModuleNotFoundError("numpy deliberately unavailable")
    return original(name, *args, **kwargs)
builtins.__import__ = no_numpy
from wq_forum_rag.search_records import search_forum_records
from wq_forum_rag import cli, mcp_server
assert search_forum_records and cli and mcp_server
'''
    subprocess.run([sys.executable, '-c', code], env=os.environ.copy(), check=True)
