# Author: konrad
# WQ_ID: OB53521
# Encoding: utf-8

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator
from pathlib import Path

from wq_forum_rag.models import Chunk, Topic
from wq_forum_rag.parser import build_chunks


class ForumStore:
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

    def __enter__(self) -> ForumStore:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def upsert_topics(
        self,
        topics: Iterable[Topic],
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
        seen_ids: set[str] = set()
        with self.conn:
            for topic in topics:
                stats["seen"] += 1
                seen_ids.add(topic.id)
                topic.content_hash = topic.compute_content_hash()
                existing = self._get_topic_hash(topic.id)
                if existing == topic.content_hash and self._has_chunks(topic.id):
                    stats["skipped"] += 1
                    continue

                operation = "updated" if existing else "inserted"
                self._upsert_topic_row(topic)
                self.conn.execute("DELETE FROM chunks WHERE topic_id = ?", (topic.id,))
                chunks = build_chunks(topic, window_size=window_size, overlap=overlap)
                self.conn.executemany(
                    """
                    INSERT INTO chunks (
                        chunk_id,
                        parent_chunk_id,
                        topic_id,
                        community_id,
                        url,
                        created_at,
                        chunk_index,
                        chunk_level,
                        start_offset,
                        end_offset,
                        content,
                        content_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            chunk.id,
                            chunk.parent_id,
                            chunk.topic_id,
                            chunk.community_id,
                            chunk.url,
                            chunk.created_at,
                            chunk.chunk_index,
                            chunk.level,
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

            self.conn.execute(
                """
                INSERT INTO schema_meta(key, value)
                VALUES ('last_upsert_count', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (str(stats["seen"]),),
            )
        return stats, seen_ids

    def get_topic(self, topic_id: str) -> Topic | None:
        if not self._table_exists("topics"):
            return None
        row = self.conn.execute(
            """
            SELECT
                topic_id,
                community_id,
                community_title,
                title,
                body_text,
                url,
                created_at,
                author,
                vote_num,
                comment_count,
                comments_json,
                content_hash
            FROM topics
            WHERE topic_id = ?
            """,
            (topic_id,),
        ).fetchone()
        if row is None:
            return None
        payload = {
            "id": row["topic_id"],
            "community_id": row["community_id"],
            "community_title": row["community_title"],
            "title": row["title"],
            "body_text": row["body_text"],
            "url": row["url"],
            "created_at": row["created_at"],
            "author": row["author"],
            "vote_num": row["vote_num"],
            "comment_count": row["comment_count"],
            "comments": json.loads(row["comments_json"]),
            "content_hash": row["content_hash"],
        }
        return Topic.from_dict(payload)

    def iter_chunks(self, topic_id: str | None = None) -> Iterator[Chunk]:
        if not self._table_exists("chunks"):
            return
        sql = """
            SELECT
                chunk_id,
                parent_chunk_id,
                topic_id,
                community_id,
                url,
                created_at,
                chunk_index,
                chunk_level,
                start_offset,
                end_offset,
                content,
                content_hash
            FROM chunks
        """
        params: tuple[str, ...] = ()
        if topic_id is not None:
            sql += " WHERE topic_id = ?"
            params = (topic_id,)
        sql += " ORDER BY topic_id, chunk_level, chunk_index"
        rows = self.conn.execute(sql, params)
        for row in rows:
            yield Chunk(
                id=row["chunk_id"],
                parent_id=row["parent_chunk_id"],
                topic_id=row["topic_id"],
                community_id=row["community_id"],
                url=row["url"],
                created_at=row["created_at"],
                chunk_index=row["chunk_index"],
                level=row["chunk_level"],
                start_offset=row["start_offset"],
                end_offset=row["end_offset"],
                content=row["content"],
                content_hash=row["content_hash"],
            )

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS topics (
                topic_id TEXT PRIMARY KEY,
                community_id TEXT NOT NULL,
                community_title TEXT NOT NULL,
                title TEXT NOT NULL,
                body_text TEXT NOT NULL,
                url TEXT NOT NULL,
                created_at TEXT NOT NULL,
                author TEXT NOT NULL,
                vote_num INTEGER NOT NULL,
                comment_count INTEGER NOT NULL,
                comments_json TEXT NOT NULL,
                content_hash TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS chunks (
                chunk_id TEXT PRIMARY KEY,
                parent_chunk_id TEXT,
                topic_id TEXT NOT NULL,
                community_id TEXT NOT NULL,
                url TEXT NOT NULL,
                created_at TEXT NOT NULL,
                chunk_index INTEGER NOT NULL,
                chunk_level INTEGER NOT NULL,
                start_offset INTEGER NOT NULL,
                end_offset INTEGER NOT NULL,
                content TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                FOREIGN KEY(topic_id) REFERENCES topics(topic_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS embedding_cache (
                backend_id TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                vector_json TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (backend_id, content_hash)
            );

            CREATE INDEX IF NOT EXISTS idx_topics_content_hash
                ON topics(content_hash);
            CREATE INDEX IF NOT EXISTS idx_chunks_topic_id
                ON chunks(topic_id);
            """
        )
        self.conn.execute(
            """
            INSERT INTO schema_meta(key, value)
            VALUES ('schema_version', '1')
            ON CONFLICT(key) DO NOTHING
            """
        )
        self.conn.commit()

    def _get_topic_hash(self, topic_id: str) -> str | None:
        if not self._table_exists("topics"):
            return None
        row = self.conn.execute(
            "SELECT content_hash FROM topics WHERE topic_id = ?",
            (topic_id,),
        ).fetchone()
        return None if row is None else str(row["content_hash"])

    def _has_chunks(self, topic_id: str) -> bool:
        if not self._table_exists("chunks"):
            return False
        row = self.conn.execute(
            "SELECT 1 FROM chunks WHERE topic_id = ? LIMIT 1",
            (topic_id,),
        ).fetchone()
        return row is not None

    def _table_exists(self, name: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (name,),
        ).fetchone()
        return row is not None

    def _upsert_topic_row(self, topic: Topic) -> None:
        self.conn.execute(
            """
            INSERT INTO topics (
                topic_id,
                community_id,
                community_title,
                title,
                body_text,
                url,
                created_at,
                author,
                vote_num,
                comment_count,
                comments_json,
                content_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(topic_id) DO UPDATE SET
                community_id = excluded.community_id,
                community_title = excluded.community_title,
                title = excluded.title,
                body_text = excluded.body_text,
                url = excluded.url,
                created_at = excluded.created_at,
                author = excluded.author,
                vote_num = excluded.vote_num,
                comment_count = excluded.comment_count,
                comments_json = excluded.comments_json,
                content_hash = excluded.content_hash
            """,
            (
                topic.id,
                topic.community_id,
                topic.community_title,
                topic.title,
                topic.body_text,
                topic.url,
                topic.created_at,
                topic.author,
                topic.vote_num,
                topic.comment_count,
                json.dumps(
                    [comment.to_dict() for comment in topic.comments],
                    ensure_ascii=False,
                ),
                topic.content_hash,
            ),
        )
