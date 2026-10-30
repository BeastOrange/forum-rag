from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

SRC_ROOT = Path(__file__).resolve().parents[1] / "src"


def _load_module(module_name: str, file_path: Path):
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


package = types.ModuleType("wq_forum_rag")
package.__path__ = [str(SRC_ROOT / "wq_forum_rag")]
sys.modules.setdefault("wq_forum_rag", package)

_load_module("wq_forum_rag.models", SRC_ROOT / "wq_forum_rag" / "models.py")
parser_module = _load_module(
    "wq_forum_rag.parser", SRC_ROOT / "wq_forum_rag" / "parser.py"
)
storage_module = _load_module(
    "wq_forum_rag.storage", SRC_ROOT / "wq_forum_rag" / "storage.py"
)

build_chunks = parser_module.build_chunks
iter_topics = parser_module.iter_topics
ForumStore = storage_module.ForumStore


@pytest.fixture
def sample_json_path(tmp_path: Path) -> Path:
    payload = {
        "byCommunity": {
            "community-1": {
                "title": "中文论坛",
                "topics": {
                    "topic-1": {
                        "id": "topic-1",
                        "author": "alice",
                        "commentNum": 2,
                        "comments": {
                            "comment-2": {
                                "id": "comment-2",
                                "author": "charlie",
                                "commentContent": "<p>Second reply</p>",
                                "commentTimeDatetime": "2024-01-02T00:00:00Z",
                                "voteNum": 1,
                            },
                            "comment-1": {
                                "id": "comment-1",
                                "author": "bob",
                                "commentContent": '<p>Read the <a href="https://example.com">docs</a>.</p>',
                                "commentTimeDatetime": "2024-01-01T00:00:00Z",
                                "voteNum": 2,
                            },
                        },
                        "datetime": "2024-01-01T12:00:00Z",
                        "postContent": """
                            <div class="post-content">
                                <div class="post-body">
                                    <p>Hello <strong>world</strong> and <a href="https://example.com">link</a>.</p>
                                    <p>Second line<img src="/ignored.png"></p>
                                </div>
                            </div>
                        """,
                        "title": "Sample topic",
                        "url": "https://forum.example/topics/topic-1",
                        "voteNum": 3,
                    }
                },
            }
        }
    }
    json_path = tmp_path / "forum.json"
    json_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return json_path


def test_iter_topics_parses_nested_json_and_cleans_html(sample_json_path: Path) -> None:
    topic = next(iter(iter_topics(sample_json_path)))

    assert topic.id == "topic-1"
    assert topic.community_id == "community-1"
    assert topic.community_title == "中文论坛"
    assert topic.body_text == "Hello world and link.\nSecond line"
    assert [comment.id for comment in topic.comments] == ["comment-1", "comment-2"]
    assert topic.comments[0].content_text == "Read the docs."
    assert "ignored" not in topic.body_text


def test_build_chunks_creates_parent_child_windows(sample_json_path: Path) -> None:
    topic = next(iter(iter_topics(sample_json_path)))

    chunks = build_chunks(topic, window_size=35, overlap=10)

    assert chunks[0].is_parent is True
    assert chunks[0].parent_id is None
    assert chunks[0].topic_id == topic.id
    assert chunks[0].community_id == topic.community_id
    assert chunks[0].url == topic.url
    assert chunks[0].created_at == topic.created_at
    assert len(chunks) > 2
    assert all(chunk.parent_id == chunks[0].id for chunk in chunks[1:])
    assert all(chunk.level == 1 for chunk in chunks[1:])


def test_store_upsert_skips_unchanged_topics_and_rebuilds_changed_ones(
    sample_json_path: Path,
    tmp_path: Path,
) -> None:
    topic = next(iter(iter_topics(sample_json_path)))
    db_path = tmp_path / "forum.sqlite3"

    with ForumStore(db_path) as store:
        first, first_seen = store.upsert_topics([topic], window_size=40, overlap=10)
        stored = store.get_topic(topic.id)
        initial_chunks = list(store.iter_chunks(topic.id))
        second, second_seen = store.upsert_topics([topic], window_size=40, overlap=10)

        updated_payload = topic.to_dict()
        updated_payload["body_text"] = "Updated body text for storage verification."
        updated_payload["content_hash"] = ""
        updated_topic = type(topic).from_dict(updated_payload)
        third, third_seen = store.upsert_topics(
            [updated_topic], window_size=40, overlap=10
        )
        updated_stored = store.get_topic(topic.id)
        updated_chunks = list(store.iter_chunks(topic.id))

    assert first == {
        "seen": 1,
        "inserted": 1,
        "updated": 0,
        "skipped": 0,
        "chunks_written": len(initial_chunks),
    }
    assert first_seen == {topic.id}
    assert second_seen == {topic.id}
    assert third_seen == {topic.id}
    assert stored is not None
    assert stored.comments[0].content_text == "Read the docs."
    assert second["skipped"] == 1
    assert second["chunks_written"] == 0
    assert third["updated"] == 1
    assert updated_stored is not None
    assert updated_stored.body_text == "Updated body text for storage verification."
    assert any("Updated body text" in chunk.content for chunk in updated_chunks)
