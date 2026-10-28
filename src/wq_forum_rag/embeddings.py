# -*- coding: utf-8 -*-
# Author: konrad
# WQ_ID: OB53521
# Encoding: utf-8

from __future__ import annotations

import hashlib
import copy
from collections import OrderedDict
import math
import re
import threading
import json
from pathlib import Path
from typing import Protocol

WORD_PATTERN = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]+")
DEFAULT_HASH_DIMS = 256
DEFAULT_HASH_SEED = "wq-forum-rag-v1"


class EmbeddingBackend(Protocol):
    """Pluggable embedding interface for dense retrieval."""

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of documents."""

    def embed_query(self, text: str) -> list[float]:
        """Embed a single query."""


def _normalize_token(token: str) -> str:
    return token.lower().strip()


def _iter_features(text: str) -> list[str]:
    features: list[str] = []
    for raw_token in WORD_PATTERN.findall(text.lower()):
        token = raw_token.strip()
        if not token:
            continue
        if re.fullmatch(r"[\u4e00-\u9fff]+", token):
            features.append(token)
            features.extend(token[index] for index in range(len(token)))
            features.extend(
                token[index : index + 2]
                for index in range(max(len(token) - 1, 0))
            )
            continue
        features.append(token)
        if len(token) >= 3:
            features.extend(
                token[index : index + 3]
                for index in range(len(token) - 2)
            )
    return features


def _l2_normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0:
        return vector
    return [value / norm for value in vector]


class HashEmbeddingBackend:
    """Deterministic offline embedding backend for tests and fallback."""

    def __init__(self, dims: int = DEFAULT_HASH_DIMS, seed: str = DEFAULT_HASH_SEED):
        if dims <= 0:
            raise ValueError("dims must be positive")
        self.dims = dims
        self.seed = _normalize_token(seed)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self.embed_query(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        vector = [0.0] * self.dims
        for feature in _iter_features(text):
            digest = hashlib.sha256(f"{self.seed}:{feature}".encode("utf-8")).digest()
            bucket = int.from_bytes(digest[:4], byteorder="big") % self.dims
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            magnitude = 1.0 + (digest[5] / 255.0)
            vector[bucket] += sign * magnitude
        return _l2_normalize(vector)


class FastEmbedBackend:
    """Optional fastembed wrapper for local dense retrieval."""

    def __init__(
        self,
        model_name: str,
        cache_dir: str | Path | None = None,
        batch_size: int = 32,
    ) -> None:
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError("fastembed is not installed") from exc

        kwargs: dict[str, object] = {"model_name": model_name}
        if cache_dir is not None:
            kwargs["cache_dir"] = str(cache_dir)
        self._model = TextEmbedding(**kwargs)
        self._batch_size = batch_size
        self.model_name = model_name

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        vectors = self._model.embed(texts, batch_size=self._batch_size)
        return [list(vector) for vector in vectors]

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]


def build_embedding_backend(
    model_name: str | None = None,
    cache_dir: str | Path | None = None,
    allow_download: bool = False,
) -> EmbeddingBackend:
    """
    Prefer fastembed only when the caller points to a local model/cache
    or explicitly allows model download. Otherwise use deterministic hash.
    """

    if model_name or cache_dir or allow_download:
        try:
            resolved_name = model_name or "BAAI/bge-small-en-v1.5"
            return FastEmbedBackend(
                model_name=resolved_name,
                cache_dir=cache_dir,
            )
        except Exception:
            pass
    return HashEmbeddingBackend()

# This separate backend never falls back to hashing or downloads model files.
SEMANTIC_MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"


def file_signature(path: str | Path) -> tuple:
    path = Path(path)
    def signature(stat):
        return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
    return signature(path.lstat()), signature(path.stat())


_CACHE_LIMIT = 4
_model_identity_cache = OrderedDict()
_model_identity_lock = threading.RLock()


def model_identity(model_dir: str | Path) -> dict:
    directory = Path(model_dir).resolve(strict=True)
    with _model_identity_lock:
        cached = _model_identity_cache.get(directory)
        if cached is not None:
            signatures, identity = cached
            if all(file_signature(directory / name) == value for name, value in signatures.items()):
                _model_identity_cache.move_to_end(directory)
                return copy.deepcopy(identity)
        identity, signatures = _validate_model_identity(directory)
        _model_identity_cache[directory] = (signatures, identity)
        _model_identity_cache.move_to_end(directory)
        while len(_model_identity_cache) > _CACHE_LIMIT:
            _model_identity_cache.popitem(last=False)
        return copy.deepcopy(identity)


def _validate_model_identity(directory: Path):
    spec_signature = file_signature(directory / "model-spec.json")
    spec = json.loads((directory / "model-spec.json").read_text(encoding="utf-8"))
    files = spec.get("files") or spec.get("filehash") or spec.get("file_hashes")
    if not isinstance(files, dict) or not files or not spec.get("revision"):
        raise RuntimeError("model spec must contain revision and file hashes")
    if (spec.get("repo") or spec.get("repository")) != "qdrant/paraphrase-multilingual-MiniLM-L12-v2-onnx-Q":
        raise RuntimeError("unexpected model repository")
    required = {"model_optimized.onnx", "config.json", "tokenizer.json",
                "tokenizer_config.json", "special_tokens_map.json"}
    if not required.issubset(files):
        raise RuntimeError("model spec must include all model and tokenizer files")
    signatures = {"model-spec.json": spec_signature}
    for name, expected in files.items():
        path = (directory / name).resolve(strict=True)
        if not path.is_relative_to(directory) or not path.is_file():
            raise RuntimeError("invalid model file path")
        signatures[name] = file_signature(directory / name)
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        if digest.hexdigest() != expected:
            raise RuntimeError(f"model checksum mismatch: {name}")
    if any(file_signature(directory / name) != value for name, value in signatures.items()):
        raise RuntimeError("model changed during validation")
    return ({"model_name": SEMANTIC_MODEL_NAME, "repo": spec.get("repo") or spec.get("repository"),
             "revision": spec["revision"], "files": files, "dimension": 384,
             "preprocessing": "clean community title content joined with spaces; query unchanged; L2 normalized"}, signatures)


class LocalSemanticBackend:
    def __init__(self, model_dir: str | Path, *, threads: int = 1, batch_size: int = 32):
        from fastembed import TextEmbedding

        self._lock = threading.Lock()
        self._batch_size = batch_size
        self._model = TextEmbedding(model_name=SEMANTIC_MODEL_NAME, threads=threads,
                                    specific_model_path=str(Path(model_dir).resolve()),
                                    local_files_only=True, providers=["CPUExecutionProvider"])

    def embed_documents(self, texts: list[str]):
        with self._lock:
            return list(self._model.embed(texts, batch_size=self._batch_size, parallel=None))

    def embed_query(self, text: str):
        with self._lock:
            return next(iter(self._model.query_embed(text)))


_semantic_models: OrderedDict[tuple, LocalSemanticBackend] = OrderedDict()
_semantic_models_lock = threading.Lock()


def get_semantic_model(model_dir: str | Path, identity: dict, *, threads: int = 1, batch_size: int = 32):
    directory = Path(model_dir).resolve(strict=True)
    key = (str(directory), json.dumps(identity, sort_keys=True), threads, batch_size)
    with _semantic_models_lock:
        if key in _semantic_models:
            _semantic_models.move_to_end(key)
            return _semantic_models[key]
        if model_identity(directory) != identity:
            raise RuntimeError("model changed before loading")
        names = ["model-spec.json", *identity.get("files", {})] if identity.get("files") else []
        signatures = {name: file_signature(directory / name) for name in names}
        model = LocalSemanticBackend(directory, threads=threads, batch_size=batch_size)
        if (model_identity(directory) != identity or
                any(file_signature(directory / name) != value for name, value in signatures.items())):
            raise RuntimeError("model changed during loading")
        _semantic_models[key] = model
        while len(_semantic_models) > _CACHE_LIMIT:
            _semantic_models.popitem(last=False)
        return model
