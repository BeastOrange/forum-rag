"""Code comparisons must survive the search surface's HTML cleanup."""

import sqlite3
from contextlib import closing

import pytest

from wq_forum_rag.batch_search import search_forum_batch
from wq_forum_rag.search import _clean_text, lexical_score
from wq_forum_rag.search_index import candidate_ids


@pytest.mark.parametrize(
    "expression",
    [
        "if a < b and risk_gate > 0: exit_position()",
        "if a<b and risk_gate>0: exit_position()",
        "if a<b&&risk_gate>0: exit_position()",
    ],
)
def test_candidate_code_identifier_reaches_batch_results(tmp_path, expression):
    path = tmp_path / "forum.sqlite3"
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.executescript("""
            CREATE TABLE topics(topic_id TEXT PRIMARY KEY, title TEXT, community_title TEXT, url TEXT, content_hash TEXT);
            CREATE TABLE chunks(chunk_id TEXT PRIMARY KEY, topic_id TEXT, chunk_index INTEGER, chunk_level INTEGER,
                                start_offset INTEGER, end_offset INTEGER, content TEXT, content_hash TEXT);
            CREATE VIRTUAL TABLE document_fts USING fts5(kind UNINDEXED, record_id UNINDEXED, topic_id UNINDEXED,
                                                       title, community, content);
        """)
        conn.execute(
            "INSERT INTO topics VALUES (?,?,?,?,?)",
            ("code", "Example", "", "url", "topic-hash"),
        )
        conn.execute(
            "INSERT INTO chunks VALUES (?,?,?,?,?,?,?,?)",
            ("chunk", "code", 0, 1, 0, len(expression), expression, "chunk-hash"),
        )
        conn.execute(
            "INSERT INTO document_fts VALUES (?,?,?,?,?,?)",
            ("forum_chunk", "chunk", "code", "Example", "", expression),
        )
        assert candidate_ids(conn, "risk_gate", kind="forum_chunk", limit=10) == [
            "chunk"
        ]
    hits = search_forum_batch(path, "risk_gate")["results"]
    assert [hit["topic_id"] for hit in hits] == ["code"]
    assert hits[0]["evidence"][0]["excerpt"] == expression
    assert search_forum_batch(path, "unknown_identifier")["results"] == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('<p class="note">risk_gate &amp; limits</p>', "risk_gate & limits"),
        ("<DIV data-note='a > b'><b>risk_gate</b><br/>limit</DIV>", "risk_gate limit"),
        ('<span class=note data-id="1">risk_gate</span>', "risk_gate"),
        (
            "if a &lt; b and risk_gate &gt; 0: exit_position()",
            "if a < b and risk_gate > 0: exit_position()",
        ),
        (
            "<p>if a&lt;b and risk_gate&gt;0: exit_position()</p>",
            "if a<b and risk_gate>0: exit_position()",
        ),
    ],
)
def test_markup_attributes_entities_and_encoded_code(text, expected):
    assert _clean_text(text) == expected
    assert lexical_score("risk_gate", {"content": text}) > 0
    assert lexical_score("unrelatedword", {"content": text}) == 0


def test_tag_attributes_do_not_become_searchable_content():
    assert (
        lexical_score(
            "hiddenword", {"content": '<p title="hiddenword">visibleword</p>'}
        )
        == 0
    )
    assert (
        lexical_score(
            "visibleword", {"content": '<p title="hiddenword">visibleword</p>'}
        )
        > 0
    )


def test_code_inside_markup_keeps_comparisons():
    text = '<pre class="code">if a<b and risk_gate>0: exit_position()</pre>'
    assert _clean_text(text) == "if a<b and risk_gate>0: exit_position()"
    assert lexical_score("risk_gate", {"content": text}) > 0


def test_explicit_markup_ambiguity_boundary():
    # Bare tags are recognized even when surrounding text also looks like code.
    assert _clean_text("a<b>c") == "a c"
    # Valueless attributes are outside the conservative subset: do not guess.
    assert _clean_text("<input disabled>") == "<input disabled>"
