from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from wq_forum_rag.models import hash_text, normalize_text

SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._/-]{0,120}$")
_SLUG_REPLACE = re.compile(r"[^a-z0-9._/-]+")
_H1_PATTERN = re.compile(r"^\s{0,3}#\s+(.+?)\s*$", re.MULTILINE)


def _slug_from_filename(stem: str) -> str:
    lowered = stem.strip().lower().replace(" ", "-").replace("_", "-")
    cleaned = _SLUG_REPLACE.sub("-", lowered).strip("-/")
    cleaned = re.sub(r"-{2,}", "-", cleaned)
    if not cleaned:
        raise ValueError(f"cannot derive slug from filename: {stem!r}")
    if not SLUG_PATTERN.fullmatch(cleaned):
        raise ValueError(f"derived slug {cleaned!r} does not match pattern")
    return cleaned


def _title_from_body(body: str, fallback: str) -> str:
    match = _H1_PATTERN.search(body)
    if match:
        return normalize_text(match.group(1))
    return normalize_text(fallback)


@dataclass(slots=True)
class Document:
    slug: str
    title: str
    source_path: str
    body: str
    content_hash: str = ""
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self) -> None:
        if not SLUG_PATTERN.fullmatch(self.slug):
            raise ValueError(f"invalid slug: {self.slug!r}")
        self.title = normalize_text(self.title) or self.slug
        self.source_path = (self.source_path or "").strip()
        self.body = normalize_text(self.body)
        if not self.content_hash:
            self.content_hash = hash_text(self.slug, self.title, self.body)

    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "title": self.title,
            "source_path": self.source_path,
            "body": self.body,
            "content_hash": self.content_hash,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(slots=True)
class DocChunk:
    chunk_id: str
    slug: str
    chunk_index: int
    start_offset: int
    end_offset: int
    content: str
    content_hash: str = ""

    def __post_init__(self) -> None:
        if not self.content_hash:
            self.content_hash = hash_text(
                self.slug, str(self.chunk_index), str(self.start_offset), self.content
            )


def iter_documents(directory: str | Path) -> Iterator[Document]:
    root = Path(directory)
    if not root.is_dir():
        raise NotADirectoryError(f"not a directory: {root}")
    for md_path in sorted(root.rglob("*.md")):
        if not md_path.is_file():
            continue
        relative = md_path.relative_to(root)
        slug = _slug_from_filename(md_path.stem)
        raw_body = md_path.read_text(encoding="utf-8")
        title = _title_from_body(raw_body, md_path.stem)
        yield Document(
            slug=slug,
            title=title,
            source_path=str(relative),
            body=raw_body,
        )


def build_doc_chunks(
    document: Document,
    *,
    window_size: int = 1_500,
    overlap: int = 200,
) -> list[DocChunk]:
    if window_size <= 0:
        raise ValueError("window_size must be positive")
    if overlap < 0 or overlap >= window_size:
        raise ValueError("overlap must be >= 0 and smaller than window_size")
    body = document.body
    if not body:
        return []

    chunks: list[DocChunk] = []
    start = 0
    index = 0
    while start < len(body):
        end = min(len(body), start + window_size)
        content = body[start:end]
        chunks.append(
            DocChunk(
                chunk_id=f"{document.slug}#chunk:{index}",
                slug=document.slug,
                chunk_index=index,
                start_offset=start,
                end_offset=end,
                content=content,
            )
        )
        index += 1
        if end >= len(body):
            break
        start = max(end - overlap, start + 1)
    return chunks


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class DocumentStore:
    def __init__(self, db_path: str | Path, *, initialize: bool = True) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        if initialize:
            self._init_schema()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> DocumentStore:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def upsert_documents(
        self,
        documents: Iterable[Document],
        *,
        window_size: int = 1_500,
        overlap: int = 200,
    ) -> tuple[dict[str, int], set[str]]:
        stats = {
            "seen": 0,
            "inserted": 0,
            "updated": 0,
            "skipped": 0,
            "chunks_written": 0,
        }
        seen_slugs: set[str] = set()
        with self.conn:
            for document in documents:
                stats["seen"] += 1
                seen_slugs.add(document.slug)
                existing_hash = self._existing_hash(document.slug)
                if existing_hash == document.content_hash and self._has_chunks(
                    document.slug
                ):
                    stats["skipped"] += 1
                    continue

                operation = "updated" if existing_hash else "inserted"
                now = _now_iso()
                created_at = (
                    document.created_at
                    or self._existing_created_at(document.slug)
                    or now
                )
                self.conn.execute(
                    """
                    INSERT INTO documents (
                        slug, title, source_path, body, content_hash, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(slug) DO UPDATE SET
                        title = excluded.title,
                        source_path = excluded.source_path,
                        body = excluded.body,
                        content_hash = excluded.content_hash,
                        updated_at = excluded.updated_at
                    """,
                    (
                        document.slug,
                        document.title,
                        document.source_path,
                        document.body,
                        document.content_hash,
                        created_at,
                        now,
                    ),
                )
                self.conn.execute(
                    "DELETE FROM doc_chunks WHERE slug = ?", (document.slug,)
                )
                chunks = build_doc_chunks(
                    document, window_size=window_size, overlap=overlap
                )
                self.conn.executemany(
                    """
                    INSERT INTO doc_chunks (
                        chunk_id, slug, chunk_index, start_offset, end_offset, content, content_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            chunk.chunk_id,
                            chunk.slug,
                            chunk.chunk_index,
                            chunk.start_offset,
                            chunk.end_offset,
                            chunk.content,
                            chunk.content_hash,
                        )
                        for chunk in chunks
                    ],
                )
                stats[operation] += 1
                stats["chunks_written"] += len(chunks)
        return stats, seen_slugs

    def prune_missing(self, seen_slugs: set[str]) -> int:
        existing = {
            row[0] for row in self.conn.execute("SELECT slug FROM documents").fetchall()
        }
        to_delete = sorted(existing - seen_slugs)
        if not to_delete:
            return 0
        with self.conn:
            for offset in range(0, len(to_delete), 500):
                batch = to_delete[offset : offset + 500]
                placeholders = ",".join("?" * len(batch))
                self.conn.execute(
                    f"DELETE FROM documents WHERE slug IN ({placeholders})",
                    tuple(batch),
                )
        return len(to_delete)

    def get_document(self, slug: str) -> dict[str, Any] | None:
        if not self._table_exists("documents"):
            return None
        row = self.conn.execute(
            "SELECT * FROM documents WHERE slug = ?",
            (slug,),
        ).fetchone()
        return dict(row) if row else None

    def list_documents(self) -> list[dict[str, Any]]:
        if not self._table_exists("documents"):
            return []
        rows = self.conn.execute(
            "SELECT slug, title, source_path, content_hash, updated_at FROM documents ORDER BY slug"
        ).fetchall()
        return [dict(row) for row in rows]

    def count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0])

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS documents (
                slug TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                source_path TEXT NOT NULL,
                body TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS doc_chunks (
                chunk_id TEXT PRIMARY KEY,
                slug TEXT NOT NULL,
                chunk_index INTEGER NOT NULL,
                start_offset INTEGER NOT NULL,
                end_offset INTEGER NOT NULL,
                content TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                FOREIGN KEY(slug) REFERENCES documents(slug) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_doc_chunks_slug ON doc_chunks(slug);
            """
        )
        self.conn.commit()

    def _existing_hash(self, slug: str) -> str | None:
        row = self.conn.execute(
            "SELECT content_hash FROM documents WHERE slug = ?",
            (slug,),
        ).fetchone()
        return None if row is None else str(row["content_hash"])

    def _existing_created_at(self, slug: str) -> str | None:
        row = self.conn.execute(
            "SELECT created_at FROM documents WHERE slug = ?",
            (slug,),
        ).fetchone()
        return None if row is None else str(row["created_at"])

    def _has_chunks(self, slug: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM doc_chunks WHERE slug = ? LIMIT 1",
            (slug,),
        ).fetchone()
        return row is not None

    def _table_exists(self, name: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (name,),
        ).fetchone()
        return row is not None


def ingest_documents(
    directory: str | Path,
    db_path: str | Path,
    *,
    rebuild: bool = False,
    prune: bool = True,
) -> dict[str, Any]:
    directory = Path(directory)
    with DocumentStore(db_path) as store:
        if rebuild:
            with store.conn:
                store.conn.execute("DELETE FROM doc_chunks")
                store.conn.execute("DELETE FROM documents")
        stats, seen_slugs = store.upsert_documents(iter_documents(directory))
        # An empty scan usually means a wrong directory, not a request to delete
        # every stored document. Skipping prune keeps a typo from wiping the index.
        prune_skipped = bool(prune and not rebuild and not seen_slugs)
        pruned = (
            store.prune_missing(seen_slugs)
            if (prune and not rebuild and seen_slugs)
            else 0
        )
        total = store.count()
    return {
        "status": "rebuilt" if rebuild else "ingested",
        "directory": str(directory),
        "db": str(db_path),
        "indexed_documents": total,
        "pruned": pruned,
        "prune_skipped": prune_skipped,
        **stats,
    }
