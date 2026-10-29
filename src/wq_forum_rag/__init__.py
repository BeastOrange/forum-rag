# -*- coding: utf-8 -*-
# Author: konrad
# WQ_ID: OB53521
# Encoding: utf-8

"""Offline lightweight RAG for WorldQuant forum exports."""

from .cli import DEFAULT_DB_PATH, ForumIndexService, app
from .mcp_server import build_mcp_server

__all__ = ["DEFAULT_DB_PATH", "ForumIndexService", "app", "build_mcp_server"]
__version__ = "0.2.0"
