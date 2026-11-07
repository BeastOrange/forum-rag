from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from wq_forum_rag import forum_sync
from wq_forum_rag.cli import app
from wq_forum_rag.forum_sync import (
    ARTICLES_PATH,
    CATEGORIES_PATH,
    POSTS_PATH,
    SECTIONS_PATH,
    TOPICS_PATH,
    ForumSyncService,
    maybe_start_background_sync,
    topic_from_api_post,
)
from wq_forum_rag.indexer import ForumIndexService
from wq_forum_rag.knowledge import KnowledgeStore
from wq_forum_rag.storage import ForumStore
from wq_forum_rag.support_client import SupportAuthError, SupportNetworkError

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(days=4)


def _ts(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def api_post(
    pid: int,
    *,
    title: str,
    body: str,
    updated: datetime = NOW,
    comment_count: int = 0,
    topic_id: int = 100,
) -> dict:
    return {
        "id": pid,
        "topic_id": topic_id,
        "title": title,
        "details": f"<p>{body}</p>",
        "html_url": f"https://support.example/posts/{pid}",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": _ts(updated),
        "author_id": 7,
        "vote_sum": 2,
        "comment_count": comment_count,
    }


def api_comment(cid: int, body: str, at: str = "2026-01-02T00:00:00Z") -> dict:
    return {
        "id": cid,
        "author_id": 9,
        "body": f"<p>{body}</p>",
        "created_at": at,
        "updated_at": at,
        "vote_sum": 1,
    }


def api_article(aid: int, *, title: str, body: str, updated: datetime = NOW) -> dict:
    return {
        "id": aid,
        "title": title,
        "body": f"<p>{body}</p>",
        "html_url": f"https://support.example/articles/{aid}",
        "created_at": "2025-06-01T00:00:00Z",
        "updated_at": _ts(updated),
        "author_id": 3,
        "vote_sum": 5,
        "comment_count": 0,
        "section_id": 500,
    }


def community_path(cid: int | str) -> str:
    return f"/api/v2/community/topics/{cid}/posts.json"


def comments_path(pid: int | str) -> str:
    return f"/api/v2/community/posts/{pid}/comments.json"


def make_routes(
    *,
    community_pages: dict[int, list[list[dict]]] | None = None,
    recent_pages: list[list[dict]] | None = None,
    comments: dict[int, list[dict]] | None = None,
    article_pages: list[list[dict]] | None = None,
) -> dict[str, list[dict]]:
    community_pages = community_pages or {}
    routes: dict[str, list[dict]] = {
        TOPICS_PATH: [
            {
                "topics": [
                    {"id": cid, "name": f"Community {cid}"} for cid in community_pages
                ]
            }
        ],
        CATEGORIES_PATH: [{"categories": [{"id": 400, "name": "Getting Started"}]}],
        SECTIONS_PATH: [{"sections": [{"id": 500, "category_id": 400}]}],
        ARTICLES_PATH: [{"articles": page} for page in (article_pages or [[]])],
    }
    for cid, pages in community_pages.items():
        routes[community_path(cid)] = [{"posts": page} for page in pages]
    if recent_pages is not None:
        routes[POSTS_PATH] = [{"posts": page} for page in recent_pages]
    for pid, items in (comments or {}).items():
        routes[comments_path(pid)] = [{"comments": items}]
    return routes


class FakeSupportClient:
    def __init__(
        self,
        routes: dict[str, list[dict]],
        *,
        failures: dict[str, Exception] | None = None,
        login_error: Exception | None = None,
    ) -> None:
        self.routes = routes
        self.failures = failures or {}
        self.login_error = login_error
        self.calls: Counter[str] = Counter()
        self.params: dict[str, dict | None] = {}
        self.logins = 0
        self.last_iteration_truncated = False

    def login(self) -> dict:
        self.logins += 1
        if self.login_error is not None:
            raise self.login_error
        return {"login_status": 201, "hops": 3, "role": "end-user"}

    def get_json(self, path: str, params: dict | None = None) -> dict:
        self.calls[path] += 1
        return self.routes[path][0]

    def iter_pages(self, path: str, params: dict | None = None, *, max_pages=None):
        self.last_iteration_truncated = False
        self.params[path] = params
        if path in self.failures:
            self.calls[path] += 1
            raise self.failures[path]
        if path not in self.routes:
            raise AssertionError(f"unexpected request to {path}")
        pages = self.routes[path]
        for number, page in enumerate(pages, start=1):
            self.calls[path] += 1
            yield page
            if number == len(pages):
                return
            if max_pages is not None and number >= max_pages:
                self.last_iteration_truncated = True
                return

    def map_concurrent(self, fn, items):
        results = []
        for item in items:
            try:
                results.append((item, fn(item)))
            except Exception as exc:  # noqa: BLE001 - mirrors SupportClient
                results.append((item, exc))
        return results


def make_service(
    db: Path, client: FakeSupportClient, *, now: datetime = NOW, **kwargs
) -> ForumSyncService:
    return ForumSyncService(
        db, client_factory=lambda: client, now=lambda: now, **kwargs
    )


def full_build(db: Path, *, now: datetime = NOW, extra_posts=()) -> dict:
    client = FakeSupportClient(
        make_routes(
            community_pages={
                100: [
                    [
                        api_post(
                            101,
                            title="Alpha decay",
                            body="Use neutralization on alpha decay",
                            comment_count=1,
                        ),
                        api_post(102, title="Turnover notes", body="Plain turnover"),
                        *extra_posts,
                    ]
                ]
            },
            comments={101: [api_comment(1001, "Subindustry grouping helps")]},
            article_pages=[
                [api_article(201, title="Welcome", body="Onboarding checklist guide")]
            ],
        )
    )
    return make_service(db, client, now=now).sync(full=True)


def topic_ids(db: Path) -> set[str]:
    with sqlite3.connect(db) as conn:
        return {row[0] for row in conn.execute("SELECT topic_id FROM topics")}


def meta(db: Path) -> dict[str, str]:
    with sqlite3.connect(db) as conn:
        return dict(conn.execute("SELECT key, value FROM schema_meta").fetchall())


def search_ids(db: Path, query: str) -> list[str]:
    return [
        hit["topic_id"]
        for hit in ForumIndexService(db, auto_refresh=False).search(query, top_k=5)
    ]


def test_full_build_on_missing_db_creates_searchable_index(tmp_path: Path) -> None:
    db = tmp_path / "forum.sqlite3"

    result = full_build(db)

    assert result["status"] == "synced"
    assert result["posts_seen"] == 2
    assert result["posts_upserted"] == 2
    assert result["articles_seen"] == 1
    assert result["comment_fetches"] == 1
    assert result["backup"] is None
    assert "101" in search_ids(db, "neutralization")
    assert "201" in search_ids(db, "onboarding")
    article = ForumIndexService(db, auto_refresh=False).get_post("201")
    assert article["community_id"] == "400"
    assert article["community_title"] == "Help Center / Getting Started"
    post = ForumIndexService(db, auto_refresh=False).get_post("101")
    assert post["community_title"] == "Community 100"
    assert [c["author"] for c in post["comments"]] == ["9"]
    stored = meta(db)
    assert stored["sync_last_success_at"] == "2026-11-07T12:00:00Z"
    assert stored["sync_last_full_at"] == "2026-11-07T12:00:00Z"
    assert stored["sync_last_status"] == "synced"
    assert json.loads(stored["sync_last_stats"])["posts_seen"] == 2
    assert ForumSyncService(db).watermark() == NOW
    assert set(os.listdir(tmp_path)) <= {"forum.sqlite3", "backups"}


def test_incremental_stops_at_cutoff_reuses_comments_and_keeps_absent(
    tmp_path: Path,
) -> None:
    db = tmp_path / "forum.sqlite3"
    full_build(db, extra_posts=[api_post(109, title="Kept", body="Absent topic")])
    old = NOW - timedelta(hours=3)
    client = FakeSupportClient(
        make_routes(
            recent_pages=[
                [
                    api_post(
                        101,
                        title="Alpha decay revised",
                        body="Now about crowding factors",
                        updated=LATER,
                        comment_count=1,
                    ),
                    api_post(
                        102,
                        title="Turnover notes",
                        body="Plain turnover",
                        updated=LATER,
                        comment_count=1,
                    ),
                ],
                [api_post(103, title="Old", body="Old post", updated=old)],
                [api_post(104, title="Never", body="Never requested")],
            ],
            comments={102: [api_comment(1002, "First reply on turnover")]},
            article_pages=[[api_article(201, title="Welcome", body="x", updated=old)]],
        )
    )

    result = make_service(db, client, now=LATER).sync()

    assert result["status"] == "synced"
    assert client.calls[POSTS_PATH] == 2
    assert client.calls[comments_path(101)] == 0
    assert client.calls[comments_path(102)] == 1
    assert result["comments_reused"] == 1
    assert result["posts_seen"] == 2
    assert result["articles_seen"] == 0
    assert result["pruned"] == 0
    assert client.params[POSTS_PATH]["sort_by"] == "updated_at"
    assert client.params[ARTICLES_PATH]["sort_by"] == "updated_at"
    assert topic_ids(db) == {"101", "102", "109", "201"}
    post = ForumIndexService(db, auto_refresh=False).get_post("101")
    assert post["title"] == "Alpha decay revised"
    assert len(post["comments"]) == 1
    assert "101" in search_ids(db, "crowding")
    assert "101" not in search_ids(db, "neutralization")
    assert "102" in search_ids(db, "reply")
    assert ForumSyncService(db).watermark() == LATER
    assert meta(db)["sync_last_full_at"] == "2026-11-07T12:00:00Z"


def test_full_sync_prunes_orphans_but_keeps_knowledge_sources(tmp_path: Path) -> None:
    db = tmp_path / "forum.sqlite3"
    full_build(db, extra_posts=[api_post(109, title="Cited", body="Cited evidence")])
    with KnowledgeStore(db) as kstore, kstore.conn:
        kstore.conn.execute(
            """
            INSERT INTO knowledge_pages(slug, title, summary, body, status, confidence,
                content_hash, created_at, updated_at)
            VALUES ('alpha-decay', 'Alpha Decay', 'summary', 'body', 'published', 0.9,
                'hash', '2026-04-28T13:00:00Z', '2026-04-28T13:00:00Z')
            """
        )
        kstore.conn.execute(
            "INSERT INTO knowledge_sources(slug, topic_id, evidence_note) "
            "VALUES ('alpha-decay', '109', 'cited')"
        )
    client = FakeSupportClient(
        make_routes(
            community_pages={
                100: [[api_post(101, title="Alpha decay", body="neutralization")]]
            },
            article_pages=[[api_article(201, title="Welcome", body="Onboarding")]],
        )
    )

    result = make_service(db, client, now=LATER).sync(full=True)

    assert result["status"] == "synced"
    assert result["pruned"] == 1
    assert result["protected_orphans"] == ["109"]
    assert topic_ids(db) == {"101", "109", "201"}
    assert search_ids(db, "turnover") == []


def test_watermark_derived_from_export_refresh(tmp_path: Path) -> None:
    json_path = tmp_path / "WQP_CommunityState_20261027_184817.json"
    db = tmp_path / "forum.sqlite3"
    json_path.write_text(
        json.dumps(
            {
                "byCommunity": {
                    "c1": {
                        "title": "Alpha Lab",
                        "topics": {
                            "t1": {
                                "id": "t1",
                                "title": "Alpha decay",
                                "postContent": "<p>notes</p>",
                            }
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    stamp = datetime(2026, 10, 27, 10, 48, 19, tzinfo=UTC).timestamp()
    os.utime(json_path, (stamp, stamp))

    result = CliRunner().invoke(app, ["refresh", str(json_path), "--db", str(db)])

    assert result.exit_code == 0, result.output
    service = ForumSyncService(db, now=lambda: NOW)
    assert service.watermark() == datetime.fromtimestamp(stamp, UTC)
    status = service.status()
    assert status["watermark_source"] == "export"
    assert status["needs_full"] is False
    assert status["stale"] is True
    assert status["due_at"] == "2026-10-30T10:48:19Z"


def test_auto_refresh_skips_exports_older_than_the_last_sync(
    tmp_path: Path, monkeypatch
) -> None:
    db = tmp_path / "forum.sqlite3"
    assert full_build(db)["status"] == "synced"
    exports = tmp_path / "exports"
    exports.mkdir()
    json_path = exports / "WQP_CommunityState_20261101_000000.json"
    json_path.write_text(
        json.dumps(
            {
                "byCommunity": {
                    "100": {
                        "title": "Community 100",
                        "topics": {
                            "101": {
                                "id": "101",
                                "title": "Alpha decay",
                                "postContent": "<p>stale export text</p>",
                            }
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    older = (NOW - timedelta(days=1)).timestamp()
    os.utime(json_path, (older, older))
    monkeypatch.delenv("WQ_FORUM_RAG_SOURCE", raising=False)
    monkeypatch.setenv("WQ_FORUM_RAG_SOURCE_DIR", str(exports))
    service = ForumIndexService(db)

    assert service.auto_refresh_if_needed()["status"] == "superseded_by_sync"
    assert [hit["topic_id"] for hit in service.search("neutralization")] == ["101"]
    assert service.search("stale export") == []
    assert topic_ids(db) == {"101", "102", "201"}

    newer = (NOW + timedelta(days=1)).timestamp()
    os.utime(json_path, (newer, newer))
    assert service.auto_refresh_if_needed()["status"] == "refreshed"


def test_needs_full_without_watermark_makes_no_client_calls(tmp_path: Path) -> None:
    db = tmp_path / "forum.sqlite3"
    calls: list[str] = []

    def factory():
        calls.append("built")
        raise AssertionError("client must not be built")

    missing = ForumSyncService(db, client_factory=factory).sync()
    assert missing["status"] == "needs_full"
    assert not db.exists()

    ForumStore(db).close()
    result = ForumSyncService(db, client_factory=factory).sync()

    assert result["status"] == "needs_full"
    assert calls == []
    assert not (tmp_path / "forum.sqlite3.sync.lock").exists()
    status = ForumSyncService(db).status()
    assert status["needs_full"] is True
    assert status["watermark"] is None


def test_max_pages_truncation_is_partial_and_keeps_watermark(tmp_path: Path) -> None:
    db = tmp_path / "forum.sqlite3"
    full_build(db)
    client = FakeSupportClient(
        make_routes(
            recent_pages=[
                [api_post(105, title="New one", body="fresh text", updated=LATER)],
                [api_post(106, title="New two", body="more text", updated=LATER)],
            ],
        )
    )

    result = make_service(db, client, now=LATER).sync(max_pages=1)

    assert result["status"] == "partial"
    assert "max-pages" in result["reason"]
    assert client.calls[POSTS_PATH] == 1
    assert "105" in topic_ids(db)
    assert ForumSyncService(db).watermark() == NOW
    assert meta(db)["sync_last_status"] == "partial"


def test_bounded_full_sync_never_prunes(tmp_path: Path) -> None:
    db = tmp_path / "forum.sqlite3"
    full_build(db)
    client = FakeSupportClient(
        make_routes(
            community_pages={
                100: [
                    [api_post(101, title="Alpha decay", body="neutralization")],
                    [api_post(107, title="Second page", body="x")],
                ]
            },
            article_pages=[[api_article(201, title="Welcome", body="Onboarding")]],
        )
    )

    result = make_service(db, client, now=LATER).sync(full=True, max_pages=1)

    assert result["status"] == "partial"
    assert result["pruned"] == 0
    assert "102" in topic_ids(db)
    assert meta(db)["sync_last_full_at"] == "2026-11-07T12:00:00Z"


def test_failed_comment_fetch_is_partial_without_prune(tmp_path: Path) -> None:
    db = tmp_path / "forum.sqlite3"
    full_build(db)
    client = FakeSupportClient(
        make_routes(
            community_pages={
                100: [
                    [
                        api_post(
                            101,
                            title="Alpha decay",
                            body="changed body text",
                            comment_count=2,
                        )
                    ]
                ]
            },
            article_pages=[[api_article(201, title="Welcome", body="Onboarding")]],
        ),
        failures={comments_path(101): SupportNetworkError("Network error")},
    )

    result = make_service(db, client, now=LATER).sync(full=True)

    assert result["status"] == "partial"
    assert result["failed_items"] == 1
    assert result["pruned"] == 0
    assert "102" in topic_ids(db)
    post = ForumIndexService(db, auto_refresh=False).get_post("101")
    assert "changed" not in post["body_text"]
    assert ForumSyncService(db).watermark() == NOW


def test_blocked_comment_fetch_stops_the_run(tmp_path: Path) -> None:
    db = tmp_path / "forum.sqlite3"
    full_build(db)
    client = FakeSupportClient(
        make_routes(
            community_pages={
                100: [[api_post(101, title="Alpha", body="x", comment_count=2)]]
            },
        ),
        failures={comments_path(101): SupportAuthError("session rejected")},
    )

    result = make_service(db, client, now=LATER).sync(full=True)

    assert result["status"] == "auth_failed"
    assert ARTICLES_PATH not in client.calls
    stored = meta(db)
    assert stored["sync_last_status"] == "auth_failed"
    assert stored["sync_last_success_at"] == "2026-11-07T12:00:00Z"


def test_login_failure_maps_status_before_any_write(tmp_path: Path) -> None:
    db = tmp_path / "forum.sqlite3"
    client = FakeSupportClient({}, login_error=SupportAuthError("BRAIN login failed"))

    result = make_service(db, client).sync(full=True)

    assert result["status"] == "auth_failed"
    assert not db.exists()
    assert sorted(os.listdir(tmp_path)) == []


def _dummy_backups(db: Path, count: int) -> list[Path]:
    backups = db.parent / "backups"
    backups.mkdir(exist_ok=True)
    paths = []
    for index in range(count):
        path = backups / f"forum-before-manual-{index}.sqlite3"
        path.write_bytes(b"old")
        stamp = 1_700_000_000 + index * 100
        os.utime(path, (stamp, stamp))
        paths.append(path)
    (backups / "other-before-keep.sqlite3").write_bytes(b"unrelated")
    return paths


@pytest.mark.parametrize(
    ("keep", "expected_dummies"),
    [(1, []), (0, []), (2, ["forum-before-manual-2.sqlite3"])],
)
def test_backup_retention(
    tmp_path: Path, keep: int, expected_dummies: list[str]
) -> None:
    db = tmp_path / "forum.sqlite3"
    full_build(db)
    _dummy_backups(db, 3)
    client = FakeSupportClient(
        make_routes(recent_pages=[[]], article_pages=[[]]),
    )

    result = make_service(db, client, now=LATER, keep_backups=keep).sync()

    assert result["status"] == "synced"
    backups = db.parent / "backups"
    names = sorted(path.name for path in backups.glob("forum-before-*.sqlite3"))
    if keep == 0:
        assert result["backup"] is None
        assert names == []
    else:
        new_name = "forum-before-sync-20261111-120000.sqlite3"
        assert result["backup"] == str(backups / new_name)
        assert names == sorted([*expected_dummies, new_name])
        with sqlite3.connect(backups / new_name) as conn:
            assert conn.execute("SELECT COUNT(*) FROM topics").fetchone()[0] == 3
    assert len(result["backups_removed"]) == 3 - len(expected_dummies)
    assert (backups / "other-before-keep.sqlite3").exists()
    assert list(backups.glob("*.partial")) == []
    status = ForumSyncService(db, keep_backups=keep).status()
    assert len(status["backups"]) == len(names)


def test_held_lock_returns_already_running(tmp_path: Path) -> None:
    db = tmp_path / "forum.sqlite3"
    lock = tmp_path / "forum.sqlite3.sync.lock"
    lock.write_text(json.dumps({"pid": os.getpid(), "started_at": _ts(NOW)}))
    client = FakeSupportClient({})

    result = make_service(db, client).sync(full=True)

    assert result["status"] == "already_running"
    assert client.logins == 0
    assert lock.exists()


def test_stale_lock_is_replaced(tmp_path: Path) -> None:
    db = tmp_path / "forum.sqlite3"
    lock = tmp_path / "forum.sqlite3.sync.lock"
    stale = NOW - timedelta(hours=13)
    lock.write_text(json.dumps({"pid": os.getpid(), "started_at": _ts(stale)}))

    result = full_build(db)

    assert result["status"] == "synced"
    assert not lock.exists()


def test_synced_post_matches_exported_hash(tmp_path: Path) -> None:
    json_path = tmp_path / "WQP_CommunityState_20261027_184817.json"
    db = tmp_path / "forum.sqlite3"
    exported_comments = {
        "1001": {
            "id": "1001",
            "author": "12429216262167",
            "commentContent": "<p>Try a <b>longer</b> decay window.</p>",
            "commentTimeDatetime": "2026-03-13T03:25:05Z",
            "voteNum": 0,
        },
        "1002": {
            "id": "1002",
            "author": "555",
            "commentContent": "<p>Agreed.</p>",
            "commentTimeDatetime": "2026-03-14T00:00:00Z",
            "voteNum": 2,
        },
    }
    json_path.write_text(
        json.dumps(
            {
                "byCommunity": {
                    "100": {
                        "title": "Community 100",
                        "topics": {
                            "101": {
                                "id": "101",
                                "title": "Alpha decay",
                                "postContent": "<p>Decay <i>matters</i>.</p>",
                                "url": "https://support.example/posts/101",
                                "datetime": "2026-03-07T10:45:26Z",
                                "author": "7",
                                "voteNum": 3,
                                "commentNum": 2,
                                "comments": exported_comments,
                            }
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    assert (
        CliRunner().invoke(app, ["refresh", str(json_path), "--db", str(db)]).exit_code
        == 0
    )
    post = api_post(101, title="Alpha decay", body="Decay <i>matters</i>.")
    post["comment_count"] = 2
    api_comments = [
        {
            "id": 1001,
            "author_id": 12429216262167,
            "body": "<p>Try a <b>longer</b> decay window.</p>",
            "created_at": "2026-03-07T10:45:26Z",
            "updated_at": "2026-03-13T03:25:05Z",
            "vote_sum": 0,
        },
        {
            "id": 1002,
            "author_id": 555,
            "body": "<p>Agreed.</p>",
            "created_at": "2026-03-14T00:00:00Z",
            "updated_at": None,
            "vote_sum": 2,
        },
    ]
    with ForumStore(db) as store:
        exported = store.get_topic("101")
    assert exported is not None
    assert (
        topic_from_api_post(post, "Community 100", comments=api_comments).content_hash
        == exported.content_hash
    )

    client = FakeSupportClient(
        make_routes(community_pages={100: [[post]]}, article_pages=[[]])
    )
    result = make_service(db, client).sync(full=True)

    assert result["status"] == "synced"
    assert result["posts_skipped"] == 1
    assert result["posts_upserted"] == 0
    assert result["comment_fetches"] == 0


def test_settings_helpers(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    assert forum_sync.auto_sync_enabled() is False
    for value in ("1", "yes", "on", "true"):
        monkeypatch.setenv(forum_sync.AUTO_SYNC_ENV, value)
        assert forum_sync.auto_sync_enabled() is True
    monkeypatch.delenv(forum_sync.AUTO_SYNC_ENV)
    assert forum_sync.auto_sync_enabled() is True

    assert forum_sync.resolve_interval_days() == 3.0
    assert forum_sync.resolve_interval_days(0.5) == 0.5
    monkeypatch.setenv(forum_sync.INTERVAL_ENV, "7")
    assert forum_sync.resolve_interval_days() == 7.0
    caplog.set_level(logging.WARNING, logger="wq_forum_rag.sync")
    for bad in ("soon", "-1"):
        monkeypatch.setenv(forum_sync.INTERVAL_ENV, bad)
        assert forum_sync.resolve_interval_days() == 3.0
    assert forum_sync.INTERVAL_ENV in caplog.text

    assert forum_sync.resolve_keep_backups() == 1
    assert forum_sync.resolve_keep_backups(0) == 0
    monkeypatch.setenv(forum_sync.KEEP_BACKUPS_ENV, "4")
    assert forum_sync.resolve_keep_backups() == 4
    monkeypatch.setenv(forum_sync.KEEP_BACKUPS_ENV, "many")
    assert forum_sync.resolve_keep_backups() == 1


def test_sanitize_error_drops_query_strings() -> None:
    message = "failed at https://support.worldquantbrain.com/access/jwt?jwt=SECRET x"
    cleaned = forum_sync.sanitize_error(message)
    assert "SECRET" not in cleaned
    assert "jwt=" not in cleaned
    assert "https://support.worldquantbrain.com/access/jwt" in cleaned
    assert len(forum_sync.sanitize_error("x" * 1000)) == 300


class FakeService:
    def __init__(self, watermark: datetime | None, *, error: bool = False) -> None:
        self._watermark = watermark
        self.error = error
        self.calls: list[bool] = []
        self.done = threading.Event()

    def watermark(self) -> datetime | None:
        return self._watermark

    def sync(self, *, full: bool = False) -> dict:
        self.calls.append(full)
        self.done.set()
        if self.error:
            raise RuntimeError("boom at https://x.example/a?jwt=SECRET")
        return {"status": "synced"}


def _join_sync_threads() -> None:
    for thread in threading.enumerate():
        if thread.name == "forum-rag-sync":
            thread.join(timeout=5)


def test_maybe_start_background_sync_status_matrix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    db = tmp_path / "forum.sqlite3"
    now = datetime.now(UTC)

    def factory_for(service: FakeService):
        def factory(path, *, keep_backups=None):
            assert Path(path) == db
            return service

        return factory

    untouched = factory_for(FakeService(None))
    assert maybe_start_background_sync(db, service_factory=untouched)["status"] == (
        "disabled"
    )
    monkeypatch.setenv(forum_sync.AUTO_SYNC_ENV, "1")
    assert maybe_start_background_sync(db, service_factory=untouched)["status"] == (
        "no_db"
    )
    ForumStore(db).close()
    assert maybe_start_background_sync(db, service_factory=untouched)["status"] == (
        "no_credentials"
    )
    monkeypatch.setenv("WQ_BRAIN_USERNAME", "user@example.com")
    monkeypatch.setenv("WQ_BRAIN_PASSWORD", "not-a-real-password")
    assert maybe_start_background_sync(db, service_factory=untouched)["status"] == (
        "needs_full"
    )

    recent = FakeService(now - timedelta(days=1))
    fresh = maybe_start_background_sync(db, service_factory=factory_for(recent))
    assert fresh["status"] == "fresh"
    assert recent.calls == []

    caplog.set_level(logging.INFO, logger="wq_forum_rag.sync")
    short = FakeService(now - timedelta(days=1))
    started = maybe_start_background_sync(
        db, interval_days=0.5, service_factory=factory_for(short)
    )
    assert started["status"] == "started"
    assert short.done.wait(5)
    _join_sync_threads()
    assert short.calls == [False]

    old = FakeService(now - timedelta(days=4))
    assert (
        maybe_start_background_sync(db, service_factory=factory_for(old))["status"]
        == "started"
    )
    assert old.done.wait(5)
    _join_sync_threads()
    assert "forum sync finished: synced" in caplog.text

    failing = FakeService(now - timedelta(days=4), error=True)
    assert (
        maybe_start_background_sync(db, service_factory=factory_for(failing))["status"]
        == "started"
    )
    assert failing.done.wait(5)
    _join_sync_threads()
    assert "forum sync failed" in caplog.text
    assert "SECRET" not in caplog.text
