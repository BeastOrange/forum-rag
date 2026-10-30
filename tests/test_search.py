# Author: konrad
# WQ_ID: OB53521
# Encoding: utf-8

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "src" / "wq_forum_rag"


def _load_module(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load module: {module_name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


package = types.ModuleType("wq_forum_rag")
package.__path__ = [str(PACKAGE_ROOT)]
sys.modules.setdefault("wq_forum_rag", package)

embeddings = _load_module("wq_forum_rag.embeddings", PACKAGE_ROOT / "embeddings.py")
search = _load_module("wq_forum_rag.search", PACKAGE_ROOT / "search.py")

HashEmbeddingBackend = embeddings.HashEmbeddingBackend
build_embedding_backend = embeddings.build_embedding_backend
ForumSearcher = search.ForumSearcher
cosine_similarity = search.cosine_similarity
hybrid_search = search.hybrid_search
lexical_score = search.lexical_score


@pytest.fixture
def sample_chunks() -> list[dict[str, str]]:
    return [
        {
            "topic_id": "t-chn-1",
            "chunk_id": "t-chn-1#post",
            "community": "中文论坛",
            "title": "CHN Region 是否提供高频数据",
            "text": "<p>Hello BRAIN Team, 请问 CHN Region 是否提供 tick/minutes 高频数据？</p>",
        },
        {
            "topic_id": "t-usa-1",
            "chunk_id": "t-usa-1#post",
            "community": "English Forum",
            "title": "How to improve Sharpe after the universe test",
            "text": "<p>The universe Sharpe test still fails. Any advice to improve Sharpe?</p>",
        },
        {
            "topic_id": "t-usa-2",
            "chunk_id": "t-usa-2#post",
            "community": "English Forum",
            "title": "How to improve share count reporting",
            "text": "<p>This post is about share count disclosure, not Sharpe.</p>",
        },
    ]


def test_hash_embedding_backend_is_deterministic() -> None:
    backend = HashEmbeddingBackend(dims=64, seed="unit-test")
    left = backend.embed_query("Sharpe test with CHN data")
    right = backend.embed_query("Sharpe test with CHN data")

    assert left == right
    assert cosine_similarity(left, right) == pytest.approx(1.0)


def test_build_embedding_backend_falls_back_to_hash() -> None:
    backend = build_embedding_backend()
    assert isinstance(backend, HashEmbeddingBackend)


def test_lexical_score_prefers_exact_title_and_community_matches(
    sample_chunks: list[dict[str, str]],
) -> None:
    sharpe_hit = lexical_score("sharpe", sample_chunks[1])
    share_hit = lexical_score("sharpe", sample_chunks[2])
    chinese_hit = lexical_score("中文论坛 高频数据", sample_chunks[0])

    assert sharpe_hit > share_hit
    assert chinese_hit > 0


def test_hybrid_search_returns_ranked_hits_with_filters_and_snippets(
    sample_chunks: list[dict[str, str]],
) -> None:
    backend = HashEmbeddingBackend(dims=96, seed="search-test")
    hits = hybrid_search(
        query="高频数据",
        chunks=sample_chunks,
        top_k=2,
        filters={"community": "中文论坛"},
        embedding_backend=backend,
    )

    assert [hit.topic_id for hit in hits] == ["t-chn-1"]
    assert hits[0].chunk_id == "t-chn-1#post"
    assert "高频数据" in hits[0].snippet
    assert "<p>" not in hits[0].snippet
    assert hits[0].score > 0


def test_forum_searcher_uses_hybrid_ranking(
    sample_chunks: list[dict[str, str]],
) -> None:
    searcher = ForumSearcher(
        sample_chunks,
        embedding_backend=HashEmbeddingBackend(dims=96, seed="forum-searcher"),
    )
    hits = searcher.search("Sharpe universe test", top_k=2)

    assert hits[0].topic_id == "t-usa-1"
    assert hits[0].lexical_score >= hits[1].lexical_score
