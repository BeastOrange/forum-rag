# -*- coding: utf-8 -*-
# Author: konrad
# WQ_ID: OB53521
# Encoding: utf-8

from __future__ import annotations

from dataclasses import dataclass
from html import unescape
import math
import re
from typing import Any

from .embeddings import EmbeddingBackend, build_embedding_backend

TITLE_WEIGHT = 2.5
COMMUNITY_WEIGHT = 1.4
CONTENT_WEIGHT = 1.0
EXACT_MATCH_BONUS = 1.5
SNIPPET_WINDOW = 180
WORD_PATTERN = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]+")
# Strip a conservative markup subset: bare tags and explicitly assigned
# attributes. Free-form comparisons (including a<b and flag>0) remain text.
# Bare a<b>c is inherently ambiguous and is treated as a <b> tag; valueless
# attributes and malformed markup are retained rather than guessing them away.
TAG_PATTERN = re.compile(
    r"</[A-Za-z][A-Za-z0-9:-]*\s*>"
    r"|<[A-Za-z][A-Za-z0-9:-]*"
    r"(?:\s+[A-Za-z_:][A-Za-z0-9_.:-]*\s*=\s*"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s\"'=<>`]+))*\s*/?>"
)
SPACE_PATTERN = re.compile(r"\s+")


@dataclass(frozen=True)
class SearchHit:
    chunk_id: str
    topic_id: str
    score: float
    snippet: str
    title: str
    community: str
    dense_score: float
    lexical_score: float

def cosine_similarity(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        raise ValueError("cosine_similarity requires vectors with the same length")
    if not left:
        return 0.0
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    numerator = sum(a * b for a, b in zip(left, right, strict=True))
    return numerator / (left_norm * right_norm)


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    elif isinstance(value, (list, tuple, set)):
        text = " ".join(_clean_text(item) for item in value)
    else:
        text = str(value)
    return SPACE_PATTERN.sub(" ", unescape(TAG_PATTERN.sub(" ", text))).strip()

def _get_field(chunk: dict[str, Any], *names: str) -> str:
    for name in names:
        if name in chunk and chunk[name] is not None:
            return _clean_text(chunk[name])
    metadata = chunk.get("metadata")
    if isinstance(metadata, dict):
        for name in names:
            if name in metadata and metadata[name] is not None:
                return _clean_text(metadata[name])
    return ""

def _normalize_text(text: str) -> str:
    return SPACE_PATTERN.sub(" ", text.lower()).strip()


def tokenize_text(text: str) -> list[str]:
    tokens: list[str] = []
    seen: set[str] = set()
    for raw_token in WORD_PATTERN.findall(_normalize_text(text)):
        if re.fullmatch(r"[\u4e00-\u9fff]+", raw_token):
            variants = [raw_token]
            variants.extend(raw_token[index] for index in range(len(raw_token)))
            variants.extend(
                raw_token[index : index + 2]
                for index in range(max(len(raw_token) - 1, 0))
            )
        else:
            variants = [raw_token]
        for token in variants:
            if token and token not in seen:
                tokens.append(token)
                seen.add(token)
    return tokens

def _contains_exact(text: str, token: str) -> bool:
    if not text or not token:
        return False
    if re.fullmatch(r"[\u4e00-\u9fff]+", token):
        return token in text
    pattern = re.compile(rf"\b{re.escape(token)}\b")
    return bool(pattern.search(text))


def lexical_score(
    query: str,
    chunk: dict[str, Any],
    *,
    title_weight: float = TITLE_WEIGHT,
    community_weight: float = COMMUNITY_WEIGHT,
    content_weight: float = CONTENT_WEIGHT,
    exact_match_bonus: float = EXACT_MATCH_BONUS,
) -> float:
    normalized_query = _normalize_text(query)
    query_tokens = tokenize_text(normalized_query)
    if not query_tokens:
        return 0.0

    title = _normalize_text(_get_field(chunk, "title"))
    community = _normalize_text(_get_field(chunk, "community", "community_title"))
    content = _normalize_text(
        _get_field(chunk, "text", "content", "body", "postContent", "post_content")
    )
    title_tokens = set(tokenize_text(title))
    community_tokens = set(tokenize_text(community))
    content_tokens = set(tokenize_text(content))

    score = 0.0
    token_weight = 1.0 / len(query_tokens)
    for token in query_tokens:
        if token in title_tokens:
            score += title_weight * token_weight
        if token in community_tokens:
            score += community_weight * token_weight
        if token in content_tokens:
            score += content_weight * token_weight
        if _contains_exact(title, token):
            score += exact_match_bonus * title_weight * token_weight
        if _contains_exact(community, token):
            score += exact_match_bonus * community_weight * token_weight
        if _contains_exact(content, token):
            score += exact_match_bonus * content_weight * token_weight

    if _contains_exact(title, normalized_query):
        score += exact_match_bonus * title_weight
    if _contains_exact(community, normalized_query):
        score += exact_match_bonus * community_weight
    if _contains_exact(content, normalized_query):
        score += exact_match_bonus * content_weight
    return score


def _chunk_text(chunk: dict[str, Any]) -> str:
    fields = [
        _get_field(chunk, "community", "community_title"),
        _get_field(chunk, "title"),
        _get_field(chunk, "text", "content", "body", "postContent", "post_content"),
    ]
    return " ".join(field for field in fields if field).strip()


def _normalize_scores(values: list[float]) -> list[float]:
    positives = [max(value, 0.0) for value in values]
    max_score = max(positives, default=0.0)
    if max_score == 0.0:
        return [0.0] * len(values)
    return [value / max_score for value in positives]


def _matches_filters(chunk: dict[str, Any], filters: dict[str, Any] | None) -> bool:
    if not filters:
        return True
    for key, expected in filters.items():
        actual = _get_field(chunk, key)
        if isinstance(expected, (set, list, tuple)):
            normalized = {_clean_text(item) for item in expected}
            if actual not in normalized:
                return False
            continue
        if actual != _clean_text(expected):
            return False
    return True


def _first_literal_match_start(text: str, candidates: list[str]) -> int | None:
    """Find the earliest lowercased literal match, returning an original offset.

    Keep str.lower substring semantics (not re.I or casefold equivalences).
    Unicode lowercase expansion, such as İ -> i + combining dot, must not
    shift source offsets. Contextual lowercasing changes sigma's form but not
    its width, so per-character widths map the full-string lowercase offset.
    """
    normalized_text = text.lower()
    starts = [normalized_text.find(candidate.lower()) for candidate in candidates if candidate]
    start = min((position for position in starts if position >= 0), default=None)
    if start is None or len(normalized_text) == len(text):
        return start
    normalized_offset = 0
    for original_offset, char in enumerate(text):
        normalized_offset += len(char.lower())
        if normalized_offset > start:
            return original_offset
    return None


def _find_snippet_start(text: str, query: str) -> int:
    start = _first_literal_match_start(text, [query] + tokenize_text(query))
    return max(start - 30, 0) if start is not None else 0


def _build_snippet(query: str, chunk: dict[str, Any], max_chars: int = SNIPPET_WINDOW) -> str:
    text = _get_field(chunk, "text", "content", "body", "postContent", "post_content")
    if not text:
        text = _get_field(chunk, "title")
    if len(text) <= max_chars:
        return text
    start = _find_snippet_start(text, query)
    end = min(start + max_chars, len(text))
    snippet = text[start:end].strip()
    if start > 0:
        snippet = f"...{snippet}"
    if end < len(text):
        snippet = f"{snippet}..."
    return snippet


def _topic_id(chunk: dict[str, Any], index: int) -> str:
    return _get_field(chunk, "topic_id", "topicId", "id") or str(index)


def _chunk_id(chunk: dict[str, Any], index: int, topic_id: str) -> str:
    return _get_field(chunk, "chunk_id", "chunkId") or f"{topic_id}#{index}"


def hybrid_search(
    query: str,
    chunks: list[dict[str, Any]],
    top_k: int = 5,
    filters: dict[str, Any] | None = None,
    *,
    embedding_backend: EmbeddingBackend | None = None,
    dense_weight: float = 0.55,
    lexical_weight: float = 0.45,
) -> list[SearchHit]:
    filtered_chunks = [chunk for chunk in chunks if _matches_filters(chunk, filters)]
    if not filtered_chunks or top_k <= 0:
        return []

    lexical_scores = [lexical_score(query, chunk) for chunk in filtered_chunks]
    candidate_limit = min(len(filtered_chunks), max(top_k * 20, 80))
    candidate_indexes = sorted(
        (index for index in range(len(filtered_chunks)) if lexical_scores[index] > 0),
        key=lambda index: (lexical_scores[index], _chunk_id(filtered_chunks[index], index, _topic_id(filtered_chunks[index], index))),
        reverse=True,
    )[:candidate_limit]

    if not candidate_indexes:
        return []
    backend = embedding_backend or build_embedding_backend()
    query_embedding = backend.embed_query(query)
    candidate_embeddings = backend.embed_documents(
        [_chunk_text(filtered_chunks[index]) for index in candidate_indexes]
    )
    dense_scores = [
        cosine_similarity(query_embedding, document_embedding)
        for document_embedding in candidate_embeddings
    ]
    candidate_lexical_scores = [lexical_scores[index] for index in candidate_indexes]
    normalized_dense = _normalize_scores(dense_scores)
    normalized_lexical = _normalize_scores(candidate_lexical_scores)

    hits: list[SearchHit] = []
    for candidate_position, index in enumerate(candidate_indexes):
        chunk = filtered_chunks[index]
        topic_id = _topic_id(chunk, index)
        chunk_id = _chunk_id(chunk, index, topic_id)
        dense_value = normalized_dense[candidate_position]
        lexical_value = normalized_lexical[candidate_position]
        score = (dense_weight * dense_value) + (lexical_weight * lexical_value)
        hits.append(
            SearchHit(
                chunk_id=chunk_id,
                topic_id=topic_id,
                score=score,
                snippet=_build_snippet(query, chunk),
                title=_get_field(chunk, "title"),
                community=_get_field(chunk, "community", "community_title"),
                dense_score=dense_value,
                lexical_score=lexical_value,
            )
        )
    hits.sort(
        key=lambda hit: (hit.score, hit.lexical_score, hit.dense_score, hit.chunk_id),
        reverse=True,
    )
    return hits[:top_k]


class ForumSearcher:
    """In-memory hybrid searcher for forum chunks."""

    def __init__(
        self,
        chunks: list[dict[str, Any]],
        embedding_backend: EmbeddingBackend | None = None,
        *,
        dense_weight: float = 0.55,
        lexical_weight: float = 0.45,
    ) -> None:
        self._chunks = list(chunks)
        self._embedding_backend = embedding_backend or build_embedding_backend()
        self._dense_weight = dense_weight
        self._lexical_weight = lexical_weight

    def search(
        self,
        query: str,
        top_k: int = 5,
        filters: dict[str, Any] | None = None,
    ) -> list[SearchHit]:
        return hybrid_search(
            query=query,
            chunks=self._chunks,
            top_k=top_k,
            filters=filters,
            embedding_backend=self._embedding_backend,
            dense_weight=self._dense_weight,
            lexical_weight=self._lexical_weight,
        )
