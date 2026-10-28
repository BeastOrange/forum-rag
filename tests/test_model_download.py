import hashlib
import io
import json
from urllib.error import HTTPError, URLError

import pytest

from wq_forum_rag import model_download as download


@pytest.fixture
def small_model(monkeypatch):
    files = {"config.json": b"config", "model_optimized.onnx": b"model"}
    monkeypatch.setattr(
        download,
        "FILES",
        {name: hashlib.sha256(data).hexdigest() for name, data in files.items()},
    )
    return files


def test_fallback_and_retry_publish_verified_model(tmp_path, monkeypatch, small_model):
    attempts = []

    def open_url(url, connect_timeout, read_timeout):
        attempts.append(url)
        if "huggingface.co" in url:
            raise HTTPError(url, 404, "missing", {}, None)
        if len([x for x in attempts if "hf-mirror.com" in x]) == 1:
            raise URLError("offline")
        return io.BytesIO(small_model[url.rsplit("/", 1)[-1]])

    monkeypatch.setattr(download, "_open", open_url)
    monkeypatch.setattr(download.time, "sleep", lambda _: None)
    result = download.prepare_model(tmp_path / "model", attempts_per_source=2)
    assert result.sources == {"https://hf-mirror.com"}
    assert result.attempts == 5
    assert (
        json.loads((tmp_path / "model/model-spec.json").read_text())["files"]
        == download.FILES
    )
    assert (tmp_path / "model/model_optimized.onnx").read_bytes() == b"model"


def test_corrupt_or_partial_download_never_publishes(
    tmp_path, monkeypatch, small_model
):
    monkeypatch.setattr(download, "_open", lambda *args: io.BytesIO(b"bad"))
    with pytest.raises(download.ModelDownloadError):
        download.prepare_model(
            tmp_path / "model", source="official", attempts_per_source=1
        )
    assert not (tmp_path / "model").exists()
    assert list(tmp_path.iterdir()) == []


def test_existing_directory_is_preserved_and_complete_cache_is_offline(
    tmp_path, monkeypatch, small_model
):
    target = tmp_path / "model"
    target.mkdir()
    (target / "sentinel").write_text("keep")
    with pytest.raises(download.ModelDownloadError):
        download.prepare_model(target)
    assert (target / "sentinel").read_text() == "keep"
    (target / "sentinel").unlink()
    for name, data in small_model.items():
        (target / name).write_bytes(data)
    (target / "model-spec.json").write_text(json.dumps(download.model_spec()))
    monkeypatch.setattr(
        download, "_open", lambda *args: pytest.fail("must stay offline")
    )
    result = download.prepare_model(target)
    assert result.cached and result.attempts == 0


def test_endpoint_validation_and_size_limit(tmp_path, monkeypatch, small_model):
    with pytest.raises(ValueError):
        download.prepare_model(tmp_path / "bad", source="http://example.com")
    monkeypatch.setattr(download, "MAX_FILE_SIZES", {name: 2 for name in small_model})
    monkeypatch.setattr(download, "_open", lambda *args: io.BytesIO(b"oversized"))
    with pytest.raises(download.ModelDownloadError):
        download.prepare_model(
            tmp_path / "model", source="https://example.com", attempts_per_source=1
        )
    assert not (tmp_path / "model").exists()


def test_interrupted_stream_retries_without_publishing_partial_file(
    tmp_path, monkeypatch, small_model
):
    class BrokenStream(io.BytesIO):
        def read(self, size=-1):
            if self.tell():
                raise OSError("connection lost")
            return super().read(2)

    calls = []

    def open_url(url, *args):
        calls.append(url)
        data = small_model[url.rsplit("/", 1)[-1]]
        return BrokenStream(data) if len(calls) == 1 else io.BytesIO(data)

    monkeypatch.setattr(download, "_open", open_url)
    monkeypatch.setattr(download.time, "sleep", lambda _: None)
    result = download.prepare_model(
        tmp_path / "model", source="official", attempts_per_source=2
    )
    assert result.attempts == 3
    assert (result.path / "config.json").read_bytes() == b"config"


def test_target_created_during_download_is_never_replaced(
    tmp_path, monkeypatch, small_model
):
    target = tmp_path / "model"
    created = []

    def open_url(url, *args):
        target.mkdir(exist_ok=True)
        created.append(target.stat().st_ino)
        return io.BytesIO(small_model[url.rsplit("/", 1)[-1]])

    monkeypatch.setattr(download, "_open", open_url)
    with pytest.raises(download.ModelDownloadError):
        download.prepare_model(target, source="official", attempts_per_source=1)
    assert target.stat().st_ino == created[0]
    assert list(target.iterdir()) == []
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize("field", ["connect_timeout", "read_timeout", "backoff"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_timeout_and_backoff_rejected_before_io(
    tmp_path, monkeypatch, field, value
):
    monkeypatch.setattr(
        download, "_open", lambda *args: pytest.fail("unexpected network")
    )
    with pytest.raises(ValueError):
        download.prepare_model(tmp_path / "model", **{field: value})
    assert list(tmp_path.iterdir()) == []


def test_publish_failure_cleans_own_files_and_preserves_foreign_file(
    tmp_path, monkeypatch, small_model
):
    import os

    target = tmp_path / "model"
    real_link = os.link
    calls = []

    def failing_link(src, dst):
        calls.append(dst)
        if len(calls) == 2:
            (target / "foreign").write_text("keep")
            raise OSError("publication interrupted")
        real_link(src, dst)

    monkeypatch.setattr(
        download,
        "_open",
        lambda url, *args: io.BytesIO(small_model[url.rsplit("/", 1)[-1]]),
    )
    monkeypatch.setattr(os, "link", failing_link)
    with pytest.raises(download.ModelDownloadError):
        download.prepare_model(target, source="official", attempts_per_source=1)
    assert [p.name for p in target.iterdir()] == ["foreign"]
    assert (target / "foreign").read_text() == "keep"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["model"]


def test_concurrent_preparations_publish_only_one_complete_model(
    tmp_path, monkeypatch, small_model
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    target = tmp_path / "model"
    barrier = Barrier(2)

    def open_url(url, *args):
        name = url.rsplit("/", 1)[-1]
        if name == "model_optimized.onnx":
            barrier.wait(timeout=5)
        return io.BytesIO(small_model[name])

    def prepare():
        try:
            return download.prepare_model(
                target, source="official", attempts_per_source=1
            )
        except download.ModelDownloadError as exc:
            return exc

    monkeypatch.setattr(download, "_open", open_url)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: prepare(), range(2)))
    assert sum(isinstance(result, download.PrepareResult) for result in results) == 1
    assert (
        sum(isinstance(result, download.ModelDownloadError) for result in results) == 1
    )
    assert download._valid_cache(target)
    assert [p.name for p in tmp_path.iterdir()] == ["model"]


def test_completion_marker_published_after_all_model_files(
    tmp_path, monkeypatch, small_model
):
    import os

    target = tmp_path / "model"
    real_link = os.link

    def inspect_link(src, dst):
        if dst.name == "model-spec.json":
            assert not dst.exists()
            assert all(
                (target / name).read_bytes() == data
                for name, data in small_model.items()
            )
        real_link(src, dst)

    monkeypatch.setattr(
        download,
        "_open",
        lambda url, *args: io.BytesIO(small_model[url.rsplit("/", 1)[-1]]),
    )
    monkeypatch.setattr(os, "link", inspect_link)
    download.prepare_model(target, source="official", attempts_per_source=1)
    assert download._valid_cache(target)


def test_script_runs_with_stdlib_only_and_reuses_complete_cache(tmp_path):
    import shutil
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    script = root / "scripts/prepare_semantic_model.py"
    help_result = subprocess.run(
        [sys.executable, "-B", "-S", str(script), "--help"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert help_result.returncode == 0, help_result.stderr
    assert "--model-dir" in help_result.stdout
    invalid = subprocess.run(
        [sys.executable, "-B", "-S", str(script), "--connect-timeout", "1e300"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert invalid.returncode == 1
    assert "3600 seconds" in invalid.stderr

    # Copy the actual entry point and downloader, replacing only pinned test data.
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    module_dir = repo / "src/wq_forum_rag"
    module_dir.mkdir(parents=True)
    shutil.copyfile(script, repo / "scripts/prepare_semantic_model.py")
    module = (root / "src/wq_forum_rag/model_download.py").read_text()
    data = b"small model"
    files = {"config.json": hashlib.sha256(data).hexdigest()}
    module = module.replace(
        "MAX_FILE_SIZES = {", f"FILES = {files!r}\nMAX_FILE_SIZES = {{", 1
    )
    (module_dir / "model_download.py").write_text(module)
    cache = tmp_path / "model"
    cache.mkdir()
    (cache / "config.json").write_bytes(data)
    spec = download.model_spec()
    spec["files"] = files
    (cache / "model-spec.json").write_text(json.dumps(spec))
    before = {p.name: p.stat().st_mtime_ns for p in cache.iterdir()}
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-S",
            str(repo / "scripts/prepare_semantic_model.py"),
            "--model-dir",
            str(cache),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert "local cache; attempts: 0" in result.stdout
    assert {p.name: p.stat().st_mtime_ns for p in cache.iterdir()} == before


@pytest.mark.parametrize("field", ["connect_timeout", "read_timeout", "backoff"])
def test_huge_finite_timeout_and_backoff_rejected_before_io(
    tmp_path, monkeypatch, field
):
    monkeypatch.setattr(
        download, "_open", lambda *args: pytest.fail("unexpected network")
    )
    with pytest.raises(ValueError):
        download.prepare_model(tmp_path / "model", **{field: 1e300})
    assert list(tmp_path.iterdir()) == []
