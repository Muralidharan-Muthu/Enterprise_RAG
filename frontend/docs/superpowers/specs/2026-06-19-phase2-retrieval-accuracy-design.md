# Phase 2 — Retrieval Accuracy Strategy (spec + plan)

**Date:** 2026-06-19 · **Branch:** `feat/phase2-retrieval-accuracy` · Phase 2 of 4.

## Problem

`retriever_service.retrieve()` queries each store top-15, concatenates, sorts by **raw cosine distance**, returns. `reranker_service.rerank()` then reranks only `candidates[:12]` (closest by distance). Two accuracy defects:

1. **Cross-store distance is not comparable.** vector chunks, table summaries, clause text, research chunks, and image captions are embedded from different text distributions; their cosine distances live on different scales. Merging by raw distance is unfair, and truncating to top-12-by-distance **starves whole stores** before the cross-encoder (the only fair judge) sees them.
2. **No query intent.** Stores are chosen only from an optional `document_types` filter; otherwise all are searched and merged blindly. The UI's intent→retriever routing is not implemented.

## Design

1. **Balanced candidate pool** — take top-N **per store_type** (default 8) → pool of ≤40, instead of merge-then-truncate-by-distance. Guarantees representation.
2. **Cross-encoder reranks the whole pool** — MiniLM `(query, text)` scores are absolute and comparable across stores, so the reranker IS the fusion. Raise `MAX_RERANK_CANDIDATES` 12→40 (MiniLM ~<1s for 40 pairs at 256 tokens on CPU).
3. **Intent routing** — `intent_service.classify_intent(query)` → `{stores, doc_types, confidence, used_fallback}`. Gemma (JSON) with rule-based keyword fallback. `retrieve()` searches the intent's stores; **ambiguous/low-confidence ⇒ all stores** (recall-safe). Reranker still does final precision.
4. **RRF fallback** — when `use_reranker=False` (or reranker errors), merge per-store ranked lists by Reciprocal Rank Fusion `Σ 1/(k+rank)` (k=60) instead of raw distance.

Non-goals: changing embeddings, new stores, graph/multi-hop (Phase 4), trace deep-links (Phase 3).

## Stores ↔ doc_types

`vector`(policy/entity/financial text), `clause`(legal), `research`(research), `table`(financial), `image`(figures, any). doc_types: policy, financial, legal, entity, research.

## Tasks (TDD, inline)

- **T1 Intent classifier** — `app/services/intent_service.py`: `classify_intent(query)->dict`; pure `_rule_based_intent(query)->dict` (keyword map) + Gemma path with fallback. Unit-test the rule fallback + JSON parse.
- **T2 Balanced pool + RRF helpers** — in `retriever_service.py`: `balanced_pool(results, per_store_cap=8)->list` (interleave top-N per store_type, dedup by chunk_id); `rrf_merge(results, k=60)->list`. Unit-test both with synthetic RetrievedChunks.
- **T3 Raise rerank cap + accept prebuilt pool** — `reranker_service.MAX_RERANK_CANDIDATES=40`; `rerank()` reranks the full given list when it's already a pool (callers pass the balanced pool). Unit-test cap.
- **T4 Wire intent into retrieve()** — `retrieve(query, document_types, document_id, use_intent=True)`: if `document_types` given, keep current behavior; else classify intent → choose stores; ambiguous ⇒ all. Return per-store-tagged results. Unit-test store selection from intent (mock classify).
- **T5 Query route fusion** — `query.py`: build `balanced_pool` → `rerank` (use_reranker) else `rrf_merge`; expose `retrieval_stats` (intent, stores_searched, pool_size). Unit-test the pool→rerank path with mocks.
- **T6 Live check** — real query latency + that all 5 stores can surface; confirm <~2s.

## Acceptance

- A financial/table/image query returns results from those stores even when a text store has many closer-by-raw-distance chunks (no starvation).
- Intent routing narrows stores when confident, falls back to all when not.
- No-reranker path uses RRF, not raw distance.
- Latency stays ≲2s; existing query behavior preserved when `document_types` is passed.
