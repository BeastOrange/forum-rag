from __future__ import annotations

import sqlite3

from test_evolution import _index_fixture
from test_knowledge_quality import propose

from wq_forum_rag.evolution import EvolutionService
from wq_forum_rag.search_records import search_knowledge_records


def _search(db):
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        return search_knowledge_records(conn, "Evidence guide", top_k=5)


def _reviewed(service, slug):
    propose(service, slug)
    service.review_knowledge_page(
        slug, "reviewed", note="Checked claim against excerpt"
    )
    assert service.publish_knowledge_page(slug)["published"]


def test_search_excludes_both_conflict_endpoints_and_changed_sources(tmp_path):
    db = _index_fixture(tmp_path)
    service = EvolutionService(db)
    _reviewed(service, "alpha/base")
    _reviewed(service, "alpha/other")
    assert len(_search(db)) == 2
    service.link_knowledge_pages("alpha/other", "alpha/base", "conflicts_with")
    assert _search(db) == []


def test_search_excludes_changed_source_without_writing_or_migrating(tmp_path):
    db = _index_fixture(tmp_path)
    service = EvolutionService(db)
    _reviewed(service, "alpha/base")
    assert _search(db)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE topics SET content_hash='changed' WHERE topic_id='t1'")
    before = db.read_bytes()
    assert _search(db) == []
    assert db.read_bytes() == before


def test_conflict_relations_do_not_contribute_positive_backlink_boost(tmp_path):
    from wq_forum_rag.search_records import _knowledge_link_counts

    db = _index_fixture(tmp_path)
    service = EvolutionService(db)
    for slug in ["alpha/source", "alpha/target"]:
        _reviewed(service, slug)
    service.link_knowledge_pages("alpha/source", "alpha/target", "conflicts_with")
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        assert _knowledge_link_counts(conn).get("alpha/target", 0) == 0


def test_mixed_query_unreviewed_fts_hits_cannot_crowd_out_trusted_page(tmp_path):
    db = _index_fixture(tmp_path)
    service = EvolutionService(db)
    for slug in ["alpha/first", "alpha/second", "alpha/third", "alpha/trusted"]:
        _reviewed(service, slug)
        if slug != "alpha/trusted":
            service.review_knowledge_page(
                slug, "needs_review", note="Needs additional verification"
            )
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        assert search_knowledge_records(conn, "Evidence")[0]["slug"] == "alpha/trusted"
        assert (
            search_knowledge_records(conn, "中性 Evidence")[0]["slug"]
            == "alpha/trusted"
        )
