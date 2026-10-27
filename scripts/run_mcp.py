"""Start MCP from this checkout using an existing Python environment."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True, help="Existing SQLite index")
    args = parser.parse_args()
    if not args.db.is_file():
        parser.error("--db must point to an existing SQLite index")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    os.environ["FORUM_RAG_DB"] = str(args.db.resolve())
    # Serving an existing index does not implicitly authorize ingestion.
    # An explicit caller environment setting can retain auto-refresh if wanted.
    os.environ.setdefault("WQ_FORUM_RAG_AUTO_REFRESH", "0")
    from wq_forum_rag.mcp_server import main as serve

    serve()


if __name__ == "__main__":
    main()
