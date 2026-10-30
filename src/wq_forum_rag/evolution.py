# Author: konrad
# WQ_ID: OB53521
# Encoding: utf-8

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from wq_forum_rag.indexer import (
    ForumDatabaseBusyError,
    ForumIndexService,
    raise_if_database_locked,
)
from wq_forum_rag.knowledge import (
    DRAFT,
    PUBLISHED,
    KnowledgePage,
    KnowledgeStore,
    normalize_slug,
)
from wq_forum_rag.knowledge_quality import quality_issues
from wq_forum_rag.search_cache import CachedEmbeddingBackend
from wq_forum_rag.search_records import search_forum_records, search_knowledge_records
from wq_forum_rag.storage import ForumStore
from wq_forum_rag.wiki import WikiService

AUTO_PUBLISH_CONFIDENCE = 0.85


class EvolutionService:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)

    def build_evolution_context(self, query: str, top_k: int = 5) -> dict[str, Any]:
        self._ensure_forum_fresh()
        knowledge = self.search_knowledge(
            query, top_k=min(top_k, 5), include_drafts=False
        )
        forum = self.search_forum(query, top_k=top_k)
        posts = [self.get_post(item["topic_id"]) for item in forum]
        return {
            "query": query,
            "instructions": (
                "Use published_knowledge first, then forum_evidence. "
                "Call propose_knowledge_page with source_topic_ids that support each key claim."
            ),
            "published_knowledge": knowledge,
            "forum_evidence": [post for post in posts if post],
        }

    def propose_knowledge_page(
        self,
        *,
        slug: str,
        title: str,
        summary: str,
        body: str,
        source_topic_ids: list[str],
        confidence: float,
        links: list[dict[str, Any]] | None = None,
        auto_publish: bool = True,
        source_evidence: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        self._ensure_forum_fresh()
        page = KnowledgePage(
            slug=slug,
            title=title,
            summary=summary,
            body=body,
            status=DRAFT,
            confidence=confidence,
        )
        unique_sources = sorted(
            {str(topic_id) for topic_id in source_topic_ids if str(topic_id).strip()}
        )
        with KnowledgeStore(self.db_path) as store:
            if not store.topic_ids_exist(unique_sources):
                raise ValueError(
                    "source_topic_ids must reference existing forum topics"
                )
            issues = self._lint_page_payload(page, unique_sources, links or [])
            # A proposal always requires a separate explicit review.
            issues.append(self._issue(page.slug, "block", "review_required"))
            existing = store.get_page(page.slug)
            if existing and any(
                link["relation_type"] == "conflicts_with"
                for link in existing["links"] + existing["backlinks"]
            ):
                if not any(
                    issue["code"] == "conflict_link_present" for issue in issues
                ):
                    issues.append(
                        self._issue(page.slug, "block", "conflict_link_present")
                    )
            stored = store.upsert_page(
                page,
                source_topic_ids=unique_sources,
                links=links or [],
                source_evidence=source_evidence,
                event_type="proposed",
                event_note="auto-published" if page.status == PUBLISHED else "drafted",
            )
            return {
                "page": store.get_page(stored.slug),
                "issues": issues,
                "auto_published": stored.status == PUBLISHED,
            }

    def publish_knowledge_page(self, slug: str) -> dict[str, Any]:
        with KnowledgeStore(self.db_path) as store:
            # Acquire a write lock before checking, so evidence and links cannot change
            # between validation and publication.
            store.conn.execute("BEGIN IMMEDIATE")
            page = store.get_page(slug)
            if page is None:
                return {
                    "published": False,
                    "slug": slug,
                    "issues": [self._issue(slug, "block", "missing_page")],
                }
            codes = quality_issues(store.conn).get(page["slug"], {"review_required"})
            blockers = [
                self._issue(page["slug"], "block", code) for code in sorted(codes)
            ]
            if blockers:
                return {"published": False, "slug": slug, "issues": blockers}
            published = store.set_status(slug, PUBLISHED, "manual publish")
            store.conn.commit()
            return {"published": True, "page": published}

    def review_knowledge_page(
        self,
        slug: str,
        action: str,
        *,
        note: str,
        replacement_slug: str | None = None,
    ) -> dict[str, Any]:
        if action not in {"reviewed", "needs_review", "archived"}:
            raise ValueError("action must be reviewed, needs_review, or archived")
        if not note.strip():
            raise ValueError("a review or archive note is required")
        with KnowledgeStore(self.db_path) as store:
            store.conn.execute("BEGIN IMMEDIATE")
            page = store.get_page(slug)
            if page is None:
                raise ValueError("knowledge page does not exist")
            if replacement_slug:
                replacement_slug = normalize_slug(replacement_slug)
                if (
                    action != "archived"
                    or replacement_slug == page["slug"]
                    or not store.page_exists(replacement_slug)
                ):
                    raise ValueError(
                        "replacement must be another existing page for an archive"
                    )
            if action == "reviewed":
                issues = quality_issues(store.conn).get(
                    page["slug"], {"missing_support"}
                )
                blockers = issues - {"review_required"}
                if blockers:
                    raise ValueError(
                        "cannot review page: " + ", ".join(sorted(blockers))
                    )
            store.conn.execute(
                "UPDATE knowledge_pages SET review_status = ?, updated_at = CURRENT_TIMESTAMP WHERE slug = ?",
                ("reviewed" if action == "reviewed" else "needs_review", page["slug"]),
            )
            if action == "archived":
                if replacement_slug:
                    store.link_pages(page["slug"], replacement_slug, "superseded_by")
                store.set_status(
                    page["slug"],
                    "archived",
                    note
                    + (f"; replacement:{replacement_slug}" if replacement_slug else ""),
                )
            else:
                store.add_event(page["slug"], f"review:{action}", note)
                store._sync_fts(page["slug"])
            store.conn.commit()
            return store.get_page(page["slug"])

    def get_knowledge_page(self, slug: str) -> dict[str, Any] | None:
        try:
            with KnowledgeStore(self.db_path, initialize=False) as store:
                return store.get_page(slug)
        except sqlite3.OperationalError as exc:
            raise_if_database_locked(exc, self.db_path)
            raise

    def link_knowledge_pages(
        self,
        source_slug: str,
        target_slug: str,
        relation_type: str,
        *,
        weight: float = 1.0,
        confidence: float = 0.8,
    ) -> dict[str, Any]:
        with KnowledgeStore(self.db_path) as store, store.conn:
            return store.link_pages(
                source_slug,
                target_slug,
                relation_type,
                weight=weight,
                confidence=confidence,
            )

    def search_knowledge(
        self,
        query: str,
        top_k: int = 5,
        *,
        include_drafts: bool = False,
    ) -> list[dict[str, Any]]:
        self._ensure_forum_fresh()
        with ForumStore(self.db_path, initialize=False) as store:
            return search_knowledge_records(
                store.conn,
                query,
                top_k=top_k,
                include_drafts=include_drafts,
                embedding_backend=CachedEmbeddingBackend(self.db_path, writable=False),
            )

    def search_forum(self, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        self._ensure_forum_fresh()
        with ForumStore(self.db_path, initialize=False) as store:
            return search_forum_records(
                store.conn,
                query,
                top_k=top_k,
                embedding_backend=CachedEmbeddingBackend(self.db_path, writable=False),
            )

    def get_post(self, topic_id: str) -> dict[str, Any] | None:
        self._ensure_forum_fresh()
        with ForumStore(self.db_path, initialize=False) as store:
            topic = store.get_topic(topic_id)
        if topic is None:
            return None
        payload = topic.to_dict()
        payload["topic_id"] = payload.get("id", topic_id)
        return payload

    def lint_knowledge(self, slug: str | None = None) -> dict[str, Any]:
        try:
            with KnowledgeStore(self.db_path, initialize=False) as store:
                quality = quality_issues(store.conn)
                pages = (
                    [store.get_page(slug)]
                    if slug
                    else [
                        store.get_page(page["slug"])
                        for page in store.list_pages(include_drafts=True)
                    ]
                )
        except sqlite3.OperationalError as exc:
            raise_if_database_locked(exc, self.db_path)
            raise
        issues: list[dict[str, Any]] = []
        for page in [item for item in pages if item]:
            page_issues = self._lint_page_record(page)
            existing_codes = {item["code"] for item in page_issues}
            page_issues.extend(
                self._issue(page["slug"], "block", code)
                for code in sorted(
                    quality.get(page["slug"], {"review_required"}) - existing_codes
                )
            )
            issues.extend(page_issues)
        return {
            "slug": slug,
            "issues": issues,
            "blocking_count": sum(1 for item in issues if item["severity"] == "block"),
            "warning_count": sum(1 for item in issues if item["severity"] == "warn"),
        }

    def graph_query(
        self, slug: str, *, depth: int = 1, relation_type: str | None = None
    ) -> dict[str, Any]:
        return WikiService(self.db_path).graph_query(
            slug, depth=depth, relation_type=relation_type
        )

    def export_wiki(
        self,
        output_dir: str | Path = ".cache/wiki",
        *,
        include_drafts: bool = False,
    ) -> dict[str, Any]:
        return WikiService(self.db_path).export_wiki(
            output_dir, include_drafts=include_drafts
        )

    def _ensure_forum_fresh(self) -> None:
        refresh = ForumIndexService(self.db_path).auto_refresh_if_needed()
        if refresh.get("status") == "locked":
            raise ForumDatabaseBusyError(refresh)

    def _lint_page_payload(
        self,
        page: KnowledgePage,
        source_topic_ids: list[str],
        links: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        issues: list[dict[str, Any]] = []
        if not source_topic_ids:
            issues.append(self._issue(page.slug, "block", "missing_sources"))
        if page.confidence < AUTO_PUBLISH_CONFIDENCE:
            issues.append(self._issue(page.slug, "warn", "low_confidence"))
        if any(link.get("relation_type") == "conflicts_with" for link in links):
            issues.append(self._issue(page.slug, "block", "conflict_link_present"))
        if len(page.summary) < 20 or len(page.body) < 40:
            issues.append(self._issue(page.slug, "warn", "thin_content"))
        return issues

    def _lint_page_record(self, page: dict[str, Any]) -> list[dict[str, Any]]:
        issues: list[dict[str, Any]] = []
        if not page["sources"]:
            issues.append(self._issue(page["slug"], "block", "missing_sources"))
        if float(page["confidence"]) < AUTO_PUBLISH_CONFIDENCE:
            issues.append(self._issue(page["slug"], "warn", "low_confidence"))
        links = page["links"] + page["backlinks"]
        if any(item["relation_type"] == "conflicts_with" for item in links):
            issues.append(self._issue(page["slug"], "block", "conflict_link_present"))
        if not links:
            issues.append(self._issue(page["slug"], "warn", "orphan_page"))
        return issues

    @staticmethod
    def _issue(slug: str, severity: str, code: str) -> dict[str, str]:
        return {"slug": slug, "severity": severity, "code": code}
