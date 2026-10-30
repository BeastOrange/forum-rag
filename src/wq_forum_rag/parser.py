from __future__ import annotations

import json
import re
from collections.abc import Iterator
from html import unescape
from html.parser import HTMLParser
from pathlib import Path

from wq_forum_rag.models import Chunk, Comment, Topic, hash_text, normalize_text


class _HTMLToTextParser(HTMLParser):
    _block_tags = {"div", "p", "section", "article", "ul", "ol", "table", "tr"}
    _line_tags = {"br"}
    _skip_tags = {"script", "style"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._skip_tags:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag in self._block_tags or tag in self._line_tags:
            self._parts.append("\n")
        if tag == "li":
            self._parts.append("\n- ")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._skip_tags and self._skip_depth:
            self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if tag in self._block_tags or tag == "li":
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth and data:
            self._parts.append(data)

    def text(self) -> str:
        raw = unescape("".join(self._parts))
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        return normalize_text(raw)


def clean_html_to_text(html: str | None) -> str:
    if not html:
        return ""
    parser = _HTMLToTextParser()
    parser.feed(html)
    parser.close()
    return parser.text()


def iter_topics(json_path: str | Path) -> Iterator[Topic]:
    with Path(json_path).open("r", encoding="utf-8") as handle:
        payload = json.load(handle)

    for community_id, community in payload.get("byCommunity", {}).items():
        community_title = community.get("title", "")
        topics = community.get("topics", {}) or {}
        for topic_id, topic_data in topics.items():
            comments = _parse_comments(topic_data.get("comments", {}) or {})
            yield Topic(
                id=topic_data.get("id", topic_id),
                community_id=community_id,
                community_title=community_title,
                title=topic_data.get("title", ""),
                body_text=clean_html_to_text(topic_data.get("postContent", "")),
                url=topic_data.get("url", ""),
                created_at=topic_data.get("datetime", ""),
                author=topic_data.get("author", ""),
                vote_num=topic_data.get("voteNum", 0),
                comment_count=topic_data.get("commentNum", len(comments)),
                comments=comments,
            )


def build_chunks(
    topic: Topic,
    *,
    window_size: int = 1_500,
    overlap: int = 200,
) -> list[Chunk]:
    if window_size <= 0:
        raise ValueError("window_size must be positive")
    if overlap < 0 or overlap >= window_size:
        raise ValueError("overlap must be >= 0 and smaller than window_size")

    document = topic.document_text()
    topic_hash = topic.compute_content_hash()
    parent_id = f"{topic.id}:parent"
    parent = Chunk(
        id=parent_id,
        topic_id=topic.id,
        parent_id=None,
        community_id=topic.community_id,
        url=topic.url,
        created_at=topic.created_at,
        chunk_index=0,
        level=0,
        start_offset=0,
        end_offset=len(document),
        content=document,
        content_hash=topic_hash,
    )

    if not document:
        return [parent]

    chunks = [parent]
    start = 0
    child_index = 0
    while start < len(document):
        end = min(len(document), start + window_size)
        content = document[start:end]
        chunks.append(
            Chunk(
                id=f"{topic.id}:chunk:{child_index}",
                topic_id=topic.id,
                parent_id=parent_id,
                community_id=topic.community_id,
                url=topic.url,
                created_at=topic.created_at,
                chunk_index=child_index,
                level=1,
                start_offset=start,
                end_offset=end,
                content=content,
                content_hash=hash_text(
                    topic_hash, str(child_index), str(start), content
                ),
            )
        )
        child_index += 1
        if end >= len(document):
            break
        start = max(end - overlap, start + 1)

    return chunks


def _parse_comments(raw_comments: dict[str, dict[str, object]]) -> tuple[Comment, ...]:
    comments = [
        Comment(
            id=item.get("id", comment_id),
            author=item.get("author", ""),
            content_text=clean_html_to_text(str(item.get("commentContent", ""))),
            created_at=str(item.get("commentTimeDatetime", "")),
            vote_num=int(item.get("voteNum", 0) or 0),
        )
        for comment_id, item in raw_comments.items()
    ]
    comments.sort(key=lambda comment: (comment.created_at, comment.id))
    return tuple(comments)
