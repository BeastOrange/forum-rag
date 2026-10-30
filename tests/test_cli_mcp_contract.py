from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from typer.testing import CliRunner

from wq_forum_rag.cli import app
from wq_forum_rag.indexer import ForumIndexService
from wq_forum_rag.knowledge import KnowledgeStore
from wq_forum_rag.mcp_server import (
    find_by_exact,
    get_doc,
    get_knowledge_page,
    get_post,
    graph_query,
    link_knowledge_pages,
    lint_knowledge,
    propose_knowledge_page,
    publish_knowledge_page,
    rebuild_search_index,
    related_posts,
    search_docs,
    search_forum,
    search_knowledge,
    source_ingest_plan,
    source_status,
)


def _write_fixture(json_path: Path) -> None:
    payload = {
        "byCommunity": {
            "c1": {
                "id": "c1",
                "title": "Alpha Lab",
                "followers": 12,
                "posts": 2,
                "url": "https://example.com/community/c1",
                "topics": {
                    "t1": {
                        "id": "t1",
                        "author": "alice",
                        "commentNum": 3,
                        "datetime": "2026-04-28T13:00:00Z",
                        "postContent": "<p>Alpha decay and neutralization notes.</p>",
                        "title": "Alpha decay",
                        "url": "https://example.com/t1",
                        "voteNum": 10,
                    },
                    "t2": {
                        "id": "t2",
                        "author": "bob",
                        "commentNum": 1,
                        "comments": {
                            "c2": {
                                "id": "c2",
                                "author": "carol",
                                "commentContent": "<p>Use subindustry neutralization for this setup.</p>",
                                "commentTimeDatetime": "2026-04-28T13:20:00Z",
                                "voteNum": 1,
                            }
                        },
                        "datetime": "2026-04-28T13:10:00Z",
                        "postContent": "<p>Portfolio neutralization checklist.</p>",
                        "title": "Neutralization checklist",
                        "url": "https://example.com/t2",
                        "voteNum": 8,
                    },
                },
            }
        }
    }
    json_path.write_text(json.dumps(payload), encoding="utf-8")


def _write_fixture_only_t1(json_path: Path) -> None:
    payload = {
        "byCommunity": {
            "c1": {
                "id": "c1",
                "title": "Alpha Lab",
                "followers": 12,
                "posts": 1,
                "url": "https://example.com/community/c1",
                "topics": {
                    "t1": {
                        "id": "t1",
                        "author": "alice",
                        "commentNum": 3,
                        "datetime": "2026-04-28T13:00:00Z",
                        "postContent": "<p>Alpha decay and neutralization notes.</p>",
                        "title": "Alpha decay",
                        "url": "https://example.com/t1",
                        "voteNum": 10,
                    },
                },
            }
        }
    }
    json_path.write_text(json.dumps(payload), encoding="utf-8")


def _write_fixture_with_t3(json_path: Path) -> None:
    payload = {
        "byCommunity": {
            "c1": {
                "id": "c1",
                "title": "Alpha Lab",
                "followers": 12,
                "posts": 3,
                "url": "https://example.com/community/c1",
                "topics": {
                    "t1": {
                        "id": "t1",
                        "author": "alice",
                        "commentNum": 3,
                        "datetime": "2026-04-28T13:00:00Z",
                        "postContent": "<p>Alpha decay and neutralization notes.</p>",
                        "title": "Alpha decay",
                        "url": "https://example.com/t1",
                        "voteNum": 10,
                    },
                    "t2": {
                        "id": "t2",
                        "author": "bob",
                        "commentNum": 1,
                        "datetime": "2026-04-28T13:10:00Z",
                        "postContent": "<p>Portfolio neutralization checklist.</p>",
                        "title": "Neutralization checklist",
                        "url": "https://example.com/t2",
                        "voteNum": 8,
                    },
                    "t3": {
                        "id": "t3",
                        "author": "dave",
                        "commentNum": 0,
                        "datetime": "2026-04-28T13:30:00Z",
                        "postContent": "<p>New alpha turnover ideas with decay tuning.</p>",
                        "title": "Turnover ideas",
                        "url": "https://example.com/t3",
                        "voteNum": 6,
                    },
                },
            }
        }
    }
    json_path.write_text(json.dumps(payload), encoding="utf-8")


def test_cli_contract_end_to_end(tmp_path: Path) -> None:
    json_path = tmp_path / "forum.json"
    db_path = tmp_path / "forum.sqlite3"
    _write_fixture(json_path)
    runner = CliRunner()

    index_result = runner.invoke(
        app, ["index", "--json", str(json_path), "--db", str(db_path), "--rebuild"]
    )
    assert index_result.exit_code == 0
    assert '"indexed_topics": 2' in index_result.stdout

    unchanged_result = runner.invoke(
        app, ["index", "--json", str(json_path), "--db", str(db_path)]
    )
    assert unchanged_result.exit_code == 0
    assert '"status": "unchanged"' in unchanged_result.stdout

    search_result = runner.invoke(
        app, ["search", "neutralization", "--db", str(db_path), "--top-k", "2"]
    )
    assert search_result.exit_code == 0
    assert "Neutralization checklist" in search_result.stdout

    reindex_result = runner.invoke(app, ["search-reindex", "--db", str(db_path)])
    assert reindex_result.exit_code == 0
    assert '"fts_available": true' in reindex_result.stdout

    show_result = runner.invoke(app, ["show", "t1", "--db", str(db_path)])
    assert show_result.exit_code == 0
    assert '"topic_id": "t1"' in show_result.stdout


def test_mcp_tool_contract_without_client(tmp_path: Path) -> None:
    json_path = tmp_path / "forum.json"
    db_path = tmp_path / "forum.sqlite3"
    _write_fixture(json_path)
    runner = CliRunner()
    runner.invoke(
        app, ["index", "--json", str(json_path), "--db", str(db_path), "--rebuild"]
    )

    search_payload = search_forum("alpha", db=str(db_path), top_k=3)
    assert search_payload["query"] == "alpha"
    assert search_payload["results"][0]["topic_id"] == "t1"

    post_payload = get_post("t1", db=str(db_path))
    assert post_payload["post"]["title"] == "Alpha decay"

    exact_payload = find_by_exact("subindustry neutralization", db=str(db_path))
    assert exact_payload["results"][0]["topic_id"] == "t2"

    community_payload = find_by_exact(
        "neutralization",
        db=str(db_path),
        community="Alpha Lab",
        top_k=2,
    )
    assert {item["topic_id"] for item in community_payload["results"]} <= {"t1", "t2"}

    related_payload = related_posts("t1", db=str(db_path), top_k=2)
    assert related_payload["topic_id"] == "t1"
    assert len(related_payload["results"]) <= 2

    reindex_payload = rebuild_search_index(db=str(db_path))
    assert reindex_payload["fts_available"] is True


def test_mcp_read_only_tools_tolerate_existing_empty_database(tmp_path: Path) -> None:
    db_path = tmp_path / "empty.sqlite3"
    db_path.touch()

    assert search_forum("alpha", db=str(db_path))["results"] == []
    assert get_post("t1", db=str(db_path))["post"] is None
    assert find_by_exact("alpha", db=str(db_path))["results"] == []
    assert related_posts("t1", db=str(db_path))["results"] == []
    assert search_docs("alpha", db=str(db_path))["results"] == []
    assert get_doc("alpha", db=str(db_path))["document"] is None
    assert search_knowledge("alpha", db=str(db_path))["results"] == []
    assert get_knowledge_page("alpha/x", db=str(db_path))["page"] is None
    assert lint_knowledge(db=str(db_path))["blocking_count"] == 0
    assert graph_query("alpha/x", db=str(db_path))["nodes"] == [
        {"slug": "alpha/x", "missing": True}
    ]


def test_mcp_write_tools_tolerate_existing_empty_database(tmp_path: Path) -> None:
    db_path = tmp_path / "empty.sqlite3"
    db_path.touch()

    try:
        propose_knowledge_page(
            slug="alpha/test",
            title="Alpha",
            summary="Summary with enough length for a valid page.",
            body="Body with enough content for a valid page that still lacks sources.",
            source_topic_ids=["t1"],
            confidence=0.9,
            db=str(db_path),
        )
    except ValueError as exc:
        assert "source_topic_ids must reference existing forum topics" in str(exc)
    else:
        raise AssertionError(
            "propose_knowledge_page should reject missing forum topics"
        )

    publish_payload = publish_knowledge_page("alpha/missing", db=str(db_path))
    assert publish_payload["published"] is False
    assert publish_payload["issues"] == [
        {"slug": "alpha/missing", "severity": "block", "code": "missing_page"}
    ]

    try:
        link_knowledge_pages("alpha/a", "alpha/b", "refines", db=str(db_path))
    except ValueError as exc:
        assert (
            "both source_slug and target_slug must reference existing knowledge pages"
            in str(exc)
        )
    else:
        raise AssertionError(
            "link_knowledge_pages should reject missing knowledge pages"
        )


def test_cli_read_only_tools_tolerate_existing_empty_database(tmp_path: Path) -> None:
    db_path = tmp_path / "empty.sqlite3"
    db_path.touch()
    runner = CliRunner()

    search_result = runner.invoke(app, ["search", "alpha", "--db", str(db_path)])
    show_result = runner.invoke(app, ["show", "t1", "--db", str(db_path)])
    context_result = runner.invoke(
        app, ["evolve-context", "alpha", "--db", str(db_path)]
    )
    knowledge_search_result = runner.invoke(
        app, ["knowledge-search", "alpha", "--db", str(db_path), "--json"]
    )

    assert search_result.exit_code == 0
    assert "Top 0" in search_result.stdout
    assert show_result.exit_code == 1
    assert context_result.exit_code == 0
    assert '"forum_evidence": []' in context_result.stdout
    assert knowledge_search_result.exit_code == 0
    assert '"results": []' in knowledge_search_result.stdout


def test_search_auto_refreshes_when_source_json_changes(
    tmp_path: Path, monkeypatch
) -> None:
    json_path = tmp_path / "WQPCommunityState_20260618_142702.json"
    db_path = tmp_path / "forum.sqlite3"
    _write_fixture(json_path)
    runner = CliRunner()
    runner.invoke(app, ["refresh", str(json_path), "--db", str(db_path), "--rebuild"])

    _write_fixture_with_t3(json_path)
    monkeypatch.setenv("WQ_FORUM_RAG_SOURCE", str(json_path))

    search_payload = search_forum("turnover ideas", db=str(db_path), top_k=3)
    assert search_payload["results"][0]["topic_id"] == "t3"

    post_payload = get_post("t3", db=str(db_path))
    assert post_payload["post"]["title"] == "Turnover ideas"


def test_auto_refresh_returns_locked_status_when_write_lock_is_busy(
    tmp_path: Path, monkeypatch
) -> None:
    json_path = tmp_path / "WQPCommunityState_20260618_142702.json"
    db_path = tmp_path / "forum.sqlite3"
    _write_fixture(json_path)
    runner = CliRunner()
    runner.invoke(app, ["refresh", str(json_path), "--db", str(db_path), "--rebuild"])

    _write_fixture_with_t3(json_path)
    monkeypatch.setenv("WQ_FORUM_RAG_SOURCE", str(json_path))

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("BEGIN EXCLUSIVE")
        payload = ForumIndexService(db_path).auto_refresh_if_needed()
    finally:
        conn.rollback()
        conn.close()

    assert payload["status"] == "locked"
    assert payload["json"] == str(json_path)


def test_mcp_search_returns_locked_payload_when_db_is_busy(
    tmp_path: Path, monkeypatch
) -> None:
    json_path = tmp_path / "WQPCommunityState_20260618_142702.json"
    db_path = tmp_path / "forum.sqlite3"
    _write_fixture(json_path)
    runner = CliRunner()
    runner.invoke(app, ["refresh", str(json_path), "--db", str(db_path), "--rebuild"])

    _write_fixture_with_t3(json_path)
    monkeypatch.setenv("WQ_FORUM_RAG_SOURCE", str(json_path))

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("BEGIN EXCLUSIVE")
        payload = search_forum("turnover ideas", db=str(db_path), top_k=3)
    finally:
        conn.rollback()
        conn.close()

    assert payload["status"] == "locked"
    assert payload["results"] == []
    assert payload["json"] == str(json_path)


def test_mcp_get_post_returns_locked_payload_when_db_is_busy(
    tmp_path: Path, monkeypatch
) -> None:
    json_path = tmp_path / "WQPCommunityState_20260618_142702.json"
    db_path = tmp_path / "forum.sqlite3"
    _write_fixture(json_path)
    runner = CliRunner()
    runner.invoke(app, ["refresh", str(json_path), "--db", str(db_path), "--rebuild"])

    _write_fixture_with_t3(json_path)
    monkeypatch.setenv("WQ_FORUM_RAG_SOURCE", str(json_path))

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("BEGIN EXCLUSIVE")
        payload = get_post("t3", db=str(db_path))
    finally:
        conn.rollback()
        conn.close()

    assert payload["status"] == "locked"
    assert payload["post"] is None
    assert payload["json"] == str(json_path)


def test_mcp_knowledge_and_docs_return_locked_payload_when_db_is_busy(
    tmp_path: Path,
) -> None:
    json_path = tmp_path / "forum.json"
    db_path = tmp_path / "forum.sqlite3"
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "Alpha.md").write_text("# Alpha\n\ncontent", encoding="utf-8")
    _write_fixture(json_path)
    runner = CliRunner()
    runner.invoke(
        app, ["index", "--json", str(json_path), "--db", str(db_path), "--rebuild"]
    )
    runner.invoke(
        app, ["ingest-docs", str(docs_dir), "--db", str(db_path), "--rebuild"]
    )

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("BEGIN EXCLUSIVE")
        knowledge_payload = search_knowledge("alpha", db=str(db_path), top_k=3)
        docs_payload = search_docs("alpha", db=str(db_path), top_k=3)
        reindex_payload = rebuild_search_index(db=str(db_path))
    finally:
        conn.rollback()
        conn.close()

    assert knowledge_payload["status"] == "locked"
    assert knowledge_payload["results"] == []
    assert docs_payload["status"] == "locked"
    assert docs_payload["results"] == []
    assert reindex_payload["status"] == "locked"
    assert reindex_payload["fts_available"] is False


def test_cli_commands_report_locked_payload_when_db_is_busy(
    tmp_path: Path, monkeypatch
) -> None:
    json_path = tmp_path / "WQPCommunityState_20260618_142702.json"
    db_path = tmp_path / "forum.sqlite3"
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "Alpha.md").write_text("# Alpha\n\ncontent", encoding="utf-8")
    _write_fixture(json_path)
    runner = CliRunner()
    runner.invoke(app, ["refresh", str(json_path), "--db", str(db_path), "--rebuild"])
    runner.invoke(
        app, ["ingest-docs", str(docs_dir), "--db", str(db_path), "--rebuild"]
    )
    monkeypatch.setenv("WQ_FORUM_RAG_SOURCE", str(json_path))

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("BEGIN EXCLUSIVE")
        search_result = runner.invoke(app, ["search", "alpha", "--db", str(db_path)])
        docs_result = runner.invoke(app, ["search-docs", "alpha", "--db", str(db_path)])
        context_result = runner.invoke(
            app, ["evolve-context", "alpha", "--db", str(db_path)]
        )
    finally:
        conn.rollback()
        conn.close()

    assert search_result.exit_code == 0
    assert '"status": "locked"' in search_result.stdout
    assert docs_result.exit_code == 0
    assert '"status": "locked"' in docs_result.stdout
    assert context_result.exit_code == 0
    assert '"status": "locked"' in context_result.stdout


def test_search_can_read_during_immediate_write_transaction(
    tmp_path: Path, monkeypatch
) -> None:
    json_path = tmp_path / "forum.json"
    db_path = tmp_path / "forum.sqlite3"
    _write_fixture(json_path)
    runner = CliRunner()
    result = runner.invoke(
        app, ["index", "--json", str(json_path), "--db", str(db_path), "--rebuild"]
    )
    assert result.exit_code == 0
    monkeypatch.setenv("WQ_FORUM_RAG_AUTO_REFRESH", "0")

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        payload = search_forum("alpha", db=str(db_path), top_k=3)
    finally:
        conn.rollback()
        conn.close()

    assert "status" not in payload
    assert payload["results"]


def test_cli_search_can_read_during_immediate_write_transaction(
    tmp_path: Path, monkeypatch
) -> None:
    json_path = tmp_path / "forum.json"
    db_path = tmp_path / "forum.sqlite3"
    _write_fixture(json_path)
    runner = CliRunner()
    result = runner.invoke(
        app, ["index", "--json", str(json_path), "--db", str(db_path), "--rebuild"]
    )
    assert result.exit_code == 0
    monkeypatch.setenv("WQ_FORUM_RAG_AUTO_REFRESH", "0")

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        search_result = runner.invoke(app, ["search", "alpha", "--db", str(db_path)])
    finally:
        conn.rollback()
        conn.close()

    assert search_result.exit_code == 0
    assert '"status": "locked"' not in search_result.stdout
    assert "Alpha decay" in search_result.stdout


def test_knowledge_and_docs_can_read_during_immediate_write_transaction(
    tmp_path: Path, monkeypatch
) -> None:
    json_path = tmp_path / "forum.json"
    db_path = tmp_path / "forum.sqlite3"
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "Alpha.md").write_text("# Alpha\n\ncontent", encoding="utf-8")
    _write_fixture(json_path)
    runner = CliRunner()
    assert (
        runner.invoke(
            app, ["index", "--json", str(json_path), "--db", str(db_path), "--rebuild"]
        ).exit_code
        == 0
    )
    assert (
        runner.invoke(
            app, ["ingest-docs", str(docs_dir), "--db", str(db_path), "--rebuild"]
        ).exit_code
        == 0
    )
    from wq_forum_rag.evolution import EvolutionService

    service = EvolutionService(db_path)
    service.propose_knowledge_page(
        slug="alpha/guide",
        title="Alpha guide",
        summary="Summary with enough length for a published page.",
        body="Body with enough content for a published page linked to topic t1.",
        source_topic_ids=["t1"],
        confidence=0.9,
        source_evidence=[
            {
                "topic_id": "t1",
                "claim": "Decay is discussed with neutralization",
                "excerpt": "Alpha decay and neutralization notes.",
            }
        ],
    )
    service.review_knowledge_page(
        "alpha/guide", "reviewed", note="Checked the cited excerpt"
    )
    assert service.publish_knowledge_page("alpha/guide")["published"]
    monkeypatch.setenv("WQ_FORUM_RAG_AUTO_REFRESH", "0")

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        knowledge_payload = search_knowledge("alpha", db=str(db_path), top_k=3)
        docs_payload = search_docs("alpha", db=str(db_path), top_k=3)
    finally:
        conn.rollback()
        conn.close()

    assert "status" not in knowledge_payload
    assert "status" not in docs_payload
    assert knowledge_payload["results"]
    assert docs_payload["results"]


def test_read_only_endpoints_do_not_require_optional_schema_during_immediate_write(
    tmp_path: Path,
    monkeypatch,
) -> None:
    json_path = tmp_path / "forum.json"
    db_path = tmp_path / "forum.sqlite3"
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "alpha.md").write_text("# Alpha\n", encoding="utf-8")
    _write_fixture(json_path)
    runner = CliRunner()
    assert (
        runner.invoke(
            app, ["index", "--json", str(json_path), "--db", str(db_path), "--rebuild"]
        ).exit_code
        == 0
    )
    monkeypatch.setenv("WQ_FORUM_RAG_AUTO_REFRESH", "0")

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        doc_payload = get_doc("missing", db=str(db_path))
        page_payload = get_knowledge_page("missing", db=str(db_path))
        lint_payload = lint_knowledge(db=str(db_path))
        graph_payload = graph_query("missing", db=str(db_path), depth=1)
        status_payload = source_status(str(source_dir), db=str(db_path))
        plan_payload = source_ingest_plan(
            str(source_dir), db=str(db_path), commit=False
        )
    finally:
        conn.rollback()
        conn.close()

    assert doc_payload["document"] is None
    assert "status" not in doc_payload
    assert page_payload["page"] is None
    assert "status" not in page_payload
    assert lint_payload["issues"] == []
    assert lint_payload["blocking_count"] == 0
    assert "status" not in lint_payload
    assert graph_payload["nodes"] == [{"slug": "missing", "missing": True}]
    assert graph_payload["edges"] == []
    assert "status" not in graph_payload
    assert status_payload["counts"] == {
        "new": 1,
        "modified": 0,
        "unchanged": 0,
        "deleted": 0,
    }
    assert "status" not in status_payload
    assert plan_payload["ingest_plan"] == [
        {"relative_path": "alpha.md", "status": "new"}
    ]
    assert plan_payload["delete_plan"] == []
    assert "status" not in plan_payload


def test_cli_read_only_commands_do_not_require_optional_schema_during_immediate_write(
    tmp_path: Path,
    monkeypatch,
) -> None:
    json_path = tmp_path / "forum.json"
    db_path = tmp_path / "forum.sqlite3"
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "alpha.md").write_text("# Alpha\n", encoding="utf-8")
    _write_fixture(json_path)
    runner = CliRunner()
    assert (
        runner.invoke(
            app, ["index", "--json", str(json_path), "--db", str(db_path), "--rebuild"]
        ).exit_code
        == 0
    )
    monkeypatch.setenv("WQ_FORUM_RAG_AUTO_REFRESH", "0")

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        show_doc_result = runner.invoke(
            app, ["show-doc", "missing", "--db", str(db_path)]
        )
        show_knowledge_result = runner.invoke(
            app, ["knowledge-show", "missing", "--db", str(db_path)]
        )
        lint_result = runner.invoke(app, ["knowledge-lint", "--db", str(db_path)])
        graph_result = runner.invoke(
            app, ["knowledge-graph", "missing", "--db", str(db_path), "--depth", "1"]
        )
        source_status_result = runner.invoke(
            app, ["source-status", str(source_dir), "--db", str(db_path)]
        )
        source_plan_result = runner.invoke(
            app, ["source-ingest-plan", str(source_dir), "--db", str(db_path)]
        )
    finally:
        conn.rollback()
        conn.close()

    assert show_doc_result.exit_code == 1
    assert '"status": "locked"' not in show_doc_result.stdout
    assert show_knowledge_result.exit_code == 1
    assert '"status": "locked"' not in show_knowledge_result.stdout
    assert lint_result.exit_code == 0
    assert '"blocking_count": 0' in lint_result.stdout
    assert '"status": "locked"' not in lint_result.stdout
    assert graph_result.exit_code == 0
    assert '"missing": true' in graph_result.stdout.lower()
    assert '"status": "locked"' not in graph_result.stdout
    assert source_status_result.exit_code == 0
    assert '"status": "locked"' not in source_status_result.stdout
    assert '"new": 1' in source_status_result.stdout
    assert source_plan_result.exit_code == 0
    assert '"status": "locked"' not in source_plan_result.stdout
    assert '"relative_path": "alpha.md"' in source_plan_result.stdout


def test_refresh_command_prunes_orphans_and_commits_manifest(tmp_path: Path) -> None:
    json_path = tmp_path / "forum.json"
    db_path = tmp_path / "forum.sqlite3"
    _write_fixture(json_path)
    runner = CliRunner()

    initial = runner.invoke(
        app, ["refresh", str(json_path), "--db", str(db_path), "--rebuild"]
    )
    assert initial.exit_code == 0
    initial_payload = json.loads(initial.stdout)
    assert initial_payload["index"]["indexed_topics"] == 2
    assert initial_payload["manifest"]["committed"] is True

    _write_fixture_only_t1(json_path)
    refresh_result = runner.invoke(
        app, ["refresh", str(json_path), "--db", str(db_path)]
    )
    assert refresh_result.exit_code == 0
    refresh_payload = json.loads(refresh_result.stdout)
    assert refresh_payload["index"]["indexed_topics"] == 1
    assert refresh_payload["index"]["pruned"] == 1
    assert refresh_payload["index"]["protected_orphans"] == []

    conn = sqlite3.connect(db_path)
    try:
        topic_ids = {
            row[0] for row in conn.execute("SELECT topic_id FROM topics").fetchall()
        }
        chunk_topic_ids = {
            row[0]
            for row in conn.execute("SELECT DISTINCT topic_id FROM chunks").fetchall()
        }
    finally:
        conn.close()
    assert topic_ids == {"t1"}
    assert chunk_topic_ids == {"t1"}


def test_index_no_prune_keeps_orphans(tmp_path: Path) -> None:
    json_path = tmp_path / "forum.json"
    db_path = tmp_path / "forum.sqlite3"
    _write_fixture(json_path)
    runner = CliRunner()
    runner.invoke(
        app, ["index", "--json", str(json_path), "--db", str(db_path), "--rebuild"]
    )

    _write_fixture_only_t1(json_path)
    result = runner.invoke(
        app, ["index", "--json", str(json_path), "--db", str(db_path), "--no-prune"]
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["pruned"] == 0

    conn = sqlite3.connect(db_path)
    try:
        topic_ids = {
            row[0] for row in conn.execute("SELECT topic_id FROM topics").fetchall()
        }
    finally:
        conn.close()
    assert topic_ids == {"t1", "t2"}


def test_index_prune_protects_topics_referenced_by_knowledge_pages(
    tmp_path: Path,
) -> None:
    json_path = tmp_path / "forum.json"
    db_path = tmp_path / "forum.sqlite3"
    _write_fixture(json_path)
    runner = CliRunner()
    runner.invoke(
        app, ["index", "--json", str(json_path), "--db", str(db_path), "--rebuild"]
    )

    with KnowledgeStore(db_path) as kstore:
        with kstore.conn:
            kstore.conn.execute(
                """
                INSERT INTO knowledge_pages(slug, title, summary, body, status, confidence,
                    content_hash, created_at, updated_at)
                VALUES ('alpha-decay', 'Alpha Decay', 'summary', 'body', 'published', 0.9,
                    'hash', '2026-04-28T13:00:00Z', '2026-04-28T13:00:00Z')
                """
            )
            kstore.conn.execute(
                """
                INSERT INTO knowledge_sources(slug, topic_id, evidence_note)
                VALUES ('alpha-decay', 't2', 'cited')
                """
            )

    _write_fixture_only_t1(json_path)
    result = runner.invoke(
        app, ["index", "--json", str(json_path), "--db", str(db_path)]
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["pruned"] == 0
    assert payload["protected_orphans"] == ["t2"]

    conn = sqlite3.connect(db_path)
    try:
        topic_ids = {
            row[0] for row in conn.execute("SELECT topic_id FROM topics").fetchall()
        }
    finally:
        conn.close()
    assert topic_ids == {"t1", "t2"}


def test_server_reports_package_version() -> None:
    from wq_forum_rag import __version__, build_mcp_server

    server = build_mcp_server()
    assert server.version == __version__
    assert __version__ == "0.2.0"
