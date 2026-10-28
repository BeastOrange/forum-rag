# -*- coding: utf-8 -*-
# Author: konrad
# WQ_ID: OB53521
# Encoding: utf-8

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from functools import lru_cache
from pathlib import Path

from wq_forum_rag.embeddings import EmbeddingBackend, HashEmbeddingBackend, build_embedding_backend
from wq_forum_rag.models import hash_text


@lru_cache(maxsize=2048)
def _hash_vector(text: str, dims: int, seed: str) -> tuple[float, ...]:
    """Cache only a deterministic pure computation, never a mutable result."""
    return tuple(HashEmbeddingBackend(dims=dims, seed=seed).embed_query(text))


class CachedEmbeddingBackend:
    def __init__(
        self,
        db_path: str | Path,
        backend: EmbeddingBackend | None = None,
        backend_id: str | None = None,
        *,
        writable: bool = True,
    ) -> None:
        from wq_forum_rag.search_index import init_search_schema

        self.db_path = Path(db_path)
        self.backend = backend or build_embedding_backend()
        self.backend_id = backend_id or _backend_id(self.backend)
        self.writable = writable
        if self.writable:
            with closing(sqlite3.connect(self.db_path)) as conn, conn:
                init_search_schema(conn)

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float] | None] = []
        misses: list[tuple[int, str, str]] = []
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            for index, text in enumerate(texts):
                content_hash = hash_text(text)
                cached = self._cached_vector(conn, content_hash)
                vectors.append(cached)
                if cached is None:
                    misses.append((index, content_hash, text))
            if self.writable:
                self._fill_misses(conn, misses, vectors)
            else:
                self._fill_misses_read_only(misses, vectors)
        return [vector or [] for vector in vectors]

    def _cached_vector(self, conn: sqlite3.Connection, content_hash: str) -> list[float] | None:
        if not self.writable and not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = ?", ("embedding_cache",)
        ).fetchone():
            return None
        row = conn.execute(
            """
            SELECT vector_json FROM embedding_cache
            WHERE backend_id = ? AND content_hash = ?
            """,
            (self.backend_id, content_hash),
        ).fetchone()
        return None if row is None else list(json.loads(row[0]))

    def _fill_misses(
        self,
        conn: sqlite3.Connection,
        misses: list[tuple[int, str, str]],
        vectors: list[list[float] | None],
    ) -> None:
        if not misses:
            return
        embedded = self.backend.embed_documents([item[2] for item in misses])
        conn.executemany(
            """
            INSERT OR REPLACE INTO embedding_cache(backend_id, content_hash, vector_json, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            """,
            [
                (self.backend_id, content_hash, json.dumps(vector, separators=(",", ":")))
                for (_, content_hash, _), vector in zip(misses, embedded, strict=True)
            ],
        )
        for (index, _, _), vector in zip(misses, embedded, strict=True):
            vectors[index] = vector

    def _fill_misses_read_only(
        self,
        misses: list[tuple[int, str, str]],
        vectors: list[list[float] | None],
    ) -> None:
        if not misses:
            return
        if type(self.backend) is HashEmbeddingBackend:
            embedded = [list(_hash_vector(text, self.backend.dims, self.backend.seed))
                        for _, _, text in misses]
        else:
            embedded = self.backend.embed_documents([item[2] for item in misses])
        for (index, _, _), vector in zip(misses, embedded, strict=True):
            vectors[index] = vector


def embedding_cache_count(db_path: str | Path) -> int:
    from wq_forum_rag.search_index import init_search_schema

    with closing(sqlite3.connect(db_path)) as conn, conn:
        init_search_schema(conn)
        return int(conn.execute("SELECT COUNT(*) FROM embedding_cache").fetchone()[0])


def _backend_id(backend: EmbeddingBackend) -> str:
    parts = [backend.__class__.__name__]
    for name in ("model_name", "dims", "seed"):
        value = getattr(backend, name, None)
        if value is not None:
            parts.append(f"{name}={value}")
    return "|".join(parts)
