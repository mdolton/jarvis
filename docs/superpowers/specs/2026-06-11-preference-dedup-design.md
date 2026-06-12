# Semantic Deduplication of Memory Preferences

**Date:** 2026-06-11
**Status:** Approved (design)

## Problem

Jarvis proposes new memory **preferences** (behavioral rules like "always run
tests before committing") in the Memory tab for user approval. These suggestions
are frequently near-duplicates of preferences that were already proposed or
approved.

The current dedup is purely lexical: `_normalize_preference` (`memory/service.py`)
lowercases and collapses whitespace, and a unique index on
`memory_preferences.content_normalized` rejects exact matches. Semantically
equivalent but textually different rules — "Always run tests before committing"
vs "Run the test suite before each commit" — normalize differently and both
survive. Over time the approved-preference set fills with redundant rules, and
every one of them is injected into the agent's standing-preferences prompt
section, bloating context.

## Goal

1. **Suggestion-time dedup:** before a candidate preference reaches the Memory
   tab, drop it if it is semantically a duplicate of an existing preference.
2. **Retroactive cleanup:** an on-demand "Find duplicates" tool in the Memory
   dashboard that clusters existing preferences and lets the user archive
   redundant ones.

Detection uses an embedding similarity pre-filter with an LLM tiebreak for the
ambiguous band. All dedup work is best-effort and must never block proposal
creation or approval.

## Decisions (settled during brainstorming)

- **Scope:** preferences only. Memory *entries* (conversation summaries) already
  have semantic dedup via the vector store and are out of scope.
- **Detection:** embedding cosine similarity as a cheap pre-filter; LLM judge
  only for candidates that land in an ambiguous similarity band.
- **Action on detect (suggestion time):** silently drop the duplicate candidate
  (semantic version of today's exact-match drop).
- **Retroactive cleanup:** an on-demand dashboard button that surfaces clusters
  for one-click archiving — nothing is auto-deleted.
- **Embedding storage:** a nullable column on the preference row + in-Python
  cosine, not a parallel sqlite-vec table. The preference set is small and
  clustering needs all-pairs comparison, which a column serves better than a
  k-NN index.

## Architecture

### 1. Data model — migration `0010_preference_embeddings.py`

Add two nullable columns to `memory_preferences`:

- `embedding` — JSON-encoded `list[float]`, the embedding of the preference
  content.
- `embedding_dimensions` — `int`, the vector length at write time.

Both nullable so existing rows migrate cleanly and backfill lazily.
`embedding_dimensions` guards against an embedding-model / dimension change:
any pair whose stored dimensions do not match the current
`memory.embedding_dimensions` config is skipped during comparison.

### 2. New module — `jarvis/memory/preference_dedup.py`

`PreferenceDeduplicator`, dependency-injected with:
- the existing `EmbeddingProvider` (`memory/embeddings.py`),
- an `AsyncOpenAI` client + model string (same construction pattern as
  `MemorySummarizer`),
- a small config object carrying the thresholds and judge-call cap.

The core is pure logic, testable with fakes:

- `cosine(a, b) -> float` — cosine similarity helper.
- `classify(candidate_vec, existing) -> Match | None` — find the max-cosine
  existing preference.
  - `cosine >= high_threshold` → duplicate, `method = "embedding"`.
  - `low_threshold <= cosine < high_threshold` → call `judge` on that single
    best match; judge "yes" → duplicate, `method = "llm"`.
  - `cosine < low_threshold` → not a duplicate.
- `judge(candidate_content, existing_content) -> bool` — a small strict-JSON
  prompt returning `{"duplicate": bool, "reason": str}`. On **any** error it
  returns `False` (conservative: when uncertain, propose rather than silently
  drop, because dropping is the lossy action).
- `cluster(preferences) -> list[Cluster]` — union-find over the preference set:
  connect pairs at `>= high_threshold` directly, connect band pairs via a judge
  call (subject to the per-run cap). Return clusters of size >= 2; each tags a
  suggested **keeper** (the oldest `active` member, else the most-recently
  updated member).

A pair is only compared when both embeddings are present and their
`embedding_dimensions` match the current config.

### 3. Suggestion-time integration — `memory/service.py`

In `_create_preference_proposals`, after the existing exact-normalized filter:

1. Embed each surviving candidate.
2. Compare each candidate against:
   - existing non-archived preferences in **active, pending, and rejected**
     status (rejected included so previously-rejected suggestions are not
     re-proposed), and
   - earlier-accepted candidates within the same batch.
3. Drop duplicates. For candidates that are created, persist `embedding` and
   `embedding_dimensions` on the new rows.
4. Wrap the entire semantic pass in `try/except`. On any failure, fall back to
   today's exact-match-only behavior and still propose the surviving candidates.

`MemoryPreferenceRepo.create_pending_many` is extended to accept and store the
embedding + dimensions alongside each content.

New audit events in `core/types.py`:
- `MEMORY_PREFERENCE_DEDUP_DROPPED` — payload records the matched preference id,
  similarity score, and method (`embedding` | `llm`).
- `MEMORY_PREFERENCE_DEDUP_SKIPPED` — semantic pass failed and fell back to
  exact-match.

### 4. Embedding lifecycle / backfill

- New pending rows are embedded at creation.
- Existing rows backfill lazily: whenever dedup or clustering encounters a row
  with a null embedding, it embeds and persists it.
- The "Find duplicates" action backfills all missing embeddings before
  clustering.

### 5. Dashboard "Find duplicates" — `web/routes/memory.py` + `templates/memory.html`

A button posts to `POST /memory/preferences/find-duplicates`. The handler:
1. Loads all non-archived preferences.
2. Backfills any missing embeddings.
3. Clusters them via `PreferenceDeduplicator.cluster`.
4. Renders each duplicate group (size >= 2) with the suggested keeper
   highlighted and **Archive** buttons on the remaining members.

Archiving reuses the existing `POST /memory/preferences/{id}/archive` endpoint —
no new mutation path is introduced. The action is on-demand and reusable;
nothing is auto-deleted.

### 6. Config — `config/schema.py` `MemoryConfig` + `config/jarvis.yaml.example`

```yaml
preference_dedup_enabled: true
preference_dup_high_threshold: 0.92   # cosine >= -> duplicate outright
preference_dup_low_threshold:  0.82   # cosine in [low, high) -> LLM judge; below -> distinct
preference_dedup_max_judge_calls: 5   # cap LLM judge calls per batch / per clustering run
```

Thresholds are starting estimates and will need tuning against real data.
`preference_dedup_enabled: false` restores exact-match-only behavior (the
`PreferenceDeduplicator` is not constructed and the service skips the semantic
pass).

Pydantic constraints: thresholds `ge=0.0, le=1.0`; `max_judge_calls` `ge=0`.

### 7. Wiring — `main.py`

Construct `PreferenceDeduplicator` from the already-built `embedding_provider`,
`llm_client`, `cfg.jarvis.llm.model`, and the new config fields; pass it into
`MemoryService`. When `preference_dedup_enabled` is false, inject `None` and the
service skips the semantic pass.

### 8. Error handling

Every embedding/LLM call is best-effort and never blocks proposal creation or
approval:
- Dimension mismatch or null embedding → skip that comparison.
- Judge failure → treat as **not** a duplicate.
- Suggestion-time semantic-pass failure → fall back to exact-match, emit
  `MEMORY_PREFERENCE_DEDUP_SKIPPED`.
- Dashboard clustering failure → flash an error to the user, change nothing.

## Testing

- **Unit** — `tests/unit/test_preference_dedup.py`, with fakes for the embedding
  provider and judge:
  - cosine correctness;
  - threshold routing (high → drop, band → judge invoked, low → keep);
  - judge yes/no outcomes;
  - dimension-mismatch comparison skip;
  - judge-error → keep (conservative);
  - clustering union-find grouping, keeper selection, size-`>= 2` filter;
  - judge-call cap is respected.
- **Integration** — extend `tests/integration/test_memory_service.py`:
  - a semantic duplicate of an active preference is dropped;
  - a genuinely new preference survives;
  - a previously-rejected preference is not re-suggested;
  - embedding-provider failure falls back to exact-match and still proposes.
- **Migration** — extend `tests/integration/test_memory_migration.py`: upgrade
  adds both columns as nullable; existing rows survive.
- **Web** — extend the memory web tests: find-duplicates returns clusters;
  archiving a clustered row works through the existing archive endpoint.

## Out of scope

- Deduplication of memory **entries** (conversation summaries).
- Auto-archiving without user confirmation.
- Merging two preferences into a combined rewritten rule (cleanup is
  keep-one / archive-the-rest only).
