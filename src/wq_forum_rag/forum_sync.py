"""Incremental and full forum sync from the Support API into the SQLite index.

API pages are processed and discarded page by page: every page is upserted in
its own short transaction and its FTS rows are refreshed right away, so search
keeps working while a sync runs. The only temporary file is a backup's
``*.partial``; the ``<db>.sync.lock`` file is removed when the run ends.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import logging
import math
import os
import re
import sqlite3
import threading
import time
import urllib.parse
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from wq_forum_rag import parser
from wq_forum_rag.indexer import (
    DISABLED_VALUES,
    ForumIndexService,
    database_locked_status,
    is_database_locked,
)
from wq_forum_rag.models import Topic
from wq_forum_rag.search_index import rebuild_search_index, reindex_forum_topics
from wq_forum_rag.storage import ForumStore
from wq_forum_rag.support_client import (
    PASSWORD_ENV,
    USERNAME_ENV,
    SupportAuthError,
    SupportBlockedError,
    SupportClient,
    SupportCredentials,
    SupportCredentialsMissing,
    SupportError,
)

AUTO_SYNC_ENV = "WQ_FORUM_RAG_AUTO_SYNC"
INTERVAL_ENV = "WQ_FORUM_RAG_SYNC_INTERVAL_DAYS"
KEEP_BACKUPS_ENV = "WQ_FORUM_RAG_KEEP_BACKUPS"
DEFAULT_INTERVAL_DAYS = 3.0
DEFAULT_KEEP_BACKUPS = 1
CUTOFF_OVERLAP = timedelta(hours=2)
LOCK_STALE_AFTER = timedelta(hours=12)
PAGE_SIZE = 100
MAX_ERROR_CHARS = 300

TOPICS_PATH = "/api/v2/community/topics.json"
POSTS_PATH = "/api/v2/community/posts.json"
CATEGORIES_PATH = "/api/v2/help_center/en-us/categories.json"
SECTIONS_PATH = "/api/v2/help_center/en-us/sections.json"
ARTICLES_PATH = "/api/v2/help_center/en-us/articles.json"
HELP_CENTER_ID = "help-center"
HELP_CENTER_TITLE = "Help Center"

SYNC_META_KEYS = (
    "sync_last_success_at",
    "sync_last_full_at",
    "sync_last_mode",
    "sync_last_attempt_at",
    "sync_last_status",
    "sync_last_error",
    "sync_last_stats",
)
_META_UPSERT_SQL = """
    INSERT INTO schema_meta(key, value) VALUES (?, ?)
    ON CONFLICT(key) DO UPDATE SET value = excluded.value
"""
_COUNT_KEYS = (
    "posts_seen",
    "posts_upserted",
    "posts_skipped",
    "comment_fetches",
    "comments_reused",
    "articles_seen",
    "articles_upserted",
    "pruned",
    "failed_items",
)

logger = logging.getLogger("wq_forum_rag.sync")


# -- settings -----------------------------------------------------------------


def auto_sync_enabled() -> bool:
    value = os.environ.get(AUTO_SYNC_ENV, "1").strip().lower()
    return value not in DISABLED_VALUES


def resolve_interval_days(value: float | None = None) -> float:
    if value is not None:
        return max(float(value), 0.0)
    raw = os.environ.get(INTERVAL_ENV, "").strip()
    if not raw:
        return DEFAULT_INTERVAL_DAYS
    try:
        days = float(raw)
    except ValueError:
        days = -1.0
    if days < 0 or not math.isfinite(days):
        logger.warning(
            "Invalid %s=%r; using %s days", INTERVAL_ENV, raw, DEFAULT_INTERVAL_DAYS
        )
        return DEFAULT_INTERVAL_DAYS
    return days


def resolve_keep_backups(value: int | None = None) -> int:
    if value is not None:
        return max(int(value), 0)
    raw = os.environ.get(KEEP_BACKUPS_ENV, "").strip()
    if not raw:
        return DEFAULT_KEEP_BACKUPS
    try:
        keep = int(raw)
    except ValueError:
        keep = -1
    if keep < 0:
        logger.warning(
            "Invalid %s=%r; keeping %d backup",
            KEEP_BACKUPS_ENV,
            raw,
            DEFAULT_KEEP_BACKUPS,
        )
        return DEFAULT_KEEP_BACKUPS
    return keep


def credentials_configured() -> bool:
    return bool(
        os.environ.get(USERNAME_ENV, "").strip() and os.environ.get(PASSWORD_ENV, "")
    )


def default_client_factory() -> SupportClient:
    return SupportClient(SupportCredentials.from_env())


# -- normalizers (mirror the reference extension's export mapping) ------------


def _vote(item: dict[str, Any]) -> int:
    value = item.get("vote_sum")
    if value is None:
        value = item.get("vote_count")
    return int(value or 0)


def _author(item: dict[str, Any]) -> str:
    value = item.get("author_id")
    return "" if value is None else str(value)


def post_record(post: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(post.get("id") or ""),
        "title": post.get("title") or "",
        "postContent": post.get("details") or "",
        "url": post.get("html_url") or "",
        "datetime": post.get("created_at") or "",
        "author": _author(post),
        "voteNum": _vote(post),
        "commentNum": int(post.get("comment_count") or 0),
        "topicId": str(post.get("topic_id") or ""),
    }


def comment_record(comment: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(comment.get("id") or ""),
        "author": _author(comment),
        "commentContent": comment.get("body") or "",
        "commentTimeDatetime": comment.get("updated_at")
        or comment.get("created_at")
        or "",
        "voteNum": _vote(comment),
    }


def article_record(article: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(article.get("id") or ""),
        "title": article.get("title") or "",
        "articleContent": article.get("body") or "",
        "url": article.get("html_url") or "",
        "createdAt": article.get("created_at") or "",
        "author": _author(article),
        "voteNum": _vote(article),
        "commentNum": int(article.get("comment_count") or 0),
        "sectionId": str(article.get("section_id") or ""),
    }


def topic_from_api_post(
    post: dict[str, Any],
    community_title: str,
    comments: Iterable[dict[str, Any]] | None = None,
) -> Topic:
    record = post_record(post)
    if comments is not None:
        record["comments"] = {
            item["id"]: item
            for item in (comment_record(comment) for comment in comments)
            if item["id"]
        }
    return parser.topic_from_post_record(
        record["topicId"], community_title, record["id"], record
    )


def topic_from_api_article(
    article: dict[str, Any],
    categories: dict[str, str],
    sections: dict[str, str],
) -> Topic:
    record = article_record(article)
    category_id = sections.get(record["sectionId"], "")
    if category_id in categories:
        category_title = categories[category_id]
    else:
        category_id, category_title = HELP_CENTER_ID, HELP_CENTER_TITLE
    return parser.topic_from_article_record(
        category_id, category_title, record["id"], record
    )


# -- helpers ------------------------------------------------------------------


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_time(value: object) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def sanitize_error(exc: BaseException | str) -> str:
    """Drop query strings (they may carry JWTs) and cap the length."""
    text = str(exc) or type(exc).__name__
    text = re.sub(r"(https?://[^\s?#'\"]+)[?#][^\s'\"]*", r"\1", text)
    return text[:MAX_ERROR_CHARS]


def _error_status(exc: BaseException) -> str:
    if isinstance(exc, SupportCredentialsMissing):
        return "no_credentials"
    if isinstance(exc, SupportBlockedError):
        return "blocked"
    if isinstance(exc, SupportAuthError):
        return "auth_failed"
    return "network_error"


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = ? LIMIT 1", (name,)
    ).fetchone()
    return row is not None


def _watermark_from_meta(meta: dict[str, str]) -> tuple[datetime | None, str | None]:
    synced = _parse_time(meta.get("sync_last_success_at"))
    if synced is not None:
        return synced, "sync"
    raw_ns = meta.get("source_mtime_ns")
    if raw_ns:
        try:
            seconds, remainder = divmod(int(raw_ns), 1_000_000_000)
            return (
                datetime.fromtimestamp(seconds, UTC)
                + timedelta(microseconds=remainder // 1_000),
                "export",
            )
        except (ValueError, OverflowError, OSError):
            pass
    return None, None


@dataclasses.dataclass
class _Run:
    full: bool
    cutoff: datetime | None
    counts: dict[str, int] = dataclasses.field(
        default_factory=lambda: dict.fromkeys(_COUNT_KEYS, 0)
    )
    seen_ids: set[str] = dataclasses.field(default_factory=set)
    truncated: bool = False


# -- service ------------------------------------------------------------------


class ForumSyncService:
    def __init__(
        self,
        db_path: str | Path,
        *,
        client_factory: Callable[[], Any] | None = None,
        keep_backups: int | None = None,
        now: Callable[[], datetime] | None = None,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self._client_factory = client_factory
        self.keep_backups = resolve_keep_backups(keep_backups)
        self._now = now or (lambda: datetime.now(UTC))
        self._progress = progress

    @property
    def lock_path(self) -> Path:
        return self.db_path.with_name(f"{self.db_path.name}.sync.lock")

    @property
    def backup_dir(self) -> Path:
        return self.db_path.parent / "backups"

    # -- read-only views ----------------------------------------------------

    def _read_meta(self) -> dict[str, str]:
        if not self.db_path.is_file():
            return {}
        uri = f"{self.db_path.resolve().as_uri()}?mode=ro"
        with contextlib.closing(sqlite3.connect(uri, uri=True)) as conn:
            if not _table_exists(conn, "schema_meta"):
                return {}
            rows = conn.execute("SELECT key, value FROM schema_meta").fetchall()
        return {str(key): str(value) for key, value in rows}

    def watermark(self) -> datetime | None:
        return _watermark_from_meta(self._read_meta())[0]

    def status(self, interval_days: float | None = None) -> dict[str, Any]:
        interval = resolve_interval_days(interval_days)
        exists = self.db_path.is_file()
        meta = self._read_meta() if exists else {}
        watermark, source = _watermark_from_meta(meta)
        due_at = watermark + timedelta(days=interval) if watermark else None
        payload: dict[str, Any] = {
            "db": str(self.db_path),
            "exists": exists,
            "auto_sync_enabled": auto_sync_enabled(),
            "interval_days": interval,
            "watermark": _iso(watermark) if watermark else None,
            "watermark_source": source,
            "due_at": _iso(due_at) if due_at else None,
            "stale": due_at is None or self._now() >= due_at,
            "needs_full": watermark is None,
            "running": self.lock_path.exists(),
            "credentials_configured": credentials_configured(),
            "keep_backups": self.keep_backups,
            "backups": [
                {"path": str(path), "size": path.stat().st_size}
                for path in reversed(self._backup_files())
            ],
        }
        for key in SYNC_META_KEYS:
            value: Any = meta.get(key)
            if key == "sync_last_stats" and value:
                try:
                    value = json.loads(value)
                except ValueError:
                    pass
            payload[key] = value
        return payload

    # -- sync ---------------------------------------------------------------

    def sync(
        self,
        *,
        full: bool = False,
        max_pages: int | None = None,
        community: str | None = None,
    ) -> dict[str, Any]:
        mode = "full" if full else "incremental"
        started = self._now()
        result: dict[str, Any] = {
            "status": "",
            "db": str(self.db_path),
            "mode": mode,
            "started_at": _iso(started),
            **dict.fromkeys(_COUNT_KEYS, 0),
            "backup": None,
            "backup_reused": False,
            "backups_removed": [],
        }
        if not full and not self.db_path.is_file():
            return {**result, "status": "needs_full", "reason": "no local database"}
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        if not self._acquire_lock(started):
            return {
                **result,
                "status": "already_running",
                "reason": f"lock held: {self.lock_path}",
            }
        clock = time.monotonic()
        try:
            outcome = self._sync_locked(
                result, started, full=full, max_pages=max_pages, community=community
            )
        finally:
            with contextlib.suppress(FileNotFoundError):
                self.lock_path.unlink()
            self._remove_partials()
        outcome["duration_s"] = round(time.monotonic() - clock, 2)
        return outcome

    def _sync_locked(
        self,
        result: dict[str, Any],
        started: datetime,
        *,
        full: bool,
        max_pages: int | None,
        community: str | None,
    ) -> dict[str, Any]:
        try:
            watermark = None if full else self.watermark()
        except sqlite3.OperationalError as exc:
            if is_database_locked(exc):
                return {
                    **result,
                    **database_locked_status(self.db_path, reason=str(exc)),
                }
            raise
        if not full and watermark is None:
            return {**result, "status": "needs_full", "reason": "no sync watermark"}
        run = _Run(full=full, cutoff=watermark - CUTOFF_OVERLAP if watermark else None)

        try:
            client = (self._client_factory or default_client_factory)()
            login = client.login()
        except SupportError as exc:
            return self._fail(result, run, started, exc)
        self._emit(f"login ok ({login.get('hops')} hops)")

        try:
            if self.db_path.is_file():
                backup, removed, reused = self._rotate_backups(started)
                result["backup"] = backup
                result["backup_reused"] = reused
                result["backups_removed"] = removed
            with ForumStore(self.db_path) as store:
                return self._run_store(
                    store,
                    client,
                    run,
                    result,
                    started,
                    max_pages=max_pages,
                    community=community,
                )
        except SupportError as exc:
            return self._fail(result, run, started, exc)
        except sqlite3.OperationalError as exc:
            if is_database_locked(exc):
                result.update(run.counts)
                return {
                    **result,
                    **database_locked_status(self.db_path, reason=str(exc)),
                }
            raise

    def _run_store(
        self,
        store: ForumStore,
        client: Any,
        run: _Run,
        result: dict[str, Any],
        started: datetime,
        *,
        max_pages: int | None,
        community: str | None,
    ) -> dict[str, Any]:
        store.conn.execute("PRAGMA cache_size=-65536")
        fts_existed = _table_exists(store.conn, "document_fts")
        communities = self._community_titles(client)

        if run.full:
            targets = [str(community)] if community else list(communities)
            for index, community_id in enumerate(targets, start=1):
                path = f"/api/v2/community/topics/{_quote(community_id)}/posts.json"
                for number, page in enumerate(
                    client.iter_pages(
                        path, {"page[size]": PAGE_SIZE}, max_pages=max_pages
                    ),
                    start=1,
                ):
                    posts = list(page.get("posts") or [])
                    self._process_posts(
                        store, client, run, posts, communities, fts_existed
                    )
                    self._emit(
                        f"community {index}/{len(targets)} "
                        f"{communities.get(community_id, community_id)}: "
                        f"page {number}, {len(posts)} posts"
                    )
                run.truncated |= bool(client.last_iteration_truncated)
        else:
            stopped = False
            params = {
                "page[size]": PAGE_SIZE,
                "sort_by": "updated_at",
                "sort_order": "desc",
            }
            for number, page in enumerate(
                client.iter_pages(POSTS_PATH, params, max_pages=max_pages), start=1
            ):
                posts = []
                for post in page.get("posts") or []:
                    if self._before_cutoff(post, run.cutoff):
                        stopped = True
                        break
                    posts.append(post)
                self._process_posts(store, client, run, posts, communities, fts_existed)
                self._emit(f"recent posts page {number}: {len(posts)} posts")
                if stopped:
                    break
            if not stopped:
                run.truncated |= bool(client.last_iteration_truncated)

        self._sync_articles(store, client, run, fts_existed, max_pages=max_pages)

        complete = (
            not run.truncated and community is None and run.counts["failed_items"] == 0
        )
        if run.full and complete and run.seen_ids:
            before = self._topic_ids(store.conn)
            summary = ForumIndexService._prune_orphans(store.conn, run.seen_ids)
            deleted = before - self._topic_ids(store.conn)
            if deleted and fts_existed:
                with store.conn:
                    reindex_forum_topics(store.conn, deleted)
            run.counts["pruned"] = int(summary["pruned"])
            result["protected_orphans"] = summary["protected_orphans"]

        if not fts_existed:
            result["search_index"] = rebuild_search_index(self.db_path)

        status = "synced" if complete else "partial"
        result.update(run.counts)
        result["status"] = status
        if not complete:
            result["reason"] = self._partial_reason(run, community)
        meta = {
            "sync_last_attempt_at": _iso(started),
            "sync_last_status": status,
            "sync_last_mode": result["mode"],
            "sync_last_error": result.get("reason", ""),
            "sync_last_stats": json.dumps(run.counts, sort_keys=True),
        }
        if complete:
            meta["sync_last_success_at"] = _iso(started)
            if run.full:
                meta["sync_last_full_at"] = _iso(started)
        with store.conn:
            store.conn.executemany(_META_UPSERT_SQL, list(meta.items()))
        logger.info("forum sync %s (%s): %s", status, result["mode"], run.counts)
        return result

    # -- posts --------------------------------------------------------------

    def _community_titles(self, client: Any) -> dict[str, str]:
        titles: dict[str, str] = {}
        for page in client.iter_pages(TOPICS_PATH, {"page[size]": PAGE_SIZE}):
            for topic in page.get("topics") or []:
                if topic.get("id") is not None:
                    titles[str(topic["id"])] = (
                        topic.get("name") or topic.get("title") or ""
                    )
        return titles

    @staticmethod
    def _before_cutoff(item: dict[str, Any], cutoff: datetime | None) -> bool:
        if cutoff is None:
            return False
        stamp = _parse_time(item.get("updated_at") or item.get("created_at"))
        return stamp is not None and stamp < cutoff

    def _process_posts(
        self,
        store: ForumStore,
        client: Any,
        run: _Run,
        posts: list[dict[str, Any]],
        communities: dict[str, str],
        fts_existed: bool,
    ) -> None:
        topics: list[Topic] = []
        to_fetch: list[tuple[dict[str, Any], str]] = []
        for post in posts:
            post_id = str(post.get("id") or "")
            if not post_id:
                continue
            run.counts["posts_seen"] += 1
            stored = store.get_topic(post_id)
            title = communities.get(str(post.get("topic_id") or ""), "")
            if not title and stored is not None:
                title = stored.community_title
            count = int(post.get("comment_count") or 0)
            if count == 0:
                topics.append(topic_from_api_post(post, title, comments=()))
            elif stored is not None and len(stored.comments) == count:
                topics.append(
                    dataclasses.replace(
                        topic_from_api_post(post, title),
                        comments=stored.comments,
                        comment_count=count,
                        content_hash="",
                    )
                )
                run.counts["comments_reused"] += 1
            else:
                to_fetch.append((post, title))

        if to_fetch:
            run.counts["comment_fetches"] += len(to_fetch)
            outcomes = client.map_concurrent(
                lambda item: self._fetch_comments(client, item[0]), to_fetch
            )
            fatal: BaseException | None = None
            for (post, title), outcome in outcomes:
                if isinstance(outcome, BaseException):
                    if isinstance(outcome, (SupportAuthError, SupportBlockedError)):
                        fatal = fatal or outcome
                    run.counts["failed_items"] += 1
                    logger.warning(
                        "comments for post %s failed: %s",
                        post.get("id"),
                        sanitize_error(outcome),
                    )
                    continue
                topics.append(topic_from_api_post(post, title, comments=outcome))
            if fatal is not None:
                self._store_page(store, run, topics, fts_existed, kind="posts")
                raise fatal
        self._store_page(store, run, topics, fts_existed, kind="posts")

    @staticmethod
    def _fetch_comments(client: Any, post: dict[str, Any]) -> list[dict[str, Any]]:
        path = f"/api/v2/community/posts/{_quote(str(post.get('id')))}/comments.json"
        comments: list[dict[str, Any]] = []
        for page in client.iter_pages(path, {"page[size]": PAGE_SIZE}):
            comments.extend(page.get("comments") or [])
        return comments

    # -- articles -----------------------------------------------------------

    def _sync_articles(
        self,
        store: ForumStore,
        client: Any,
        run: _Run,
        fts_existed: bool,
        *,
        max_pages: int | None,
    ) -> None:
        categories: dict[str, str] = {}
        for page in client.iter_pages(CATEGORIES_PATH, {"page[size]": PAGE_SIZE}):
            for category in page.get("categories") or []:
                if category.get("id") is not None:
                    categories[str(category["id"])] = (
                        category.get("name") or category.get("title") or ""
                    )
        sections: dict[str, str] = {}
        for page in client.iter_pages(SECTIONS_PATH, {"page[size]": PAGE_SIZE}):
            for section in page.get("sections") or []:
                if section.get("id") is not None:
                    sections[str(section["id"])] = str(section.get("category_id") or "")

        params: dict[str, Any] = {"page[size]": PAGE_SIZE}
        if not run.full:
            params.update({"sort_by": "updated_at", "sort_order": "desc"})
        stopped = False
        for number, page in enumerate(
            client.iter_pages(ARTICLES_PATH, params, max_pages=max_pages), start=1
        ):
            topics: list[Topic] = []
            for article in page.get("articles") or []:
                if self._before_cutoff(article, run.cutoff):
                    stopped = True
                    break
                if article.get("id") is None:
                    continue
                run.counts["articles_seen"] += 1
                topics.append(topic_from_api_article(article, categories, sections))
            self._store_page(store, run, topics, fts_existed, kind="articles")
            self._emit(f"articles page {number}: {len(topics)} articles")
            if stopped:
                break
        if not stopped:
            run.truncated |= bool(client.last_iteration_truncated)

    # -- storage ------------------------------------------------------------

    @staticmethod
    def _store_page(
        store: ForumStore,
        run: _Run,
        topics: list[Topic],
        fts_existed: bool,
        *,
        kind: str,
    ) -> None:
        if not topics:
            return
        stats, ids = store.upsert_topics(topics)
        run.seen_ids |= ids
        changed = stats["inserted"] + stats["updated"]
        if kind == "posts":
            run.counts["posts_upserted"] += changed
            run.counts["posts_skipped"] += stats["skipped"]
        else:
            run.counts["articles_upserted"] += changed
        if fts_existed and changed:
            with store.conn:
                reindex_forum_topics(store.conn, ids)

    @staticmethod
    def _topic_ids(conn: sqlite3.Connection) -> set[str]:
        return {str(row[0]) for row in conn.execute("SELECT topic_id FROM topics")}

    @staticmethod
    def _partial_reason(run: _Run, community: str | None) -> str:
        reasons = []
        if run.truncated:
            reasons.append("stopped by --max-pages")
        if community is not None:
            reasons.append(f"limited to community {community}")
        if run.counts["failed_items"]:
            reasons.append(f"{run.counts['failed_items']} items failed")
        return "; ".join(reasons)

    def _fail(
        self,
        result: dict[str, Any],
        run: _Run,
        started: datetime,
        exc: BaseException,
    ) -> dict[str, Any]:
        status = _error_status(exc)
        reason = sanitize_error(exc)
        result.update(run.counts)
        logger.warning("forum sync %s: %s", status, reason)
        self._record_failure(started, result["mode"], status, reason, run.counts)
        return {**result, "status": status, "reason": reason}

    def _record_failure(
        self,
        started: datetime,
        mode: str,
        status: str,
        reason: str,
        counts: dict[str, int],
    ) -> None:
        if not self.db_path.is_file():
            return
        meta = {
            "sync_last_attempt_at": _iso(started),
            "sync_last_status": status,
            "sync_last_mode": mode,
            "sync_last_error": reason,
            "sync_last_stats": json.dumps(counts, sort_keys=True),
        }
        try:
            with contextlib.closing(sqlite3.connect(self.db_path)) as conn, conn:
                if _table_exists(conn, "schema_meta"):
                    conn.executemany(_META_UPSERT_SQL, list(meta.items()))
        except sqlite3.Error as exc:
            logger.warning("could not record the sync failure: %s", exc)

    # -- backups ------------------------------------------------------------

    def _backup_files(self) -> list[Path]:
        if not self.backup_dir.is_dir():
            return []
        files = [
            path
            for path in self.backup_dir.glob(f"{self.db_path.stem}-before-*.sqlite3")
            if path.is_file()
        ]
        return sorted(files, key=lambda path: (path.stat().st_mtime_ns, path.name))

    def _rotate_backups(self, started: datetime) -> tuple[str | None, list[str], bool]:
        existing = self._backup_files()
        removed: list[str] = []
        reusable = self._reusable_backup(existing) if self.keep_backups else None
        if reusable is not None:
            # An unfinished earlier sync already backed up this state.
            others = [path for path in existing if path != reusable]
            while len(others) > self.keep_backups - 1:
                path = others.pop(0)
                path.unlink(missing_ok=True)
                removed.append(str(path))
            self._emit(f"backup reused: {reusable}")
            return str(reusable), removed, True
        while len(existing) > max(self.keep_backups - 1, 0):
            path = existing.pop(0)
            path.unlink(missing_ok=True)
            removed.append(str(path))
        if self.keep_backups == 0:
            return None, removed, False
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = started.astimezone(UTC).strftime("%Y%m%d-%H%M%S")
        final = self.backup_dir / f"{self.db_path.stem}-before-sync-{stamp}.sqlite3"
        partial = final.with_name(f"{final.name}.partial")
        try:
            with (
                contextlib.closing(sqlite3.connect(self.db_path)) as source,
                contextlib.closing(sqlite3.connect(partial)) as target,
            ):
                source.backup(target)
            os.replace(partial, final)
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
        self._emit(f"backup written: {final}")
        return str(final), removed, False

    def _reusable_backup(self, existing: list[Path]) -> Path | None:
        """Newest sync backup taken after the last successful sync, if any.

        Such a backup comes from a sync attempt that never finished, so the DB
        still holds the same pre-sync state (plus that attempt's pages). An
        export imported after the backup makes it stale, so it is not reused.
        """
        prefix = f"{self.db_path.stem}-before-sync-"
        stamped: list[tuple[datetime, Path]] = []
        for path in existing:
            if not path.name.startswith(prefix):
                continue
            raw = path.name.removeprefix(prefix).removesuffix(".sqlite3")
            try:
                taken = datetime.strptime(raw, "%Y%m%d-%H%M%S").replace(tzinfo=UTC)
            except ValueError:
                continue
            stamped.append((taken, path))
        if not stamped:
            return None
        taken, newest = max(stamped)
        meta = self._read_meta()
        succeeded = _parse_time(meta.get("sync_last_success_at"))
        if succeeded is not None and taken <= succeeded:
            return None
        exported, _ = _watermark_from_meta(
            {"source_mtime_ns": meta.get("source_mtime_ns", "")}
        )
        if exported is not None and taken < exported:
            return None
        return newest

    def _remove_partials(self) -> None:
        if not self.backup_dir.is_dir():
            return
        for path in self.backup_dir.glob(f"{self.db_path.stem}-before-*.partial"):
            path.unlink(missing_ok=True)

    # -- lock ---------------------------------------------------------------

    def _acquire_lock(self, started: datetime) -> bool:
        for attempt in range(2):
            try:
                fd = os.open(
                    self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644
                )
            except FileExistsError:
                if attempt == 0 and self._lock_is_stale():
                    with contextlib.suppress(FileNotFoundError):
                        self.lock_path.unlink()
                    continue
                return False
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"pid": os.getpid(), "started_at": _iso(started)}, handle)
            return True
        return False

    def _lock_is_stale(self) -> bool:
        try:
            info = json.loads(self.lock_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return True
        except (OSError, ValueError):
            info = {}
        if not isinstance(info, dict):
            info = {}
        started = _parse_time(info.get("started_at"))
        if started is None:
            try:
                started = datetime.fromtimestamp(self.lock_path.stat().st_mtime, UTC)
            except FileNotFoundError:
                return True
        if self._now() - started > LOCK_STALE_AFTER:
            return True
        pid = info.get("pid")
        if os.name == "posix" and isinstance(pid, int) and pid > 0:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            except OSError:
                return False
        return False

    def _emit(self, message: str) -> None:
        if self._progress is not None:
            self._progress(message)


def _quote(value: str) -> str:
    return urllib.parse.quote(value, safe="")


# -- support check ------------------------------------------------------------


def support_check(client_factory: Callable[[], Any] | None = None) -> dict[str, Any]:
    """Read-only check: BRAIN login, Support session, and one 2-post page."""
    try:
        client = (client_factory or default_client_factory)()
        login = client.login()
        page = client.get_json(POSTS_PATH, {"page[size]": 2})
    except SupportError as exc:
        return {"status": _error_status(exc), "reason": sanitize_error(exc)}
    return {
        "status": "ok",
        "login_status": login.get("login_status"),
        "hops": login.get("hops"),
        "role": login.get("role"),
        "posts_status": 200,
        "posts_returned": len(page.get("posts") or []),
    }


# -- MCP startup ----------------------------------------------------------------


def maybe_start_background_sync(
    db_path: str | Path,
    *,
    interval_days: float | None = None,
    keep_backups: int | None = None,
    service_factory: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Start a silent incremental sync in a daemon thread when one is due."""
    db = Path(db_path)
    base: dict[str, Any] = {"db": str(db)}
    if not auto_sync_enabled():
        return {**base, "status": "disabled"}
    if not db.is_file():
        return {**base, "status": "no_db"}
    if not credentials_configured():
        return {**base, "status": "no_credentials"}
    interval = resolve_interval_days(interval_days)
    factory = service_factory or ForumSyncService
    try:
        service = factory(db, keep_backups=keep_backups)
        watermark = service.watermark()
    except sqlite3.Error as exc:
        if isinstance(exc, sqlite3.OperationalError) and is_database_locked(exc):
            return {**base, **database_locked_status(db, reason=str(exc))}
        logger.warning("forum sync check failed: %s", sanitize_error(exc))
        return {**base, "status": "error", "reason": sanitize_error(exc)}
    if watermark is None:
        return {**base, "status": "needs_full"}
    due_at = watermark + timedelta(days=interval)
    if datetime.now(UTC) < due_at:
        return {**base, "status": "fresh", "due_at": _iso(due_at)}

    def run() -> None:
        try:
            outcome = service.sync(full=False)
        except Exception as exc:  # noqa: BLE001 - a daemon thread must not die loudly
            logger.warning("forum sync failed: %s", sanitize_error(exc))
            return
        logger.info("forum sync finished: %s", outcome.get("status"))

    thread = threading.Thread(name="forum-rag-sync", daemon=True, target=run)
    thread.start()
    return {**base, "status": "started", "watermark": _iso(watermark)}
