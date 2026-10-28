"""Explicit, verified preparation of the optional offline semantic model."""

from __future__ import annotations

import hashlib
import http.client
import json
import math
import os
import random
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

REPOSITORY = "qdrant/paraphrase-multilingual-MiniLM-L12-v2-onnx-Q"
REVISION = "faf4aa4225822f3bc6376869cb1164e8e3feedd0"
FILES = {
    "model_optimized.onnx": "634d0f66c29dc934c8fa72b8a4fe91dd4d420a22f1d82a241058d4316e659a99",
    "tokenizer_config.json": "0666eebf692422757e1dddf3c9fb1ded73ba3dc726c5828671fc89e45bf3609f",
    "special_tokens_map.json": "8c785abebea9ae3257b61681b4e6fd8365ceafde980c21970d001e834cf10835",
    "config.json": "c8ec081fdad2df991bf5abbf18418fec7a5cdaa421f60ffb060a30040b8c376f",
    "tokenizer.json": "fa685fc160bbdbab64058d4fc91b60e62d207e8dc60b9af5c002c5ab946ded00",
}
MAX_FILE_SIZES = {
    name: (300 * 1024 * 1024 if name.endswith(".onnx") else 20 * 1024 * 1024)
    for name in FILES
}
OFFICIAL = "https://huggingface.co"
MIRROR = "https://hf-mirror.com"


class ModelDownloadError(RuntimeError):
    """Model preparation failed; no unverified files were published."""


@dataclass(frozen=True)
class PrepareResult:
    path: Path
    cached: bool
    sources: set[str]
    attempts: int


def model_spec() -> dict:
    return {
        "model_name": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        "repository": REPOSITORY,
        "revision": REVISION,
        "dimension": 384,
        "query_prefix": "",
        "passage_prefix": "",
        "normalization": "l2",
        "pooling": "mean",
        "license": "apache-2.0",
        "files": dict(FILES),
    }


def _endpoint(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        raise ValueError(
            "endpoint must be an HTTPS origin without credentials, path, query or fragment"
        )
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("invalid endpoint port") from exc
    return value.rstrip("/")


def _sources(source: str, preferred: str) -> list[str]:
    aliases = {"official": OFFICIAL, "mirror": MIRROR}
    if source == "auto":
        if preferred not in aliases:
            raise ValueError("preferred source must be official or mirror")
        first = aliases[preferred]
        return [first, MIRROR if first == OFFICIAL else OFFICIAL]
    return [_endpoint(aliases.get(source, source))]


def _open(url: str, connect_timeout: float, read_timeout: float):
    class Connection(http.client.HTTPSConnection):
        def getresponse(self):
            if self.sock is not None:
                self.sock.settimeout(read_timeout)
            return super().getresponse()

    class Handler(HTTPSHandler):
        def https_open(self, request):
            return self.do_open(Connection, request, context=self._context)

    class Redirect(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            if urlsplit(newurl).scheme != "https":
                raise URLError("refusing non-HTTPS redirect")
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    return build_opener(Handler(), Redirect()).open(
        Request(url, headers={"User-Agent": "wq-forum-rag-model-prepare"}),
        timeout=connect_timeout,
    )


def _valid_cache(path: Path) -> bool:
    try:
        if (
            json.loads((path / "model-spec.json").read_text(encoding="utf-8"))
            != model_spec()
        ):
            return False
        for name, expected in FILES.items():
            digest = hashlib.sha256()
            with (path / name).open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            if digest.hexdigest() != expected:
                return False
        return True
    except (OSError, ValueError):
        return False


def prepare_model(
    path: str | Path,
    *,
    source: str = "auto",
    preferred: str = "official",
    attempts_per_source: int = 3,
    connect_timeout: float = 10,
    read_timeout: float = 30,
    backoff: float = 0.5,
) -> PrepareResult:
    """Prepare the pinned model explicitly; queries never call this function.

    ``source`` is auto, official, mirror, or an HTTPS origin. Auto tries both
    sources in preferred order. The result records successful sources and all attempts.
    Existing invalid directories are preserved and must be moved by the caller.
    """
    sources = _sources(source, preferred)
    if (
        not 1 <= attempts_per_source <= 10
        or not all(
            math.isfinite(value) for value in (connect_timeout, read_timeout, backoff)
        )
        or not 0 < connect_timeout <= 3600
        or not 0 < read_timeout <= 3600
        or not 0 <= backoff <= 60
    ):
        raise ValueError(
            "attempts must be 1–10, timeouts must be > 0 and <= 3600 seconds, "
            "and backoff must be between 0 and 60 seconds"
        )
    target = Path(path).absolute()
    if target.exists() or target.is_symlink():
        if target.is_dir() and _valid_cache(target):
            return PrepareResult(target, True, set(), 0)
        raise ModelDownloadError(
            f"existing model directory is incomplete or invalid; preserved: {target}"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    attempts = 0
    used: set[str] = set()
    errors = []
    with tempfile.TemporaryDirectory(
        prefix=f".{target.name}-", dir=target.parent
    ) as tmp:
        staging = Path(tmp)
        for name, expected in FILES.items():
            success = False
            for endpoint in sources:
                for attempt in range(attempts_per_source):
                    attempts += 1
                    url = f"{endpoint}/{REPOSITORY}/resolve/{REVISION}/{name}"
                    try:
                        digest = hashlib.sha256()
                        size = 0
                        with (
                            _open(url, connect_timeout, read_timeout) as response,
                            (staging / name).open("wb") as output,
                        ):
                            while block := response.read(1024 * 1024):
                                size += len(block)
                                if size > MAX_FILE_SIZES[name]:
                                    raise ModelDownloadError(
                                        f"{name}: size limit exceeded"
                                    )
                                digest.update(block)
                                output.write(block)
                        if digest.hexdigest() != expected:
                            raise ModelDownloadError(f"{name}: checksum mismatch")
                        used.add(endpoint)
                        success = True
                        break
                    except HTTPError as exc:
                        errors.append(f"{endpoint}: HTTP {exc.code}")
                        exc.close()
                        retry = exc.code == 429 or 500 <= exc.code <= 599
                    except (URLError, OSError, http.client.HTTPException) as exc:
                        errors.append(f"{endpoint}: {type(exc).__name__}: {exc}")
                        retry = True
                    except ModelDownloadError as exc:
                        errors.append(f"{endpoint}: {exc}")
                        retry = False
                    (staging / name).unlink(missing_ok=True)
                    if not retry:
                        break
                    if attempt + 1 < attempts_per_source:
                        time.sleep(backoff * (2**attempt) * random.uniform(0.75, 1.25))
                if success:
                    break
            if not success:
                raise ModelDownloadError(
                    f"failed to prepare {name} after {attempts} attempts: {'; '.join(errors)}"
                )
        (staging / "model-spec.json").write_text(
            json.dumps(model_spec(), indent=2) + "\n", encoding="utf-8"
        )
        try:
            target.mkdir()
        except OSError as exc:
            raise ModelDownloadError(
                f"could not reserve model destination; existing destination preserved: {target}"
            ) from exc
        identity = target.stat()
        published = []
        try:
            # Hard links cannot replace an existing file. The spec is the final
            # completion marker, published only after every verified model file.
            for name in [*FILES, "model-spec.json"]:
                os.link(staging / name, target / name)
                published.append(name)
        except OSError as exc:
            try:
                current = target.stat()
                if (current.st_dev, current.st_ino) == (
                    identity.st_dev,
                    identity.st_ino,
                ):
                    for name in published:
                        destination = target / name
                        try:
                            if os.path.samefile(staging / name, destination):
                                destination.unlink()
                        except FileNotFoundError:
                            pass
                    try:
                        target.rmdir()
                    except OSError:
                        pass  # Preserve files placed there by someone else.
            except OSError:
                pass
            raise ModelDownloadError(f"could not publish model: {target}") from exc
    return PrepareResult(target, False, used, attempts)
