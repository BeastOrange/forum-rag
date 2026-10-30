from __future__ import annotations

import importlib
import os
import sqlite3
from pathlib import Path
from typing import Any

from wq_forum_rag.manifest import SourceManifestService
from wq_forum_rag.search_cache import CachedEmbeddingBackend
from wq_forum_rag.search_index import fts_candidates, rebuild_search_index

EXPORT_PATTERN = "WQPCommunityState_*.json"
AUTO_REFRESH_ENV = "WQ_FORUM_RAG_AUTO_REFRESH"
SOURCE_ENV = "WQ_FORUM_RAG_SOURCE"
SOURCE_DIR_ENV = "WQ_FORUM_RAG_SOURCE_DIR"
DISABLED_VALUES = {"0", "false", "no", "off"}


class ForumDatabaseBusyError(RuntimeError):
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        super().__init__(str(payload.get("reason") or "database is locked"))


def _load_module(name: str) -> Any:
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as exc:  # pragma: no cover
        raise RuntimeError(f"Missing required module: {name}") from exc


class ForumIndexService:
    def __init__(
        self,
        db_path: str | Path,
        *,
        auto_refresh: bool = True,
        source: str | Path | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.auto_refresh = auto_refresh
        self.source = Path(source).expanduser() if source else None
        self.parser = _load_module("wq_forum_rag.parser")
        self.search_mod = _load_module("wq_forum_rag.search")
        self.storage = _load_module("wq_forum_rag.storage")

    def index_from_json(
        self,
        json_path: str | Path,
        rebuild: bool = False,
        prune: bool = True,
    ) -> dict[str, Any]:
        source = Path(json_path).expanduser()
        fingerprint = self._source_fingerprint(source)
        with self.storage.ForumStore(self.db_path) as store:
            known = dict(
                store.conn.execute("SELECT key, value FROM schema_meta").fetchall()
            )
            if not rebuild and all(
                known.get(key) == value for key, value in fingerprint.items()
            ):
                total = store.conn.execute("SELECT COUNT(*) FROM topics").fetchone()[0]
                return self._unchanged_payload(source, total)
            if rebuild:
                store.conn.execute("DELETE FROM chunks")
                store.conn.execute("DELETE FROM topics")
            stats, seen_ids = store.upsert_topics(self.parser.iter_topics(source))
            prune_summary = (
                self._prune_orphans(store.conn, seen_ids)
                if (prune and not rebuild)
                else {"pruned": 0, "protected_orphans": []}
            )
            with store.conn:
                store.conn.executemany(
                    """
                    INSERT INTO schema_meta(key, value) VALUES (?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    fingerprint.items(),
                )
            total = store.conn.execute("SELECT COUNT(*) FROM topics").fetchone()[0]
        search_index = rebuild_search_index(self.db_path)
        return {
            "status": "rebuilt" if rebuild else "indexed",
            "db": str(self.db_path),
            "json": str(source),
            "indexed_topics": total,
            "search_index": search_index,
            **stats,
            **prune_summary,
        }

    def auto_refresh_if_needed(self) -> dict[str, Any]:
        if not self.auto_refresh or not _auto_refresh_enabled():
            return {"status": "disabled", "db": str(self.db_path)}
        latest = latest_export_for(self.db_path, source=self.source)
        if latest is None:
            return {"status": "no_source", "db": str(self.db_path)}
        if not self._needs_refresh(latest):
            return {"status": "unchanged", "db": str(self.db_path), "json": str(latest)}
        try:
            index_result = self.index_from_json(latest, rebuild=False, prune=True)
            manifest_result = SourceManifestService(self.db_path).source_ingest_plan(
                latest,
                commit=True,
            )
        except sqlite3.OperationalError as exc:
            if is_database_locked(exc):
                return database_locked_status(
                    self.db_path, json_path=latest, reason=str(exc)
                )
            raise
        return {
            "status": "refreshed",
            "index": index_result,
            "manifest": manifest_result,
        }

    def search(self, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        if top_k <= 0 or not query.strip():
            return []
        self._ensure_readable()
        try:
            candidates = fts_candidates(
                self.db_path, query, kind="forum_chunk", limit=max(top_k * 20, 80)
            )
            if not candidates:
                return []
            rows = self._search_rows(candidates)
            topic_meta = {row["topic_id"]: row for row in rows}
            backend = CachedEmbeddingBackend(self.db_path, writable=False)
            hits = self.search_mod.ForumSearcher(
                rows, embedding_backend=backend
            ).search(
                query=query,
                top_k=len(rows),
            )
            return self._format_hits(hits, topic_meta, top_k)
        except sqlite3.OperationalError as exc:
            self._raise_if_locked(exc)
            raise

    def get_post(self, topic_id: str) -> dict[str, Any] | None:
        self._ensure_readable()
        try:
            with self.storage.ForumStore(self.db_path, initialize=False) as store:
                topic = store.get_topic(topic_id)
        except sqlite3.OperationalError as exc:
            self._raise_if_locked(exc)
            raise
        if topic is None:
            return None
        payload = topic.to_dict()
        payload.setdefault("topic_id", payload.get("id", topic_id))
        return payload

    def find_by_exact(
        self,
        value: str,
        *,
        community: str | None = None,
        top_k: int = 5,
    ) -> list[dict[str, Any]]:
        self._ensure_readable()
        normalized = value.strip()
        if not normalized or top_k <= 0:
            return []
        try:
            rows = self._exact_rows(normalized, community=community, top_k=top_k)
        except sqlite3.OperationalError as exc:
            self._raise_if_locked(exc)
            raise
        return [self._format_exact_row(row, normalized) for row in rows]

    def related_posts(self, topic_id: str, top_k: int = 5) -> list[dict[str, Any]]:
        self._ensure_readable()
        base = self.get_post(topic_id)
        if not base:
            return []
        ranked = [
            item
            for item in self.search(base["title"], top_k=top_k + 3)
            if item["topic_id"] != topic_id
        ]
        same = [item for item in ranked if item["community_id"] == base["community_id"]]
        return (
            same
            + [item for item in ranked if item["community_id"] != base["community_id"]]
        )[:top_k]

    def _ensure_readable(self) -> None:
        refresh = self.auto_refresh_if_needed()
        if refresh.get("status") == "locked":
            raise ForumDatabaseBusyError(refresh)

    def _raise_if_locked(self, exc: sqlite3.OperationalError) -> None:
        raise_if_database_locked(exc, self.db_path)

    def _needs_refresh(self, source: Path) -> bool:
        if not self.db_path.exists():
            return True
        fingerprint = self._source_fingerprint(source)
        try:
            with self.storage.ForumStore(self.db_path, initialize=False) as store:
                known = dict(
                    store.conn.execute("SELECT key, value FROM schema_meta").fetchall()
                )
        except Exception:
            return True
        return any(known.get(key) != value for key, value in fingerprint.items())

    def _search_rows(self, candidates: list[str]) -> list[dict[str, Any]]:
        base_sql = """
            SELECT c.chunk_id, c.topic_id, c.community_id, c.content, t.title,
                   t.community_title, t.author, t.created_at, t.url, t.vote_num,
                   t.comment_count
            FROM chunks AS c
            JOIN topics AS t ON t.topic_id = c.topic_id
            WHERE c.chunk_level = 1
        """
        order_clause = " ORDER BY c.topic_id, c.chunk_level, c.chunk_index"
        with self.storage.ForumStore(self.db_path, initialize=False) as store:
            if not self._table_exists(store.conn, "chunks") or not self._table_exists(
                store.conn, "topics"
            ):
                return []
            if not candidates:
                return []
            placeholders = ",".join("?" * len(candidates))
            query = base_sql + f" AND c.chunk_id IN ({placeholders})" + order_clause
            return [
                dict(row)
                for row in store.conn.execute(query, tuple(candidates)).fetchall()
            ]

    def _format_hits(
        self,
        hits: list[Any],
        topic_meta: dict[str, dict[str, Any]],
        top_k: int,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        seen: set[str] = set()
        for hit in hits:
            if hit.topic_id in seen:
                continue
            seen.add(hit.topic_id)
            meta = topic_meta[hit.topic_id]
            results.append(self._format_hit(hit, meta))
            if len(results) >= top_k:
                break
        return results

    @staticmethod
    def _format_hit(hit: Any, meta: dict[str, Any]) -> dict[str, Any]:
        return {
            "topic_id": hit.topic_id,
            "community_id": meta["community_id"],
            "community_title": hit.community,
            "title": hit.title,
            "author": meta["author"],
            "datetime": meta["created_at"],
            "url": meta["url"],
            "vote_num": meta["vote_num"],
            "comment_num": meta["comment_count"],
            "snippet": hit.snippet,
            "score": hit.score,
        }

    def _exact_rows(
        self, normalized: str, *, community: str | None, top_k: int
    ) -> list[Any]:
        lowered = normalized.lower()
        escaped = lowered.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like_value = f"%{escaped}%"
        with self.storage.ForumStore(self.db_path, initialize=False) as store:
            if not self._table_exists(store.conn, "topics"):
                return []
            direct = store.conn.execute(
                """SELECT t.topic_id, t.community_id, t.community_title, t.title, t.author,
                          t.created_at, t.url, t.vote_num, t.comment_count,
                          100 AS exact_rank, t.body_text AS matched_content
                   FROM topics t WHERE t.topic_id = ?
                     AND (? IS NULL OR t.community_id = ? OR lower(t.community_title) = ?)
                   LIMIT 1""",
                (
                    normalized,
                    community,
                    community,
                    community.lower() if community else None,
                ),
            ).fetchall()
            if direct:
                return direct
            return store.conn.execute(
                """
                SELECT t.topic_id, t.community_id, t.community_title, t.title, t.author,
                       t.created_at, t.url, t.vote_num, t.comment_count,
                       MAX(CASE
                           WHEN t.topic_id = ? THEN 100
                           WHEN lower(t.url) = ? THEN 95
                           WHEN lower(t.title) = ? THEN 90
                           WHEN lower(t.title) LIKE ? ESCAPE '\\' THEN 75
                           WHEN lower(t.body_text) LIKE ? ESCAPE '\\' THEN 60
                           WHEN lower(t.comments_json) LIKE ? ESCAPE '\\' THEN 50
                           WHEN lower(c.content) LIKE ? ESCAPE '\\' THEN 40
                           ELSE 0
                       END) AS exact_rank,
                       MIN(CASE WHEN lower(c.content) LIKE ? ESCAPE '\\' THEN c.content ELSE NULL END)
                           AS matched_content
                FROM topics AS t
                LEFT JOIN chunks AS c ON c.topic_id = t.topic_id AND c.chunk_level = 1
                WHERE (? IS NULL OR t.community_id = ? OR lower(t.community_title) = ?)
                  AND (
                      t.topic_id = ? OR lower(t.url) = ? OR lower(t.title) = ?
                      OR lower(t.title) LIKE ? ESCAPE '\\' OR lower(t.body_text) LIKE ? ESCAPE '\\'
                      OR lower(t.comments_json) LIKE ? ESCAPE '\\' OR lower(c.content) LIKE ? ESCAPE '\\'
                  )
                GROUP BY t.topic_id
                ORDER BY exact_rank DESC, t.vote_num DESC, t.comment_count DESC, t.created_at DESC
                LIMIT ?
                """,
                (
                    normalized,
                    lowered,
                    lowered,
                    like_value,
                    like_value,
                    like_value,
                    like_value,
                    like_value,
                    community,
                    community,
                    community.lower() if community else None,
                    normalized,
                    lowered,
                    lowered,
                    like_value,
                    like_value,
                    like_value,
                    like_value,
                    top_k,
                ),
            ).fetchall()

    def _format_exact_row(self, row: Any, normalized: str) -> dict[str, Any]:
        return {
            "topic_id": str(row["topic_id"]),
            "community_id": row["community_id"],
            "community_title": row["community_title"],
            "title": row["title"],
            "author": row["author"],
            "datetime": row["created_at"],
            "url": row["url"],
            "vote_num": row["vote_num"],
            "comment_num": row["comment_count"],
            "snippet": self.search_mod._build_snippet(
                normalized,
                {
                    "content": row["matched_content"] or row["title"],
                    "title": row["title"],
                },
            ),
            "rank": row["exact_rank"],
        }

    @staticmethod
    def _prune_orphans(conn: Any, seen_ids: set[str]) -> dict[str, Any]:
        db_topic_ids = {
            row[0] for row in conn.execute("SELECT topic_id FROM topics").fetchall()
        }
        orphans = db_topic_ids - seen_ids
        if not orphans:
            return {"pruned": 0, "protected_orphans": []}
        protected = ForumIndexService._protected_topic_ids(conn)
        to_delete = sorted(orphans - protected)
        if to_delete:
            with conn:
                for offset in range(0, len(to_delete), 500):
                    batch = to_delete[offset : offset + 500]
                    placeholders = ",".join("?" * len(batch))
                    conn.execute(
                        f"DELETE FROM topics WHERE topic_id IN ({placeholders})",
                        tuple(batch),
                    )
        return {
            "pruned": len(to_delete),
            "protected_orphans": sorted(orphans & protected),
        }

    @staticmethod
    def _protected_topic_ids(conn: Any) -> set[str]:
        if not ForumIndexService._table_exists(conn, "knowledge_sources"):
            return set()
        return {
            row[0]
            for row in conn.execute(
                "SELECT DISTINCT topic_id FROM knowledge_sources WHERE topic_id IS NOT NULL"
            ).fetchall()
        }

    @staticmethod
    def _table_exists(conn: Any, name: str) -> bool:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,),
        ).fetchone()
        return row is not None

    @staticmethod
    def _source_fingerprint(source: Path) -> dict[str, str]:
        stat = source.stat()
        return {
            "source_path": str(source.resolve()),
            "source_mtime_ns": str(stat.st_mtime_ns),
            "source_size": str(stat.st_size),
        }

    def _unchanged_payload(self, source: Path, total: int) -> dict[str, Any]:
        return {
            "status": "unchanged",
            "db": str(self.db_path),
            "json": str(source),
            "indexed_topics": total,
        }


def latest_export_for(
    db_path: str | Path, *, source: str | Path | None = None
) -> Path | None:
    configured = _configured_source(source)
    if configured:
        return _latest_from_source(configured)
    stored = _stored_source_path(Path(db_path))
    if stored is not None:
        latest = _latest_related_export(stored)
        if latest is not None:
            return latest
    for root in _default_source_roots(Path(db_path)):
        latest = _latest_from_source(root)
        if latest is not None:
            return latest
    return None


def _configured_source(source: str | Path | None) -> Path | None:
    if source:
        return Path(source).expanduser()
    env_source = os.environ.get(SOURCE_ENV) or os.environ.get(SOURCE_DIR_ENV)
    return Path(env_source).expanduser() if env_source else None


def _latest_from_source(source: Path) -> Path | None:
    if source.is_file():
        return source
    if not source.is_dir():
        return None
    exports = [path for path in source.glob(EXPORT_PATTERN) if path.is_file()]
    if not exports:
        return None
    return max(exports, key=lambda path: (path.stat().st_mtime_ns, path.name))


def _latest_related_export(source_path: Path) -> Path | None:
    if source_path.is_file():
        exports = [
            path for path in source_path.parent.glob(EXPORT_PATTERN) if path.is_file()
        ]
        if exports:
            return max(exports, key=lambda path: (path.stat().st_mtime_ns, path.name))
        return source_path
    return _latest_from_source(source_path)


def _stored_source_path(db_path: Path) -> Path | None:
    if not db_path.exists():
        return None
    try:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key = 'source_path' LIMIT 1"
            ).fetchone()
    except sqlite3.Error:
        return None
    if row is None or not row[0]:
        return None
    return Path(str(row[0])).expanduser()


def _default_source_roots(db_path: Path) -> list[Path]:
    roots: list[Path] = []
    if db_path.parent.name == ".cache":
        roots.append(db_path.parent.parent)
        roots.append(db_path.parent)
    return _dedupe_paths(roots)


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    unique: list[Path] = []
    for path in paths:
        key = str(path.expanduser().resolve())
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def _auto_refresh_enabled() -> bool:
    value = os.environ.get(AUTO_REFRESH_ENV, "1").strip().lower()
    return value not in DISABLED_VALUES


def is_database_locked(exc: sqlite3.OperationalError) -> bool:
    return "locked" in str(exc).lower()


def raise_if_database_locked(
    exc: sqlite3.OperationalError,
    db_path: str | Path,
    *,
    json_path: str | Path | None = None,
) -> None:
    if is_database_locked(exc):
        raise ForumDatabaseBusyError(
            database_locked_status(db_path, json_path=json_path, reason=str(exc))
        ) from exc


def database_locked_status(
    db_path: str | Path,
    *,
    json_path: str | Path | None = None,
    reason: str = "database is locked",
) -> dict[str, Any]:
    payload = {"status": "locked", "db": str(db_path), "reason": reason}
    if json_path is not None:
        payload["json"] = str(json_path)
    return payload
