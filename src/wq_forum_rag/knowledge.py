# -*- coding: utf-8 -*-
# Author: konrad
# WQ_ID: OB53521
# Encoding: utf-8

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from wq_forum_rag.models import hash_text, normalize_text

SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._/-]{1,120}$")
RELATION_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
PUBLISHED = "published"
DRAFT = "draft"


@dataclass(slots=True)
class KnowledgePage:
    slug: str
    title: str
    summary: str
    body: str
    status: str = DRAFT
    confidence: float = 0.0
    content_hash: str = ""

    def __post_init__(self) -> None:
        self.slug = normalize_slug(self.slug)
        self.title = normalize_text(self.title)
        self.summary = normalize_text(self.summary)
        self.body = normalize_text(self.body)
        self.status = normalize_status(self.status)
        self.confidence = max(0.0, min(float(self.confidence or 0.0), 1.0))
        if not self.content_hash:
            self.content_hash = hash_text(
                self.slug,
                self.title,
                self.summary,
                self.body,
                self.status,
                f"{self.confidence:.4f}",
            )


def normalize_slug(value: str) -> str:
    slug = (value or "").strip().lower().replace(" ", "-")
    if not SLUG_PATTERN.fullmatch(slug):
        raise ValueError("slug must match [a-z0-9][a-z0-9._/-]{1,120}")
    parts = slug.split("/")
    if any(part in {"", "."} for part in parts):
        raise ValueError("slug must not contain empty or current-directory segments")
    if any(part == ".." for part in parts):
        raise ValueError("slug must not contain path traversal")
    return slug


def normalize_status(value: str) -> str:
    status = (value or DRAFT).strip().lower()
    if status not in {DRAFT, PUBLISHED, "archived"}:
        raise ValueError("status must be draft, published, or archived")
    return status


def normalize_relation(value: str) -> str:
    relation = (value or "").strip().lower()
    if not RELATION_PATTERN.fullmatch(relation):
        raise ValueError("relation_type must match [a-z][a-z0-9_]{1,40}")
    return relation


class KnowledgeStore:
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

    def __enter__(self) -> KnowledgeStore:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def upsert_page(
        self,
        page: KnowledgePage,
        *,
        source_topic_ids: list[str],
        links: list[dict[str, Any]] | None = None,
        source_evidence: list[dict[str, Any]] | None = None,
        event_type: str = "proposed",
        event_note: str = "",
    ) -> KnowledgePage:
        links = links or []
        unique_sources = sorted({str(topic_id) for topic_id in source_topic_ids})
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO knowledge_pages (
                    slug, title, summary, body, status, confidence, content_hash,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(slug) DO UPDATE SET
                    title = excluded.title,
                    summary = excluded.summary,
                    body = excluded.body,
                    status = excluded.status,
                    confidence = excluded.confidence,
                    content_hash = excluded.content_hash,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    page.slug,
                    page.title,
                    page.summary,
                    page.body,
                    page.status,
                    page.confidence,
                    page.content_hash,
                ),
            )
            self.conn.execute("UPDATE knowledge_pages SET review_status = ? WHERE slug = ?", ("unreviewed", page.slug))
            self.conn.execute("DELETE FROM knowledge_sources WHERE slug = ?", (page.slug,))
            evidence = {str(item.get("topic_id", "")): item for item in (source_evidence or [])}
            self.conn.executemany(
                """
                INSERT INTO knowledge_sources(slug, topic_id, evidence_note, source_content_hash, claim, excerpt)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [(page.slug, topic_id, "", self._topic_hash(topic_id),
                  str(evidence.get(topic_id, {}).get("claim") or ""),
                  str(evidence.get(topic_id, {}).get("excerpt") or "")) for topic_id in unique_sources],
            )
            for link in links:
                self.link_pages(
                    page.slug,
                    str(link["target_slug"]),
                    str(link.get("relation_type", "related_to")),
                    weight=float(link.get("weight", 1.0)),
                    confidence=float(link.get("confidence", page.confidence)),
                )
            self.add_event(page.slug, event_type, event_note or f"{page.status}:{page.content_hash}")
            self._sync_fts(page.slug)
        return page

    def get_page(self, slug: str) -> dict[str, Any] | None:
        if not self._table_exists("knowledge_pages"):
            return None
        row = self.conn.execute(
            "SELECT * FROM knowledge_pages WHERE slug = ?",
            (normalize_slug(slug),),
        ).fetchone()
        if row is None:
            return None
        payload = dict(row)
        page_slug = payload["slug"]
        payload["sources"] = [dict(item) for item in self._rows("knowledge_sources", "slug", page_slug)]
        payload["links"] = [dict(item) for item in self._rows("knowledge_links", "source_slug", page_slug)]
        payload["backlinks"] = [dict(item) for item in self._rows("knowledge_links", "target_slug", page_slug)]
        payload["events"] = [dict(item) for item in self._event_rows(page_slug)]
        return payload

    def list_pages(self, *, include_drafts: bool = False) -> list[dict[str, Any]]:
        if not self._table_exists("knowledge_pages"):
            return []
        where = "" if include_drafts else "WHERE status = 'published'"
        rows = self.conn.execute(
            f"""
            SELECT * FROM knowledge_pages
            {where}
            ORDER BY status DESC, confidence DESC, updated_at DESC, slug
            """
        ).fetchall()
        return [dict(row) for row in rows]

    def topic_ids_exist(self, topic_ids: list[str]) -> bool:
        if not topic_ids:
            return False
        if not self._table_exists("topics"):
            return False
        placeholders = ",".join("?" for _ in topic_ids)
        row = self.conn.execute(
            f"SELECT COUNT(*) FROM topics WHERE topic_id IN ({placeholders})",
            tuple(topic_ids),
        ).fetchone()
        return int(row[0]) == len(set(topic_ids))

    def page_exists(self, slug: str) -> bool:
        if not self._table_exists("knowledge_pages"):
            return False
        row = self.conn.execute(
            "SELECT 1 FROM knowledge_pages WHERE slug = ? LIMIT 1",
            (normalize_slug(slug),),
        ).fetchone()
        return row is not None

    def link_pages(
        self,
        source_slug: str,
        target_slug: str,
        relation_type: str,
        *,
        weight: float = 1.0,
        confidence: float = 0.8,
    ) -> dict[str, Any]:
        source = normalize_slug(source_slug)
        target = normalize_slug(target_slug)
        relation = normalize_relation(relation_type)
        if not self.page_exists(source) or not self.page_exists(target):
            raise ValueError("both source_slug and target_slug must reference existing knowledge pages")
        bounded_confidence = max(0.0, min(float(confidence), 1.0))
        self.conn.execute(
            """
            INSERT INTO knowledge_links (
                source_slug, target_slug, relation_type, weight, confidence, created_at
            )
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(source_slug, target_slug, relation_type) DO UPDATE SET
                weight = excluded.weight,
                confidence = excluded.confidence
            """,
            (source, target, relation, float(weight), bounded_confidence),
        )
        self.add_event(source, "linked", f"{relation}:{target}")
        return {
            "source_slug": source,
            "target_slug": target,
            "relation_type": relation,
            "weight": float(weight),
            "confidence": bounded_confidence,
        }

    def set_status(self, slug: str, status: str, note: str = "") -> dict[str, Any] | None:
        page_slug = normalize_slug(slug)
        normalized_status = normalize_status(status)
        if not self.page_exists(page_slug):
            return None
        self.conn.execute(
            """
            UPDATE knowledge_pages
            SET status = ?, updated_at = CURRENT_TIMESTAMP
            WHERE slug = ?
            """,
            (normalized_status, page_slug),
        )
        self.add_event(page_slug, f"status:{normalized_status}", note)
        self._sync_fts(page_slug)
        return self.get_page(page_slug)

    def add_event(self, slug: str, event_type: str, note: str = "") -> None:
        self.conn.execute(
            """
            INSERT INTO knowledge_events(slug, event_type, note, created_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (normalize_slug(slug), normalize_text(event_type), normalize_text(note)),
        )

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS knowledge_pages (
                slug TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                summary TEXT NOT NULL,
                body TEXT NOT NULL,
                status TEXT NOT NULL,
                confidence REAL NOT NULL,
                content_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS knowledge_sources (
                slug TEXT NOT NULL,
                topic_id TEXT NOT NULL,
                evidence_note TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(slug, topic_id),
                FOREIGN KEY(slug) REFERENCES knowledge_pages(slug) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS knowledge_links (
                source_slug TEXT NOT NULL,
                target_slug TEXT NOT NULL,
                relation_type TEXT NOT NULL,
                weight REAL NOT NULL,
                confidence REAL NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(source_slug, target_slug, relation_type)
            );

            CREATE TABLE IF NOT EXISTS knowledge_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                slug TEXT NOT NULL,
                event_type TEXT NOT NULL,
                note TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_knowledge_pages_status
                ON knowledge_pages(status);
            CREATE INDEX IF NOT EXISTS idx_knowledge_links_target
                ON knowledge_links(target_slug);
            """
        )
        for table, column, definition in (
            ("knowledge_pages", "review_status", "TEXT NOT NULL DEFAULT 'unreviewed'"),
            ("knowledge_sources", "source_content_hash", "TEXT NOT NULL DEFAULT ''"),
            ("knowledge_sources", "claim", "TEXT NOT NULL DEFAULT ''"),
            ("knowledge_sources", "excerpt", "TEXT NOT NULL DEFAULT ''"),
        ):
            columns = {row[1] for row in self.conn.execute(f"PRAGMA table_info({table})")}
            if column not in columns:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        self.conn.commit()

    def _topic_hash(self, topic_id: str) -> str:
        row = self.conn.execute("SELECT content_hash FROM topics WHERE topic_id = ?", (topic_id,)).fetchone()
        return str(row[0]) if row else ""

    def _sync_fts(self, slug: str) -> None:
        if not self._table_exists("document_fts"):
            return
        self.conn.execute("DELETE FROM document_fts WHERE kind = 'knowledge_page' AND record_id = ?", (slug,))
        self.conn.execute(
            "INSERT INTO document_fts(kind, record_id, topic_id, title, community, content) "
            "SELECT 'knowledge_page', slug, slug, title, status, summary || char(10) || body "
            "FROM knowledge_pages WHERE slug = ?", (slug,))

    def _rows(self, table: str, column: str, slug: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            f"SELECT * FROM {table} WHERE {column} = ? ORDER BY 1, 2",
            (slug,),
        ).fetchall()

    def _event_rows(self, slug: str) -> list[sqlite3.Row]:
        if not self._table_exists("knowledge_events"):
            return []
        return self.conn.execute(
            "SELECT * FROM knowledge_events WHERE slug = ? ORDER BY event_id DESC LIMIT 20",
            (slug,),
        ).fetchall()

    def _table_exists(self, name: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (name,),
        ).fetchone()
        return row is not None
