# Independent semantic retrieval and performance plan

**Goal:** Implement actual local model semantic retrieval, verify concurrent behavior, and measure quality without misrepresenting machine-generated labels as human judgments.

**Authorization:** The user requested merging/pushing the preceding fix batch, then continuing in the retained worktree. Commit `3c4a004` is now on `main` and `origin/main`; this phase remains isolated on `lody/semantic-performance` unless separately integrated. Pre-existing main-checkout parser/storage edits remain uncommitted and preserved. The zero-byte `docs` collision is preserved as `docs.pre-forum-rag-20261027`.

## Design and ownership

- Model: first run the registered multilingual MiniLM ONNX model, fixed repository revision and file hashes. It fits the installed FastEmbed 0.8 runtime and supports Chinese/English; E5-small remains a comparison option with mandatory query/passage prefixes. Model preparation is explicit and may download public weights to `/tmp`; queries always load local files and must fail observably rather than silently use hash.
- Independent dense candidates: offline normalized float32 matrix plus record IDs, source fingerprint, model identity and artifact hashes in a versioned side index. NumPy exact dot products/top-k suffice at ~46k chunks. Query-time document encoding and per-vector JSON decoding are forbidden.
- Generations: build a consistent source snapshot, validate shape/finiteness/checksums, atomically publish a pointer. Requests pin a complete generation; stale source/model mismatches are observable and do not leak stale evidence. Never overwrite the original SQLite database.
- Keep existing lexical calls compatible. Expose explicit semantic and hybrid retrieval through one optional MCP/CLI entry. Hybrid uses independent lexical and dense pools with RRF; dense candidates may have zero lexical overlap. Raw cosine thresholds are explicit and model-specific; do not claim uncalibrated thresholds are probabilities.
- Worker A owns `embeddings.py`, new `semantic_index.py`, `semantic_retrieval.py`, `scripts/build_semantic_index.py`, and new semantic tests. Coordinator owns CLI/MCP integration, documentation and final verification. Worker A must not edit existing lexical/indexer/retrieval files without Coordinator agreement.
- Worker B owns `scripts/benchmark_concurrency.py`, `scripts/evaluate_relevance.py`, annotation templates and their dedicated tests. Benchmark copied data only; no editing Worker A files. Coordinator provides the final semantic interface once available.

## Acceptance and review focus

- Failing then passing tests prove independent recall with zero lexical overlap, model mismatch/staleness rejection, atomic generation behavior, deleted records, empty queries and invalid limits.
- Existing 71-test worktree suite and main's 72-test combined suite are clean baselines; preserve literal identifier, Chinese compatibility, error, lock and knowledge trust behavior.
- Run the real local model and a full-corpus index, then prove query embedding only at serving time. Record weights revision/hash, dimension, provider, threads, build throughput, matrix size and runtime.
- Measure concurrency 1/2/4/8, throughput, errors, p50/p95/p99, startup/RSS and response consistency. Include direct and actual MCP callers, competing writes on a disposable copy, and side-index rebuild/switch during reads.
- Profile first; optimize only measured bottlenecks and retain before/after regressions and quality checks. Avoid unversioned result caches or skipping source validity checks.
- Quality: keep development and test labels separate; report Recall@k/MRR/nDCG, negative queries, and lexical/semantic/hybrid ablations. Human-reviewed labels require a real annotator and attestation; pending or Agent-created labels never count as human accuracy. Coordinator has asked for an existing human-labeled dataset and will prepare a review packet if none is available.
- Fresh Reviewer independently checks final code and evidence; repair confirmed issues, repeat targeted checks, then whole-suite verification.

## Progress

- [x] Prior batch merged and pushed after fresh tests; worktree retained.
- [x] Planner and Researcher inspected architecture, runtime, model sources and dataset availability.
- [x] Local model prepared with pinned revision and hashes.
- [x] Model/side index implemented with regression tests.
- [x] CLI/MCP integration and full-corpus runtime evidence.
- [x] Concurrency, profiling and performance corrections.
- [ ] Human label intake/review and accuracy evaluation (requires actual human confirmation).
- [x] Independent review, full checks, final evidence and outstanding limitations.

Final evidence: `docs/semantic-performance-verification.md`. Full suite 105 passed; four review blockers fixed and independently cleared. Human review pool: 14 queries, 158 unjudged query-topic pairs; human accuracy remains pending.
