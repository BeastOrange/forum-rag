"""Unicode case normalization must not move excerpts off their source spans."""

import sqlite3
from contextlib import closing

import pytest

from wq_forum_rag.batch_search import _excerpt, search_forum_batch
from wq_forum_rag.search import _build_snippet, _find_snippet_start


def row(text):
    return {
        "content": text,
        "chunk_id": "c",
        "chunk_index": 2,
        "chunk_hash": "chunk-hash",
        "topic_hash": "topic-hash",
        "start_offset": 700,
        "end_offset": 700 + len(text),
    }


def test_expanding_lowercase_retains_needle_and_original_coordinates():
    text = "İ" * 160 + " needle " + "x" * 400
    evidence = _excerpt(row(text), 100, "needle")
    assert "needle" in evidence["excerpt"]
    assert evidence["excerpt_start"] == 136
    assert evidence["excerpt_end"] == 236
    assert evidence["excerpt"] == text[136:236]
    assert evidence["document_start_offset"] == 836
    assert evidence["document_end_offset"] == 936
    assert evidence["chunk_document_start_offset"] == 700
    assert evidence["chunk_document_end_offset"] == 700 + len(text)
    assert evidence["chunk_content_hash"] == "chunk-hash"
    assert evidence["topic_content_hash"] == "topic-hash"
    assert _find_snippet_start(text, "needle") == 131
    assert "needle" in _build_snippet("needle", {"content": text})


def test_real_batch_preserves_verbatim_evidence_after_unicode_prefix(tmp_path):
    path = tmp_path / "forum.sqlite3"
    text = "İ" * 160 + " needle " + "x" * 400
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.executescript("""CREATE TABLE topics(topic_id TEXT PRIMARY KEY,title TEXT,community_title TEXT,url TEXT,content_hash TEXT);
            CREATE TABLE chunks(chunk_id TEXT PRIMARY KEY,topic_id TEXT,chunk_index INTEGER,chunk_level INTEGER,
                                start_offset INTEGER,end_offset INTEGER,content TEXT,content_hash TEXT);
            CREATE VIRTUAL TABLE document_fts USING fts5(kind UNINDEXED,record_id UNINDEXED,topic_id UNINDEXED,title,community,content);""")
        conn.execute(
            "INSERT INTO topics VALUES (?,?,?,?,?)",
            ("t", "Example", "", "url", "topic-hash"),
        )
        conn.execute(
            "INSERT INTO chunks VALUES (?,?,?,?,?,?,?,?)",
            ("c", "t", 0, 1, 700, 700 + len(text), text, "chunk-hash"),
        )
        conn.execute(
            "INSERT INTO document_fts VALUES (?,?,?,?,?,?)",
            ("forum_chunk", "c", "t", "Example", "", text),
        )
    hits = search_forum_batch(path, "needle", evidence_chars=100)["results"]
    assert [hit["topic_id"] for hit in hits] == ["t"]
    evidence = hits[0]["evidence"][0]
    assert "needle" in evidence["excerpt"]
    assert (
        evidence["excerpt"] == text[evidence["excerpt_start"] : evidence["excerpt_end"]]
    )


def test_earliest_candidate_with_emoji_cjk_and_combining_characters():
    prefix = "😀e\u0301" * 40 + "İ" * 20
    text = prefix + "中文 " + "x" * 100 + " needle " + "x" * 200
    evidence = _excerpt(row(text), 100, "needle 中文")
    assert evidence["excerpt_start"] == len(prefix) - 25
    assert (
        evidence["excerpt"] == text[evidence["excerpt_start"] : evidence["excerpt_end"]]
    )
    assert "中文" in evidence["excerpt"]
    assert _find_snippet_start(text, "needle 中文") == len(prefix) - 30


@pytest.mark.parametrize(
    ("text", "query", "expected"),
    [
        ("x" * 80 + "CAFÉ", "café", 50),
        ("x" * 80 + "ΟΣ", "ΟΣ", 50),
        ("x" * 80 + "K", "k", 50),
        ("x" * 80 + "ı", "i", 0),
        ("x" * 80 + "ſ", "s", 0),
        ("x" * 80 + "ss", "ß", 0),
    ],
)
def test_existing_lower_substring_semantics_are_preserved(text, query, expected):
    assert _find_snippet_start(text, query) == expected


def test_literal_metacharacters_and_long_no_match():
    text = "x" * 80 + "literal [a].* target" + "x" * 300
    # Query candidate generation stays unchanged; punctuation remains literal.
    assert _find_snippet_start(text, "[a].*") == 55
    assert _excerpt(row("x" * 10000), 100, "no_match")["excerpt"] == "x" * 100
    assert _find_snippet_start("x" * 10000, "no_match") == 0


@pytest.mark.parametrize(
    ("text", "candidates", "expected"),
    [
        ("x" * 80 + "literal [a].* target", ["[a].*"], 88),
        ("x" * 80 + "literal [a].* target", ["[z].*"], None),
        ("İ" * 80 + " ΟΣ later", ["ΟΣ"], 81),
        ("prefix İ target", ["i\u0307"], 7),
        ("prefix İ target", ["\u0307"], 7),
        ("İ" * 80 + "中文 😀 target", ["target", "中文"], 80),
        ("plain", ["", "absent"], None),
    ],
)
def test_shared_mapping_is_literal_and_preserves_unicode_lower_semantics(
    text, candidates, expected
):
    from wq_forum_rag.search import _first_literal_match_start

    assert _first_literal_match_start(text, candidates) == expected


def test_excerpt_retains_end_clamp_and_short_text_behavior():
    text = "x" * 150 + " needle"
    evidence = _excerpt(row(text), 100, "needle")
    assert evidence["excerpt_start"] == len(text) - 100
    assert evidence["excerpt_end"] == len(text)
    assert "needle" in evidence["excerpt"]
    short = _excerpt(row("İ needle"), 100, "needle")
    assert short["excerpt"] == "İ needle"
    assert short["excerpt_start"] == 0
