"""Offline lightweight RAG for WorldQuant forum exports."""

from ._version import __version__
from .cli import DEFAULT_DB_PATH, ForumIndexService, app
from .mcp_server import build_mcp_server

__all__ = ["DEFAULT_DB_PATH", "ForumIndexService", "app", "build_mcp_server"]
