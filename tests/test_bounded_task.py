"""Real subprocess lifecycle regressions; workers sleep instead of consuming CPU."""

import importlib.util
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX process groups")
SCRIPT = Path(__file__).parents[1] / "scripts/run_bounded_task.py"
spec = importlib.util.spec_from_file_location("bounded_task", SCRIPT)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def command(tmp_path, leave=False):
    pid_file, root_file, heartbeat = (
        tmp_path / x for x in ("worker.pid", "root.pid", "heartbeat")
    )
    worker = (
        "import os,signal,time,pathlib;signal.signal(signal.SIGTERM,signal.SIG_IGN);"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()));"
        f"p=pathlib.Path({str(heartbeat)!r});"
        "\nwhile True:\n with p.open('a') as f:f.write('.')\n time.sleep(.03)\n"
    )
    parent = (
        "import os,time,pathlib,subprocess,sys;"
        f"pathlib.Path({str(root_file)!r}).write_text(str(os.getpid()));"
        f"subprocess.Popen([sys.executable,'-B','-c',{worker!r}]);"
        f"p=pathlib.Path({str(pid_file)!r});"
        "\nwhile not p.exists():time.sleep(.01)\n"
        + ("" if leave else "time.sleep(30)\n")
    )
    return [sys.executable, "-B", "-c", parent], pid_file, root_file, heartbeat


def wait_for(path, timeout=4):
    end = time.monotonic() + timeout
    while not path.exists() and time.monotonic() < end:
        time.sleep(0.02)
    assert path.exists(), f"worker never started: {path}"


def cleanup(root_file):
    if root_file.exists():
        try:
            os.killpg(int(root_file.read_text()), signal.SIGKILL)
        except ProcessLookupError:
            pass


def assert_worker_stopped(heartbeat):
    # A surviving worker would keep extending this file every 30 milliseconds.
    last = heartbeat.stat().st_size if heartbeat.exists() else 0
    time.sleep(0.25)
    assert (heartbeat.stat().st_size if heartbeat.exists() else 0) == last


def test_normal_task_exit_cleans_surviving_descendant(tmp_path):
    cmd, pid, root, beat = command(tmp_path, leave=True)
    lease = tmp_path / "lease"
    lease.touch()
    try:
        assert runner.run(cmd, lease_file=lease, timeout=4, lease_ttl=3, grace=0.1) == 0
        wait_for(pid)
        assert_worker_stopped(beat)
    finally:
        cleanup(root)


@pytest.mark.parametrize("reason,expected", [("timeout", 124), ("lease", 125)])
def test_deadline_and_expired_owner_lease_stop_whole_group(tmp_path, reason, expected):
    cmd, pid, root, beat = command(tmp_path)
    lease = tmp_path / "lease"
    lease.touch()
    try:
        result = runner.run(
            cmd,
            lease_file=lease,
            timeout=0.4 if reason == "timeout" else 4,
            lease_ttl=3 if reason == "timeout" else 0.3,
            grace=0.1,
        )
        assert result == expected
        wait_for(pid)
        assert_worker_stopped(beat)
    finally:
        cleanup(root)


def test_killed_supervisor_cannot_leave_worker_running(tmp_path):
    cmd, pid, root, beat = command(tmp_path)
    lease = tmp_path / "lease"
    lease.touch()
    process = subprocess.Popen(
        [
            sys.executable,
            "-B",
            str(SCRIPT),
            "--lease-file",
            str(lease),
            "--timeout",
            "5",
            "--lease-ttl",
            "3",
            "--grace",
            ".1",
            "--",
            *cmd,
        ]
    )
    try:
        wait_for(pid)
        process.kill()  # No finally/atexit can run in the killed supervisor.
        process.wait(timeout=3)
        time.sleep(0.4)  # Separate watchdog sees control-pipe EOF and reaps group.
        assert_worker_stopped(beat)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        cleanup(root)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, 0, 1e300])
def test_invalid_budgets_do_not_launch(tmp_path, value):
    with pytest.raises(ValueError):
        runner.run(
            [sys.executable, "-c", 'raise Exception("must not start")'],
            lease_file=tmp_path / "missing",
            timeout=value,
        )


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_cancel_signal_reaps_descendants(tmp_path, signum):
    cmd, pid, root, beat = command(tmp_path)
    lease = tmp_path / "lease"
    lease.touch()
    process = subprocess.Popen(
        [
            sys.executable,
            "-B",
            str(SCRIPT),
            "--lease-file",
            str(lease),
            "--timeout",
            "5",
            "--lease-ttl",
            "3",
            "--grace",
            ".1",
            "--",
            *cmd,
        ]
    )
    try:
        wait_for(pid)
        process.send_signal(signum)
        assert process.wait(timeout=3) == 128 + signum
        assert_worker_stopped(beat)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        cleanup(root)


def test_fresh_heartbeats_keep_owned_task_running(tmp_path):
    import threading

    lease = tmp_path / "lease"
    lease.touch()
    stop = threading.Event()

    def refresh():
        while not stop.wait(0.04):
            lease.touch()

    updater = threading.Thread(target=refresh)
    updater.start()
    try:
        assert (
            runner.run(
                [sys.executable, "-B", "-c", "import time;time.sleep(.5)"],
                lease_file=lease,
                timeout=3,
                lease_ttl=0.2,
                grace=0.1,
            )
            == 0
        )
    finally:
        stop.set()
        updater.join(timeout=1)
    assert not updater.is_alive()


def test_watchdog_start_failure_never_executes_task(tmp_path, monkeypatch):
    lease = tmp_path / "lease"
    lease.touch()
    marker = tmp_path / "executed"
    real_popen = subprocess.Popen
    calls = 0

    def launch(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            time.sleep(0.15)
            raise OSError("watchdog unavailable")
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(runner.subprocess, "Popen", launch)
    with pytest.raises(OSError, match="watchdog unavailable"):
        runner.run(
            [
                sys.executable,
                "-B",
                "-c",
                f"from pathlib import Path;Path({str(marker)!r}).touch()",
            ],
            lease_file=lease,
            timeout=3,
            lease_ttl=2,
            grace=0.1,
        )
    assert not marker.exists()


def test_supervisor_killed_before_watchdog_cannot_start_task(tmp_path):
    lease = tmp_path / "lease"
    lease.touch()
    marker = tmp_path / "executed"
    child_pid = tmp_path / "gated.pid"
    code = f"""
import importlib.util, os, signal, subprocess, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location('runner', {str(SCRIPT)!r})
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)
real_popen = subprocess.Popen
calls = 0
def launch(*args, **kwargs):
    global calls
    calls += 1
    if calls == 2:
        os.kill(os.getpid(), signal.SIGKILL)
    p = real_popen(*args, **kwargs)
    Path({str(child_pid)!r}).write_text(str(p.pid))
    return p
r.subprocess.Popen = launch
r.run([sys.executable, '-B', '-c', {f"from pathlib import Path;Path({str(marker)!r}).touch()"!r}],
      lease_file=Path({str(lease)!r}), timeout=3, lease_ttl=2, grace=.1)
"""
    process = subprocess.Popen([sys.executable, "-B", "-c", code])
    try:
        assert process.wait(timeout=3) == -signal.SIGKILL
        wait_for(child_pid)
        time.sleep(0.2)
        assert not marker.exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        if child_pid.exists():
            try:
                os.killpg(int(child_pid.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.parametrize("reason,expected", [("timeout", 124), ("lease", 125)])
def test_watchdog_preserves_reason_when_supervisor_is_delayed(
    tmp_path, monkeypatch, reason, expected
):
    lease = tmp_path / "lease"
    lease.touch()
    original_sleep = time.sleep
    paused = False

    def delay(seconds):
        nonlocal paused
        if not paused:
            paused = True
            original_sleep(0.7)
        else:
            original_sleep(seconds)

    monkeypatch.setattr(runner.time, "sleep", delay)
    result = runner.run(
        [sys.executable, "-B", "-c", "import time;time.sleep(10)"],
        lease_file=lease,
        timeout=0.3 if reason == "timeout" else 3,
        lease_ttl=3 if reason == "timeout" else 0.3,
        grace=0.1,
    )
    assert result == expected


def test_watchdog_without_ready_ack_cannot_release_task(tmp_path, monkeypatch):
    lease = tmp_path / "lease"
    lease.touch()
    marker = tmp_path / "executed"
    real_popen = subprocess.Popen
    calls = 0

    def launch(args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            args = [sys.executable, "-B", "-c", "raise SystemExit(1)"]
        return real_popen(args, **kwargs)

    monkeypatch.setattr(runner.subprocess, "Popen", launch)
    with pytest.raises(OSError, match="did not become ready"):
        runner.run(
            [
                sys.executable,
                "-B",
                "-c",
                f"from pathlib import Path;Path({str(marker)!r}).touch()",
            ],
            lease_file=lease,
            timeout=3,
            lease_ttl=2,
            grace=0.1,
        )
    assert not marker.exists()


@pytest.mark.parametrize("reason,expected", [("timeout", 124), ("lease", 125)])
def test_reason_arriving_between_pipe_check_and_poll_is_not_lost(
    tmp_path, monkeypatch, reason, expected
):
    lease = tmp_path / "lease"
    lease.touch()
    real_popen = subprocess.Popen
    calls = 0
    original_sleep = time.sleep

    def launch(*args, **kwargs):
        nonlocal calls
        calls += 1
        process = real_popen(*args, **kwargs)
        if calls == 1:
            real_poll = process.poll
            paused = False

            def delayed_poll():
                nonlocal paused
                if not paused:
                    paused = True
                    original_sleep(0.7)
                return real_poll()

            process.poll = delayed_poll
        return process

    monkeypatch.setattr(runner.subprocess, "Popen", launch)
    assert (
        runner.run(
            [sys.executable, "-B", "-c", "import time;time.sleep(10)"],
            lease_file=lease,
            timeout=0.3 if reason == "timeout" else 3,
            lease_ttl=3 if reason == "timeout" else 0.3,
            grace=0.1,
        )
        == expected
    )


def test_transient_group_probe_permission_error_is_rechecked(monkeypatch):
    now = [0.0]
    probes = [PermissionError(1, "transient"), ProcessLookupError(3, "gone")]
    signals = []

    def killpg(pgid, sig):
        assert pgid == 123456
        if sig == 0:
            raise probes.pop(0) if len(probes) > 1 else probes[0]
        signals.append(sig)

    monkeypatch.setattr(runner.os, "killpg", killpg)
    monkeypatch.setattr(runner.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(
        runner.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds)
    )
    runner._stop_group(123456, 0.03)
    assert signals == [signal.SIGTERM]
    assert len(probes) == 1 and isinstance(probes[0], ProcessLookupError)
    assert now[0] <= 0.08


def test_persistent_group_probe_permission_error_is_bounded_and_propagated(monkeypatch):
    now = [0.0]
    error = PermissionError(1, "permission denied")
    attempts = []

    def killpg(pgid, sig):
        if sig == 0:
            attempts.append(now[0])
            raise error

    monkeypatch.setattr(runner.os, "killpg", killpg)
    monkeypatch.setattr(runner.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(
        runner.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds)
    )
    with pytest.raises(PermissionError) as raised:
        runner._stop_group(123456, 0.03)
    assert raised.value is error
    assert len(attempts) > 1
    assert now[0] <= 0.08


def test_signal_permission_error_is_not_retried_or_suppressed(monkeypatch):
    error = PermissionError(1, "signal denied")

    def killpg(pgid, sig):
        raise error

    monkeypatch.setattr(runner.os, "killpg", killpg)
    with pytest.raises(PermissionError) as raised:
        runner._stop_group(123456, 0.03)
    assert raised.value is error


def test_late_probe_permission_error_shares_cleanup_deadline(monkeypatch):
    now = [0.0]
    error = PermissionError(1, "late permission denied")
    attempts = []

    def killpg(pgid, sig):
        if sig == 0 and now[0] >= 0.03:
            attempts.append(now[0])
            raise error

    monkeypatch.setattr(runner.os, "killpg", killpg)
    monkeypatch.setattr(runner.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(
        runner.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds)
    )
    with pytest.raises(PermissionError) as raised:
        runner._stop_group(123456, 0.03)
    assert raised.value is error
    assert len(attempts) > 1
    assert 0.03 <= attempts[0] <= now[0] <= 0.08
