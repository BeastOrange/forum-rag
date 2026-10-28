#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description='Build a local forum semantic index')
    parser.add_argument('--db', required=True)
    parser.add_argument('--model-dir', required=True)
    parser.add_argument('--index-dir', required=True)
    parser.add_argument('--batch-size', type=int, default=32)
    parser.add_argument('--threads', type=int, default=1)
    owner = parser.add_mutually_exclusive_group()
    owner.add_argument('--lease-file', type=Path, help='Lease refreshed by the task owner, never by the worker')
    owner.add_argument('--foreground', action='store_true', help='Explicitly run in caller-managed foreground without a lease')
    parser.add_argument('--timeout', type=float, default=600, help='Supervised task deadline in seconds')
    parser.add_argument('--lease-ttl', type=float, default=30, help='Stop when owner has not refreshed lease for this many seconds')
    parser.add_argument('--_worker', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.lease_file:
        from run_bounded_task import run
        command = [sys.executable, '-B', str(Path(__file__).resolve()), '--_worker',
                   '--db', args.db, '--model-dir', args.model_dir, '--index-dir', args.index_dir,
                   '--batch-size', str(args.batch_size), '--threads', str(args.threads)]
        try:
            return run(command, lease_file=args.lease_file, timeout=args.timeout, lease_ttl=args.lease_ttl)
        except (OSError, ValueError) as exc:
            parser.error(str(exc))
    if not args._worker and not args.foreground and not sys.stdin.isatty():
        parser.error('unattended builds require --lease-file; use --foreground only when explicitly managing this process yourself')
    # Importing the model and allocating resources happens only after ownership checks.
    from wq_forum_rag.semantic_index import build_semantic_index
    print(json.dumps(build_semantic_index(args.db, model_dir=args.model_dir, index_dir=args.index_dir,
                                          batch_size=args.batch_size, threads=args.threads)))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
