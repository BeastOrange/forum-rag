#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json

from wq_forum_rag.semantic_index import build_semantic_index


def main():
    parser = argparse.ArgumentParser(description='Build a local forum semantic index')
    parser.add_argument('--db', required=True)
    parser.add_argument('--model-dir', required=True)
    parser.add_argument('--index-dir', required=True)
    parser.add_argument('--batch-size', type=int, default=32)
    parser.add_argument('--threads', type=int, default=1)
    args = parser.parse_args()
    print(json.dumps(build_semantic_index(args.db, model_dir=args.model_dir, index_dir=args.index_dir,
                                          batch_size=args.batch_size, threads=args.threads)))


if __name__ == '__main__':
    main()
