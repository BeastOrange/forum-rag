"""Read-only trust checks for compiled knowledge.

Evidence excerpts establish location, not truth: a reviewer must assess each claim.
"""

from __future__ import annotations

import json
import sqlite3
from collections import deque


def _comment_contains(comments_json: str | None, excerpt: str) -> bool:
    try:
        comments = json.loads(comments_json or "[]")
    except (ValueError, TypeError):
        return False
    if not isinstance(comments, list):
        return False
    return any(
        isinstance(comment, dict)
        and isinstance(comment.get("content_text"), str)
        and excerpt in comment["content_text"]
        for comment in comments
    )


def quality_issues(conn: sqlite3.Connection) -> dict[str, set[str]]:
    def columns(table: str) -> set[str]:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}

    required = {
        "knowledge_pages": {"slug", "status", "review_status"},
        "knowledge_sources": {
            "slug",
            "topic_id",
            "source_content_hash",
            "claim",
            "excerpt",
        },
        "knowledge_links": {"source_slug", "target_slug", "relation_type"},
        "topics": {"topic_id", "content_hash", "title", "body_text", "comments_json"},
    }
    if any(not fields <= columns(table) for table, fields in required.items()):
        return {}
    pages = {
        row[0]: (row[1], row[2])
        for row in conn.execute(
            "SELECT slug, status, review_status FROM knowledge_pages"
        )
    }
    issues = {slug: set() for slug in pages}
    sources: dict[str, int] = {slug: 0 for slug in pages}
    for (
        slug,
        topic,
        snapshot,
        claim,
        excerpt,
        current,
        title,
        body,
        comments,
    ) in conn.execute(
        "SELECT s.slug, s.topic_id, s.source_content_hash, s.claim, s.excerpt, "
        "t.content_hash, t.title, t.body_text, t.comments_json FROM knowledge_sources s "
        "LEFT JOIN topics t ON t.topic_id=s.topic_id"
    ):
        if slug not in issues:
            continue
        sources[slug] += 1
        if current is None:
            issues[slug].add("source_missing")
        elif not snapshot:
            issues[slug].add("source_unreviewed")
        elif snapshot != current:
            issues[slug].add("source_changed")
        if (
            not claim
            or not claim.strip()
            or not excerpt
            or not excerpt.strip()
            or (
                current is not None
                and excerpt not in (title or "")
                and excerpt not in (body or "")
                and not _comment_contains(comments, excerpt)
            )
        ):
            issues[slug].add("missing_support")
    for slug, (status, review) in pages.items():
        if not sources[slug]:
            issues[slug].add("missing_sources")
        if review != "reviewed":
            issues[slug].add("review_required")
    dependencies = {slug: set() for slug in pages}
    for source, target, relation in conn.execute(
        "SELECT source_slug, target_slug, relation_type FROM knowledge_links"
    ):
        if relation == "conflicts_with":
            for slug in (source, target):
                if slug in issues:
                    issues[slug].add("conflict_link_present")
        elif relation == "depends_on" and source in issues:
            dependencies[source].add(target)
    # Resolve only pages grounded in eligible dependencies. Cycles and any pages
    # depending on them remain unresolved, as do missing or ineligible targets.
    candidates = {
        slug
        for slug, (status, _) in pages.items()
        if status == "published" and not issues[slug]
    }
    remaining = {slug: len(dependencies[slug]) for slug in candidates}
    dependents: dict[str, set[str]] = {slug: set() for slug in pages}
    for slug in candidates:
        for target in dependencies[slug]:
            if target in dependents:
                dependents[target].add(slug)
    ready = deque(slug for slug in candidates if remaining[slug] == 0)
    eligible: set[str] = set()
    while ready:
        slug = ready.popleft()
        eligible.add(slug)
        for dependent in dependents[slug]:
            remaining[dependent] -= 1
            if remaining[dependent] == 0:
                ready.append(dependent)
    for slug, targets in dependencies.items():
        if any(target not in eligible for target in targets):
            issues[slug].add("dependency_unreviewed")
    return issues


def eligible_knowledge_slugs(conn: sqlite3.Connection) -> set[str]:
    issues = quality_issues(conn)
    if not issues:
        return set()
    return {
        slug
        for slug, status in conn.execute("SELECT slug, status FROM knowledge_pages")
        if status == "published" and slug in issues and not issues[slug]
    }
