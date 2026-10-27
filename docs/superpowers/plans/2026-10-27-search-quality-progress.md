# Implementation ledger — plan: 2026-10-27-search-quality.md

The existing Lody workspace is a linked worktree on `lody/18d12279-6d3`.
User authorization: implement the reviewed proposal, coordinate the team, validate final files. No authorization to commit, push, merge or change the original database.

Subsequent user authorization: merge the verified first batch into `main` and push, then continue semantic retrieval, concurrency and accuracy work in the retained worktree. This supersedes the earlier commit/push restriction; it does not authorize including unrelated main-checkout edits or changing the original database.

| Boundary | Producer / consumer | Ruling |
| --- | --- | --- |
| Search → knowledge | Worker A owns search_records; Worker B exposes eligible_knowledge_slugs | Coordinator integrates only after Worker A finishes. |
| Knowledge → FTS | Worker B calls existing index helpers; Worker A may extend candidate helpers | Explicit knowledge writes synchronize only knowledge records, not full forum rebuilds. |
| All → evaluation | Workers own production fixes; Coordinator owns benchmark scripts/fixtures/docs | Separate files; never treat generated target probes as exhaustive human labels. |
| Search internal | Empty queries, bounded fallback, literal lookup, legacy indexes | Preserve return schema and community filters; remove false positives and unbounded Python fallback. |
| Knowledge internal | Evidence versions, review, conflicts and dependencies | Legacy unreviewed evidence is excluded from trusted retrieval; explicit review required. |
| Integration internal | Read-only benchmark over copied SQLite | Same database and source snapshot metadata for before/after. |

## Completed evidence

- Old-source snapshot full baseline: **46 passed in 74.91s**.
- The initial run under inherited FORCE_COLOR produced 10 formatting-related failures; rerunning with FORCE_COLOR/CLICOLOR_FORCE removed, TERM=dumb and NO_COLOR=1 established the clean baseline above. No product changes were made to hide those failures.
- Evaluation regression tests: six failures before the runner existed, then **6 passed**.
- Created a consistent SQLite backup at `/private/tmp/forum-rag-verification.sqlite3`; original database remains untouched.
- Created 60 labeled smoke probes (24 forum, 12 exact, 8 docs, 16 negatives). These are source-derived known targets, not user relevance judgments.

## Active ownership

- Worker A: Lody session `3f52c87a-39f0-4244-95a8-ec8fb32f6b80`, operation `forum-rag-search-implementation-20261027`.
- Worker B: Lody session `c6b95f1c-9a33-4f2b-af17-35e34b3a8f95`, operation `forum-rag-knowledge-implementation-20261027`.
- Coordinator: evaluation script, fixtures, documentation, integration after ownership release.

## Final results

- Worker A delivered search fixes with 17 targeted tests passing. Worker B delivered evidence review, version checks, lifecycle and FTS updates.
- Coordinator integrated eligibility before ranking and allowed only supports/refines backlinks to boost results; three regression tests failed before integration and passed afterward.
- First full integration: 66 passed, 2 failed. Both failures were tests assuming unreviewed auto-publication; updated fixtures explicitly review/publish or inspect drafts. Second run: 68 passed.
- Fresh Reviewer found decoded-comment evidence and mixed-query candidate truncation defects. Worker B fixed decoded content-only matching; Coordinator fixed the owned candidate integration with a failing mixed-query test. Worker B also replaced recursive dependencies after a 1200-page failing reproduction.
- Scoped Reviewer re-review: both findings ADDRESSED, no new important findings or blockers.
- Final full suite: **71 passed in 74.50s**. compileall and git diff --check passed.
- Same-snapshot MCP probes: known targets 41/44 → 44/44, empty-result negatives 0/16 → 16/16. First-call sample p95 12.036s → 0.265s; repeated after p95 0.257s. These are source-derived smoke labels, not general accuracy or cold-start guarantees.
- Real MCP temporary-database lifecycle passed. Real original-database serving passed with unchanged SHA-256, size and mtime.
- Added a `forum-rag-worktree` Lody MCP catalog entry for this checkout. Trusted UI selection is still required; the running Coordinator has not dynamically loaded it.
- Durable evidence and limitations: `docs/search-quality-verification.md` and `docs/evaluation/`.

Ruling: independent model semantic retrieval and query-log-driven ingestion remain explicitly unimplemented directions, consistent with the approved evidence-first approach. No model was downloaded; default hash remains lexical. This release makes no semantic-model quality claim.
