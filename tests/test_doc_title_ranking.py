import sqlite3
from contextlib import closing

import pytest

from wq_forum_rag.documents import Document, DocumentStore
from wq_forum_rag.search_index import rebuild_search_index
from wq_forum_rag.search_records import search_doc_records


class CollisionBackend:
    """Make an incidental mention win vector ranking, regardless of its title."""

    def embed_documents(self, texts):
        return [
            [1.0, 0.0] if text == "operators" or "Brain Genius" in text else [0.0, 1.0]
            for text in texts
        ]

    def embed_query(self, text):
        return [1.0, 0.0]


@pytest.fixture
def docs(tmp_path):
    path = tmp_path / "docs.sqlite3"
    with DocumentStore(path) as store:
        store.upsert_documents(
            [
                Document(
                    "operators",
                    "Operators",
                    "Operators.md",
                    "A catalog of available functions.",
                ),
                Document(
                    "brain-genius",
                    "Brain Genius",
                    "Brain Genius.md",
                    "operators recommendations",
                ),
            ]
        )
    rebuild_search_index(path)
    return path


def test_complete_title_beats_incidental_hash_collision(docs):
    with closing(sqlite3.connect(docs)) as conn, conn:
        conn.row_factory = sqlite3.Row
        results = search_doc_records(
            conn, "operators", top_k=2, embedding_backend=CollisionBackend()
        )
        assert [row["slug"] for row in results] == ["operators", "brain-genius"]
        assert results[0]["exact_title_match"] is True
        assert results[1]["exact_title_match"] is False
        assert (
            search_doc_records(
                conn, "operators", top_k=1, embedding_backend=CollisionBackend()
            )
            == results[:1]
        )


def test_exact_title_can_enter_even_when_fts_pool_omits_it(docs, monkeypatch):
    from wq_forum_rag import search_index

    monkeypatch.setattr(search_index, "candidate_ids", lambda *args, **kwargs: [])
    with closing(sqlite3.connect(docs)) as conn, conn:
        conn.row_factory = sqlite3.Row
        assert (
            search_doc_records(conn, " OPERATORS ", top_k=1)[0]["slug"] == "operators"
        )
        assert search_doc_records(conn, "operators missingcondition", top_k=1) == []
        assert search_doc_records(conn, "", top_k=1) == []


@pytest.mark.parametrize(
    "title,query",
    [
        ("Résumé", "RÉSUMÉ"),
        ("Multiple  words", " multiple   WORDS "),
    ],
)
def test_title_normalization_is_consistent_when_fts_misses(
    docs, monkeypatch, title, query
):
    from wq_forum_rag import search_index

    with DocumentStore(docs) as store:
        store.upsert_documents(
            [Document("named", title, "named.md", "Document introduction.")]
        )
    monkeypatch.setattr(search_index, "candidate_ids", lambda *args, **kwargs: [])
    with closing(sqlite3.connect(docs)) as conn, conn:
        conn.row_factory = sqlite3.Row
        results = search_doc_records(conn, query, top_k=1)
        assert results and results[0]["slug"] == "named"
        assert results[0]["exact_title_match"] is True


def test_title_lookup_preserves_a_callers_active_cursor(docs):
    with closing(sqlite3.connect(docs)) as conn, conn:
        conn.row_factory = sqlite3.Row
        assert search_doc_records(conn, "operators", top_k=1)
        with closing(
            conn.execute("SELECT slug FROM documents ORDER BY slug")
        ) as cursor:
            first = cursor.fetchone()[0]
            assert (
                search_doc_records(conn, "operators", top_k=1)[0]["slug"] == "operators"
            )
            assert cursor.fetchone()[0] != first
