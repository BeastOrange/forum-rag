"""Language metadata follows source text rather than export wrapper labels."""

import sqlite3
from contextlib import closing

import pytest

from wq_forum_rag.batch_search import _language, search_forum_batch
from wq_forum_rag.models import Comment, Topic


def topic(title, body, comments=()):
    return Topic(
        id="1",
        community_id="c",
        community_title="社区",
        title=title,
        body_text=body,
        url="https://example.test/1",
        created_at="2026-10-29",
        comments=comments,
    )


@pytest.mark.parametrize(
    ("title", "body", "expected"),
    [
        ("缺失值", "这里讨论缺失值处理。", "zh"),
        ("缺失值", "", "zh"),
        ("", "这里讨论缺失值处理。", "zh"),
        ("Missing values", "Handle absent observations carefully.", "en"),
        ("Missing values", "这里讨论缺失值处理。", "mixed"),
        ("缺失值", "使用 missing values 过滤。", "mixed"),
    ],
)
def test_real_export_wrapper_preserves_content_language(title, body, expected):
    post = topic(title, body)
    assert _language(post.title, post.document_text()) == expected


def test_real_comment_header_does_not_add_english_language():
    post = topic(
        "缺失值",
        "这里讨论缺失值处理。",
        (
            Comment(
                id="2",
                author="EnglishAuthor",
                content_text="需要核对数据。",
                created_at="2026-10-29",
            ),
        ),
    )
    assert _language(post.title, post.document_text()) == "zh"


def test_real_export_language_groups_and_evidence_are_consistent(tmp_path):
    post = topic("缺失值", "这里讨论缺失值处理。")
    exported = post.document_text()
    db = tmp_path / "forum.sqlite3"
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.executescript("""CREATE TABLE topics(topic_id TEXT PRIMARY KEY,title TEXT,community_title TEXT,url TEXT,content_hash TEXT);
            CREATE TABLE chunks(chunk_id TEXT PRIMARY KEY,topic_id TEXT,chunk_index INTEGER,chunk_level INTEGER,start_offset INTEGER,end_offset INTEGER,content TEXT,content_hash TEXT);""")
        conn.execute(
            "INSERT INTO topics VALUES(?,?,?,?,?)",
            (post.id, post.title, post.community_title, post.url, post.content_hash),
        )
        conn.execute(
            "INSERT INTO chunks VALUES(?,?,?,?,?,?,?,?)",
            ("chunk", post.id, 0, 1, 0, len(exported), exported, "chunk-version"),
        )
    result = search_forum_batch(db, "缺失值")
    assert result["language_groups"]["zh"] == [post.id]
    assert result["language_groups"]["mixed"] == []
    assert result["results"][0]["language"] == "zh"
    assert result["results"][0]["evidence"][0]["excerpt"] == exported
