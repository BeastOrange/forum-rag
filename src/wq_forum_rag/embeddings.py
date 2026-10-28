# -*- coding: utf-8 -*-
# Author: konrad
# WQ_ID: OB53521
# Encoding: utf-8

from __future__ import annotations

import hashlib
import math
import re
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
