from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    text = value.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
    lines = [" ".join(line.split()) for line in text.split("\n")]
    compact = "\n".join(line for line in lines if line)
    return compact.strip()


def hash_text(*parts: str) -> str:
    payload = "\x1f".join(parts).encode("utf-8")
    return sha256(payload).hexdigest()


@dataclass(slots=True)
class Comment:
    id: str
    author: str
    content_text: str
    created_at: str
    vote_num: int = 0
    content_hash: str = ""

    def __post_init__(self) -> None:
        self.id = str(self.id)
        self.author = normalize_text(self.author)
        self.content_text = normalize_text(self.content_text)
        self.created_at = (self.created_at or "").strip()
        self.vote_num = int(self.vote_num or 0)
        if not self.content_hash:
            self.content_hash = hash_text(
                self.id,
                self.author,
                self.created_at,
                self.content_text,
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "author": self.author,
            "content_text": self.content_text,
            "created_at": self.created_at,
            "vote_num": self.vote_num,
            "content_hash": self.content_hash,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Comment:
        return cls(**payload)


@dataclass(slots=True)
class Topic:
    id: str
    community_id: str
    community_title: str
    title: str
    body_text: str
    url: str
    created_at: str
    author: str = ""
    vote_num: int = 0
    comment_count: int = 0
    comments: tuple[Comment, ...] = field(default_factory=tuple)
    content_hash: str = ""

    def __post_init__(self) -> None:
        self.id = str(self.id)
        self.community_id = str(self.community_id)
        self.community_title = normalize_text(self.community_title)
        self.title = normalize_text(self.title)
        self.body_text = normalize_text(self.body_text)
        self.url = (self.url or "").strip()
        self.created_at = (self.created_at or "").strip()
        self.author = normalize_text(self.author)
        self.vote_num = int(self.vote_num or 0)
        self.comments = tuple(self.comments)
        self.comment_count = int(self.comment_count or len(self.comments))
        if not self.content_hash:
            self.content_hash = self.compute_content_hash()

    def document_text(self) -> str:
        parts: list[str] = []
        if self.title:
            parts.append(f"Title: {self.title}")
        if self.body_text:
            parts.extend(("Body:", self.body_text))
        if self.comments:
            parts.append("Comments:")
            for index, comment in enumerate(self.comments, start=1):
                header = f"[Comment {index}]"
                if comment.author:
                    header += f" {comment.author}"
                if comment.created_at:
                    header += f" @ {comment.created_at}"
                parts.extend((header, comment.content_text))
        return "\n".join(part for part in parts if part).strip()

    def compute_content_hash(self) -> str:
        return hash_text(self.document_text())

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "community_id": self.community_id,
            "community_title": self.community_title,
            "title": self.title,
            "body_text": self.body_text,
            "url": self.url,
            "created_at": self.created_at,
            "author": self.author,
            "vote_num": self.vote_num,
            "comment_count": self.comment_count,
            "comments": [comment.to_dict() for comment in self.comments],
            "content_hash": self.content_hash,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Topic:
        comments = tuple(
            Comment.from_dict(item) for item in payload.get("comments", [])
        )
        return cls(
            id=payload["id"],
            community_id=payload["community_id"],
            community_title=payload.get("community_title", ""),
            title=payload.get("title", ""),
            body_text=payload.get("body_text", ""),
            url=payload.get("url", ""),
            created_at=payload.get("created_at", ""),
            author=payload.get("author", ""),
            vote_num=payload.get("vote_num", 0),
            comment_count=payload.get("comment_count", len(comments)),
            comments=comments,
            content_hash=payload.get("content_hash", ""),
        )


@dataclass(slots=True)
class Chunk:
    id: str
    topic_id: str
    parent_id: str | None
    community_id: str
    url: str
    created_at: str
    chunk_index: int
    level: int
    start_offset: int
    end_offset: int
    content: str
    content_hash: str

    def __post_init__(self) -> None:
        self.id = str(self.id)
        self.topic_id = str(self.topic_id)
        self.parent_id = str(self.parent_id) if self.parent_id else None
        self.community_id = str(self.community_id)
        self.url = (self.url or "").strip()
        self.created_at = (self.created_at or "").strip()
        self.chunk_index = int(self.chunk_index)
        self.level = int(self.level)
        self.start_offset = int(self.start_offset)
        self.end_offset = int(self.end_offset)
        self.content = self.content or ""
        if not self.content_hash:
            self.content_hash = hash_text(
                self.topic_id,
                self.parent_id or "",
                str(self.chunk_index),
                self.content,
            )

    @property
    def is_parent(self) -> bool:
        return self.parent_id is None
