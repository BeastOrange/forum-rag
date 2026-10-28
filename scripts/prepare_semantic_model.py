#!/usr/bin/env python3
"""Explicitly download the optional semantic model; search remains offline."""

import argparse
import importlib.util
import sys
from pathlib import Path

module_path = Path(__file__).resolve().parents[1] / "src/wq_forum_rag/model_download.py"
spec = importlib.util.spec_from_file_location("_wq_model_download", module_path)
if spec is None or spec.loader is None:
    raise ImportError(f"Cannot load model downloader: {module_path}")
model_download = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = model_download
spec.loader.exec_module(model_download)
ModelDownloadError = model_download.ModelDownloadError
prepare_model = model_download.prepare_model


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", type=Path, default=Path(".cache/models/minilm"))
    parser.add_argument(
        "--source", default="auto", help="auto, official, mirror, or an HTTPS origin"
    )
    parser.add_argument(
        "--preferred", choices=("official", "mirror"), default="official"
    )
    parser.add_argument(
        "--attempts", type=int, default=3, help="maximum attempts per file and source"
    )
    parser.add_argument("--connect-timeout", type=float, default=10)
    parser.add_argument("--read-timeout", type=float, default=30)
    args = parser.parse_args()
    try:
        result = prepare_model(
            args.model_dir,
            source=args.source,
            preferred=args.preferred,
            attempts_per_source=args.attempts,
            connect_timeout=args.connect_timeout,
            read_timeout=args.read_timeout,
        )
    except (ModelDownloadError, ValueError) as exc:
        print(f"Model preparation failed: {exc}", file=sys.stderr)
        return 1
    sources = ", ".join(sorted(result.sources)) if result.sources else "local cache"
    print(f"Model ready: {result.path}; source: {sources}; attempts: {result.attempts}")
    print(
        "This was an explicit preparation step; search does not download this model at runtime."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
