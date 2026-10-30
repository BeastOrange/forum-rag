# Author: konrad
# WQ_ID: OB53521
# Encoding: utf-8

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from wq_forum_rag.indexer import raise_if_database_locked
from wq_forum_rag.knowledge import KnowledgeStore, normalize_relation, normalize_slug


class WikiService:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)

    def export_wiki(
        self,
        output_dir: str | Path = ".cache/wiki",
        *,
        include_drafts: bool = False,
    ) -> dict[str, Any]:
        root = Path(output_dir)
        root.mkdir(parents=True, exist_ok=True)
        try:
            with KnowledgeStore(self.db_path, initialize=False) as store:
                pages = [
                    store.get_page(item["slug"])
                    for item in store.list_pages(include_drafts=include_drafts)
                ]
                pages = [page for page in pages if page]
                events = self._recent_events(store)
        except sqlite3.OperationalError as exc:
            raise_if_database_locked(exc, self.db_path)
            raise
        written = []
        for page in pages:
            path = root / f"{page['slug']}.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(self._page_markdown(page), encoding="utf-8")
            written.append(str(path))
        for name, content in {
            "index.md": self._index_markdown(pages),
            "log.md": self._log_markdown(events),
            "hot.md": self._hot_markdown(pages, events),
        }.items():
            path = root / name
            path.write_text(content, encoding="utf-8")
            written.append(str(path))
        return {"output_dir": str(root), "page_count": len(pages), "written": written}

    def graph_query(
        self,
        slug: str,
        *,
        depth: int = 1,
        relation_type: str | None = None,
    ) -> dict[str, Any]:
        start = normalize_slug(slug)
        relation = normalize_relation(relation_type) if relation_type else None
        seen = {start}
        frontier = {start}
        edges: list[dict[str, Any]] = []
        try:
            with KnowledgeStore(self.db_path, initialize=False) as store:
                for _ in range(max(depth, 0)):
                    next_frontier: set[str] = set()
                    for node in sorted(frontier):
                        for edge in self._neighbor_edges(store, node, relation):
                            edges.append(edge)
                            for candidate in (edge["source_slug"], edge["target_slug"]):
                                if candidate not in seen:
                                    seen.add(candidate)
                                    next_frontier.add(candidate)
                    frontier = next_frontier
                    if not frontier:
                        break
                nodes = [
                    store.get_page(node) or {"slug": node, "missing": True}
                    for node in sorted(seen)
                ]
        except sqlite3.OperationalError as exc:
            raise_if_database_locked(exc, self.db_path)
            raise
        return {"start": start, "depth": depth, "nodes": nodes, "edges": edges}

    def _neighbor_edges(
        self,
        store: KnowledgeStore,
        slug: str,
        relation_type: str | None,
    ) -> list[dict[str, Any]]:
        if not store._table_exists("knowledge_links"):
            return []
        params: list[Any] = [slug, slug]
        relation_sql = ""
        if relation_type:
            relation_sql = " AND relation_type = ?"
            params.append(relation_type)
        rows = store.conn.execute(
            f"""
            SELECT * FROM knowledge_links
            WHERE (source_slug = ? OR target_slug = ?){relation_sql}
            ORDER BY relation_type, source_slug, target_slug
            """,
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    def _recent_events(
        self, store: KnowledgeStore, limit: int = 50
    ) -> list[dict[str, Any]]:
        rows = store.conn.execute(
            """
            SELECT * FROM knowledge_events
            ORDER BY event_id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def _page_markdown(self, page: dict[str, Any]) -> str:
        lines = [
            "---",
            f"slug: {page['slug']}",
            f"status: {page['status']}",
            f"confidence: {page['confidence']:.3f}",
            f"updated_at: {page['updated_at']}",
            "---",
            "",
            f"# {page['title']}",
            "",
            page["summary"],
            "",
            "## Body",
            "",
            page["body"],
            "",
            "## Sources",
        ]
        lines.extend(f"- forum topic `{item['topic_id']}`" for item in page["sources"])
        lines.extend(["", "## Links"])
        lines.extend(
            f"- {item['relation_type']}: [[{item['target_slug']}]]"
            for item in page["links"]
        )
        lines.extend(["", "## Backlinks"])
        lines.extend(
            f"- [[{item['source_slug']}]]: {item['relation_type']}"
            for item in page["backlinks"]
        )
        return "\n".join(lines).strip() + "\n"

    def _index_markdown(self, pages: list[dict[str, Any]]) -> str:
        lines = ["# Knowledge Index", ""]
        for page in sorted(pages, key=lambda item: item["slug"]):
            lines.append(f"- [[{page['slug']}]] - {page['title']} ({page['status']})")
        return "\n".join(lines).strip() + "\n"

    def _log_markdown(self, events: list[dict[str, Any]]) -> str:
        lines = ["# Knowledge Log", ""]
        for event in events:
            lines.append(
                f"- {event['created_at']} `{event['slug']}` {event['event_type']}: {event['note']}"
            )
        return "\n".join(lines).strip() + "\n"

    def _hot_markdown(
        self, pages: list[dict[str, Any]], events: list[dict[str, Any]]
    ) -> str:
        lines = ["# Hot Snapshot", "", "## Recently Updated"]
        for page in sorted(pages, key=lambda item: item["updated_at"], reverse=True)[
            :10
        ]:
            lines.append(f"- [[{page['slug']}]] - {page['summary']}")
        lines.extend(["", "## Recent Events"])
        for event in events[:10]:
            lines.append(f"- `{event['slug']}` {event['event_type']}")
        return "\n".join(lines).strip() + "\n"
