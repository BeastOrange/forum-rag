# Forum RAG search and knowledge quality implementation plan

> **For agentic workers:** Use test-driven changes and independent final review. The user has approved the preceding investigation and requested coordinated implementation; do not repeat approval or execution-method questions.

**Goal:** Eliminate false search results and avoidable scans, restore Chinese substring recall, and prevent stale or conflicted knowledge from becoming trusted answers.

**Architecture:** Retain SQLite, the existing CLI/MCP contracts, and offline operation. Search uses bounded, relevant candidates and literal identifier lookup. Knowledge records retain evidence versions and expose eligibility checks shared by retrieval and publication.

**Tech Stack:** Python >=3.11, SQLite/FTS5, existing pytest and MCP dependencies.

**Spec:** The current conversation's approved four-part recommendation and the completed Researcher/Planner/Reviewer findings; runtime evidence is `/private/tmp/forum-rag-benchmark.json`.

## Global constraints

- Work only in this isolated worktree; no commits, pushes, publication, or writes to the original database.
- Keep forum, official-document, and compiled-knowledge sources distinct.
- Existing tools and required parameters remain compatible; any additions are optional.
- Do not introduce a downloaded model or external service without measured need.
- Different implementers own disjoint files. Coordinator integrates shared retrieval eligibility afterward.

## Review focus

- Empty or absent identifiers return no irrelevant results, including when FTS is unavailable.
- Chinese-only and mixed queries work against existing indexes without silently rebuilding user data.
- Literal `%`, `_`, community filters, and numeric non-ID searches keep defined semantics.
- Existing conflicts, changed evidence, archived dependencies, and cycles cannot bypass trust filtering.
- Read-only calls do not migrate databases; explicit writes update search visibility.

## Task 1: Search correctness and latency (Worker A)

Files: `indexer.py`, `search.py`, `search_index.py`, `search_records.py`, `search_cache.py`, `embeddings.py`; search/index/CLI-contract regression tests and a new dedicated search-regression test module.

- [ ] Run existing targeted tests and write regression tests reproducing false results, ID scans/literal mismatches, and Chinese partial matches.
- [ ] Observe failures before implementation.
- [ ] Implement bounded candidate retrieval with empty/error/unavailable distinctions; avoid whole-corpus Python scoring on normal misses.
- [ ] Implement ID lookup shortcut preserving community filtering and literal fallback semantics.
- [ ] Unify Chinese candidate normalization and handle existing index compatibility; retain meaningful fallback without irrelevant hash matches.
- [ ] Verify targeted tests; report files, commands, compatibility and remaining limitations.

Interface: existing `search_forum_records`, `search_doc_records`, and `search_knowledge_records` signatures remain usable. Leave knowledge eligibility and relation-aware boost integration to Coordinator.

## Task 2: Knowledge trust and lifecycle (Worker B)

Files: `knowledge.py`, `evolution.py`, `mcp_server.py`, new `knowledge_quality.py`, `tests/test_evolution.py`, new knowledge-quality regression tests. Do not modify Task 1 files.

- [ ] Add failing tests for existing bidirectional conflicts, evidence changes, lifecycle transitions, and immediate FTS visibility.
- [ ] Store evidence content versions on explicit writes with backward-compatible schema handling.
- [ ] Expose `knowledge_quality.eligible_knowledge_slugs(conn) -> set[str]` for trusted published retrieval; legacy evidence without a version requires explicit review before trust.
- [ ] Publication checks must inspect stored conflicts, evidence support/versions and existing dependencies; explicit archive/review tools preserve history and do not automatically rewrite descendants.
- [ ] Add narrowly scoped lifecycle/evidence APIs with optional fields; update FTS on explicit knowledge changes.
- [ ] Verify targeted tests and document API additions for Coordinator.

## Task 3: Integration and evaluation (Coordinator)

Files: retrieval eligibility integration after Task 1 finishes; `scripts/evaluate_search.py`, `tests/fixtures/search_eval.json`, related benchmark tests, README/README_AGENT corrections and verification report.

- [ ] Run full existing suite using installed interpreter with this worktree's `src` on PYTHONPATH; record baseline.
- [ ] Filter trusted knowledge before ranking; exclude conflicts from positive link boosts.
- [ ] Build an inspectable evaluation runner with positive/negative queries, expected topic IDs, recall/ranking and cold/repeated latency. Preserve source snapshot and corpus-size metadata; synthetic regression fixtures are not a human quality benchmark.
- [ ] Compare actual old/new code through MCP against the same copied database, including long-tail misses and exact lookup.
- [ ] Validate Chinese recall, source eligibility and default backend metadata; document optional semantic-model limitation honestly.
- [ ] Run full pytest, diff checks, compilation and relevant MCP integration checks.

## Task 4: Independent review and corrections (fresh Reviewer)

- [ ] Review the final diff, specification and runtime evidence without implementation-session history.
- [ ] Return actionable correctness, compatibility, data-safety and regression findings.
- [ ] Send confirmed findings to implementer or fix within Coordinator-owned files, verify again, then perform scoped re-review.
- [ ] Deliver actual measurements, passed checks, unverified conditions, and remaining model/selection limitations.

Ruling: Existing user approval covers execution of the reviewed proposal; additional skill confirmation gates do not supersede that authorization. SQLite is retained; optional semantic model comparison is an experiment, not a promised automatic download or a reason to block correctness repairs.
