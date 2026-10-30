#!/usr/bin/env python3
"""Run an explicit task with a time budget, heartbeat lease, and process-group cleanup."""

from __future__ import annotations

import argparse
import math
import os
import select
import signal
import subprocess
import sys
import time
from pathlib import Path


def _group_exists(pgid: int, deadline: float) -> bool:
    # macOS can report EPERM briefly while the last group member exits.
    # A persistent permission error remains an error, never proof of absence.
    retry_deadline = min(deadline, time.monotonic() + 0.05)
    while True:
        try:
            os.killpg(pgid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            remaining = retry_deadline - time.monotonic()
            if remaining <= 0:
                raise
            time.sleep(min(0.005, remaining))


def _stop_group(pgid: int, grace: float, process=None) -> None:
    if pgid <= 0 or pgid == os.getpgrp():
        raise ValueError("refusing to stop our own process group")
    if process is not None:
        process.poll()  # Reap an already-finished direct child before signalling its group.
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + grace
    cleanup_deadline = deadline + 0.05
    while time.monotonic() < deadline:
        if process is not None:
            process.poll()
        if not _group_exists(pgid, cleanup_deadline):
            break
        time.sleep(max(0, min(0.02, deadline - time.monotonic())))
    if process is not None:
        process.poll()
    if _group_exists(pgid, cleanup_deadline):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process is not None:
        process.wait()


def _report_watchdog(message: bytes) -> None:
    try:
        os.write(sys.stdout.fileno(), message)
    except OSError:
        pass  # The supervisor may already be gone.


def _watchdog(
    fd: int, pgid: int, lease: Path, timeout: float, ttl: float, grace: float
) -> int:
    """A separate session observes pipe EOF if the supervisor is killed."""
    started = last_heartbeat = time.monotonic()
    try:
        signature = lease.stat().st_mtime_ns
        _report_watchdog(
            b"R"
        )  # Ready: no task is released before this acknowledgement.
        while True:
            ready, _, _ = select.select([fd], [], [], min(0.05, ttl / 4))
            if ready:
                data = os.read(fd, 1)
                if data == b"D":
                    return 0  # Parent already completed process-group cleanup.
                if not data:
                    break
            now = time.monotonic()
            current = lease.stat().st_mtime_ns
            if current != signature:
                signature, last_heartbeat = current, now
            if now - started >= timeout:
                _report_watchdog(b"T")
                break
            if now - last_heartbeat >= ttl:
                _report_watchdog(b"L")
                break
    except OSError:
        _report_watchdog(
            b"L"
        )  # A lost monitoring channel or inaccessible lease fails closed.
    finally:
        os.close(fd)
    _stop_group(pgid, grace)
    return 0


def _watchdog_reason(fd: int) -> int | None:
    ready, _, _ = select.select([fd], [], [], 0)
    if not ready:
        return None
    event = os.read(fd, 1)
    return 124 if event == b"T" else 125


def run(
    command: list[str],
    *,
    lease_file: Path,
    timeout: float = 600,
    lease_ttl: float = 30,
    grace: float = 1,
) -> int:
    """Return child status, 124 on deadline, 125 on lease loss, or 128+signal.

    The caller must refresh its own lease while it still owns the task. Ending a
    conversation without refreshing the lease cannot leave computation unbounded.
    A normally completed direct child also triggers cleanup of any descendants.
    """
    if os.name != "posix":
        raise ValueError("this process-group supervisor currently supports POSIX only")
    if (
        not command
        or not all(math.isfinite(x) and x > 0 for x in (timeout, lease_ttl, grace))
        or timeout > 86400
        or lease_ttl > 120
        or grace > 10
    ):
        raise ValueError(
            "require command, timeout <=86400s, lease TTL <=120s, grace <=10s; all positive and finite"
        )
    lease_file = Path(lease_file)
    signature = lease_file.stat().st_mtime_ns
    if time.time() - lease_file.stat().st_mtime > lease_ttl:
        raise ValueError("lease is already expired; task was not started")
    received = []
    previous = {}
    process = watchdog = None
    task_released = False
    read_fd = write_fd = gate_read = gate_write = None
    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(
                signum, lambda sig, frame: received.append(sig)
            )
        if received:
            return 128 + received[0]
        read_fd, write_fd = os.pipe()
        gate_read, gate_write = os.pipe()
        # Do not execute user work until its independent watchdog is running.
        process = subprocess.Popen(
            [
                sys.executable,
                "-B",
                str(Path(__file__).resolve()),
                "--_task",
                str(gate_read),
                *command,
            ],
            pass_fds=(gate_read,),
            start_new_session=True,
            env={**os.environ, "FORUM_RAG_JOB_SUPERVISED": "1"},
        )
        os.close(gate_read)
        gate_read = None
        watchdog = subprocess.Popen(
            [
                sys.executable,
                "-B",
                str(Path(__file__).resolve()),
                "--_watchdog",
                str(read_fd),
                str(process.pid),
                str(lease_file.resolve()),
                str(timeout),
                str(lease_ttl),
                str(grace),
            ],
            pass_fds=(read_fd,),
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        os.close(read_fd)
        read_fd = None
        status_fd = watchdog.stdout.fileno()
        ready, _, _ = select.select([status_fd], [], [], min(2, timeout, lease_ttl))
        if not ready or os.read(status_fd, 1) != b"R":
            raise OSError(
                "independent watchdog did not become ready; task was not started"
            )
        task_released = True
        os.write(gate_write, b"G")
        os.close(gate_write)
        gate_write = None
        started = last_heartbeat = time.monotonic()
        while True:
            reason = _watchdog_reason(status_fd)
            if reason is not None:
                return reason
            status = process.poll()
            if status is not None:
                # The watchdog writes the cause BEFORE killing the task. If it
                # acted between our first pipe check and poll, the cause is now
                # readable; consume it before classifying a signal exit.
                reason = _watchdog_reason(status_fd)
                if reason is not None:
                    return reason
                return status if status >= 0 else 128 - status
            if received:
                return 128 + received[0]
            now = time.monotonic()
            if now - started >= timeout:
                print(
                    "Task deadline reached; stopping its process group.",
                    file=sys.stderr,
                )
                return 124
            try:
                current = lease_file.stat().st_mtime_ns
            except FileNotFoundError:
                print(
                    "Task lease removed; stopping its process group.", file=sys.stderr
                )
                return 125
            if current != signature:
                signature, last_heartbeat = current, now
            if now - last_heartbeat >= lease_ttl:
                print(
                    "Task lease expired; stopping its process group.", file=sys.stderr
                )
                return 125
            time.sleep(min(0.05, lease_ttl / 4))
    finally:
        for gate_fd in (gate_read, gate_write):
            if gate_fd is not None:
                os.close(gate_fd)
        cleaned = False
        try:
            if process is not None:
                if task_released:
                    _stop_group(process.pid, grace, process)
                else:
                    # The gated wrapper cannot have spawned any task descendants.
                    if process.poll() is None:
                        process.kill()
                    process.wait()
            cleaned = True
        finally:
            if write_fd is not None:
                try:
                    if cleaned:
                        os.write(write_fd, b"D")
                    # Otherwise EOF asks the independent watchdog to retry cleanup.
                except OSError:
                    pass
                os.close(write_fd)
            if read_fd is not None:
                os.close(read_fd)
            if watchdog is not None:
                try:
                    watchdog.wait(timeout=grace + 2)
                except subprocess.TimeoutExpired:
                    watchdog.kill()
                    watchdog.wait()
                finally:
                    watchdog.stdout.close()
            for signum, handler in previous.items():
                signal.signal(signum, handler)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lease-file", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--lease-ttl", type=float, default=30)
    parser.add_argument("--grace", type=float, default=1)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    try:
        return run(
            command,
            lease_file=args.lease_file,
            timeout=args.timeout,
            lease_ttl=args.lease_ttl,
            grace=args.grace,
        )
    except (OSError, ValueError) as exc:
        print(f"Task failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    if sys.argv[1:2] == ["--_task"]:
        fd = int(sys.argv[2])
        ready = os.read(fd, 1)
        os.close(fd)
        if ready != b"G":
            raise SystemExit(125)
        os.execvpe(sys.argv[3], sys.argv[3:], os.environ)
    if sys.argv[1:2] == ["--_watchdog"]:
        raise SystemExit(
            _watchdog(
                int(sys.argv[2]),
                int(sys.argv[3]),
                Path(sys.argv[4]),
                float(sys.argv[5]),
                float(sys.argv[6]),
                float(sys.argv[7]),
            )
        )
    raise SystemExit(main())
