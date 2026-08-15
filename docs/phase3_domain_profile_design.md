# Phase 3 — Stage 0 Document-Domain Profiling (Design)

Status: **DESIGN ONLY — not implemented. Do not write code until reviewed + approved.**
Stable production baseline: `main @ 33d3858` (Phase 1 + Phase 2 image pre-filter).
Author: design draft for review.

## 1. Problem & goal

The image pre-filter (`image_prefilter.py`) decides SKIP / OCR_ONLY / VLM_PROCESSED
in the **images stage**, which runs **before** the router classifies the document.
So it uses **one global** threshold set tuned for finance (skip small decorative
icons). That is wrong for some domains:

- **Finance / legal / policy** — small images are decorative icons → skip is correct.
- **Medical** — a small X-ray/scan thumbnail can be clinically critical → must NOT skip.
- **Engineering** — small symbols carry meaning → must NOT skip.
- **Research** — figures can be small but meaningful → skip only the very tiny.

**Goal:** make the pre-filter **domain-aware** without reordering the ingestion
pipeline and without changing the validated filter algorithm — only *which thresholds*
it uses. Adding a new domain must be a config change, not a code change.

**Non-goals:** changing store routing, the VLM, embeddings, `stored_in`, or the
filter stages themselves. This is purely threshold *selection*.

## 2. Stage 0 responsibilities

A new, cheap **Stage 0** that runs once per document, immediately after parsing and
**before** the images stage. It must:

1. **Determine a document domain** (finance / legal / policy / research / medical /
   engineering / unknown) from cheap signals: the already-parsed first-page text
   (`parsed_doc.raw_text`), the filename, and PDF metadata. No new model; rule-based
   keyword scoring (optionally the existing rule-based router, see §3).
2. **Select a threshold profile** (a set of `PREFILTER_*` overrides) for that domain.
3. **Emit a confidence** for the domain decision.
4. **Be fail-safe**: on low confidence or any error, fall back to the **conservative**
   profile (do NOT pre-skip small images) — never to the aggressive finance profile.
5. **Be observable**: record the chosen domain + confidence + profile on the document
   (and per-image in `image_metadata.prefilter`) for audit.
6. Add ~no latency (operates on text already in memory; pure Python).

Stage 0 does **not** touch routing or stores. Its only output is the profile passed
into `ImagePrefilter(profile=...)`.

## 3. Profile selection flow

```
parse → parsed_doc (raw_text, filename, metadata)
      │
      ▼
Stage 0: detect_domain(text, filename)   ── rule-based keyword scoring
      │        (returns domain + confidence)
      ▼
confidence >= THRESHOLD ?
      ├── no  → profile = CONSERVATIVE  (keep small images)
      └── yes → profile = PROFILES[domain]
      │
      ▼
ImagePrefilter(profile)  →  images stage (Stage 1..5 unchanged, thresholds overridden)
```

A profile is a dict of `PREFILTER_*` overrides merged over the global config. Two
conventions keep it code-free:
- empty profile `{}` → use global config (current behaviour);
- a `0` value → disables that skip (e.g. `PREFILTER_VERY_SMALL_AREA: 0`, since
  `area <= 0` is never true). The universal junk filters (tiny/blank/separator/
  duplicate/corrupted) are **never** disabled by a profile — they are domain-agnostic.

## 4. Interaction with the authoritative router

There are **two classifiers with different purposes**; they must not be conflated:

| | Stage 0 (this design) | Router (`router_service`, existing) |
|---|---|---|
| Purpose | pick pre-filter **thresholds** | pick destination **store** |
| Runs | before images stage | routing stage (after images) |
| Taxonomy | finance/legal/policy/research/**medical**/**engineering**/unknown | financial/legal/research/policy/entity |
| Method | cheap rule-based on first-page text | rule-based + optional Gemma |
| Authority | advisory (thresholds only) | authoritative (storage) |

Recommended: Stage 0 should **reuse the router's rule-based path** where the
taxonomies overlap (finance/legal/research/policy) so the two are consistent, and
**extend** it with medical/engineering keyword sets (which the router does not need).
The router remains the single source of truth for `document_type`/storage; Stage 0
only borrows its rule-based logic to choose thresholds.

## 5. What happens if Stage 0 and the router disagree

Because Stage 0 runs **before** images and the router runs **after**, Stage 0's
decision (which images were skipped) is already final by the time the router runs —
there is no live "conflict" to resolve. The real risk is **Stage 0 being wrong**:

- **Stage 0 too conservative** (e.g. guessed "medical" but it was finance): images
  that should have been skipped went to the VLM. Cost: a few extra VLM calls. **Safe.**
- **Stage 0 too aggressive** (e.g. guessed "finance" but it was medical): a small but
  meaningful image may have been skipped. Cost: **lost content** — the bad case.

Mitigations (chosen so the bad case is rare and detectable):
1. **Confidence-gated aggression** — only apply an *aggressive* (small-image-skip)
   profile on a **high-confidence** finance/legal/policy match; otherwise conservative.
2. **Asymmetric fallback** — unknown/low-confidence → conservative (keep), never finance.
3. **Post-hoc audit signal** — when the router's later `document_type` contradicts the
   Stage 0 domain (e.g. Stage 0=finance, router=research) **and** Stage 0 skipped
   images, log a `prefilter_domain_mismatch` warning with the doc id + skipped count,
   so misroutes are surfaced for review/reprocess. (Detection only — we do not
   un-skip; the images are already gone for this run, but the doc can be reprocessed
   with `PREFILTER_DOMAIN_PROFILES_ENABLED=false` if needed.)

Open question for review: should a mismatch **auto-trigger reprocessing** of that
document with profiles disabled? (Default proposal: no — log + manual decision.)

## 6. Confidence handling and fail-open behaviour

- **Confidence metric**: keyword-hit count per domain; `confidence = top_score`,
  `margin = top_score - second_score`. Require `top_score >= MIN_HITS` (proposed 2)
  AND `margin >= MIN_MARGIN` (proposed 1) to trust an aggressive profile.
- **Below threshold** → `conservative` profile (keep small images).
- **Any exception** in Stage 0 → conservative profile + warning log (never crash the
  ingest; never silently switch to aggressive).
- Mirrors the existing filter's fail-open principle, applied at the *profile* level:
  when unsure, **keep more, skip less**.

## 7. Configuration format for domain profiles

Profiles live as data, not code, so a new domain = a new entry. Proposed: a
`prefilter_profiles` mapping (Python dict initially; promote to a YAML/JSON file
loaded at startup if ops want to edit without a deploy):

```yaml
# domain -> PREFILTER_* overrides (merged over global config; 0 disables a skip)
finance:      { very_small_area: 6000, icon_max_area: 10000 }
legal:        { very_small_area: 6000, icon_max_area: 10000 }
policy:       { very_small_area: 6000, icon_max_area: 10000 }
research:     { very_small_area: 3000, icon_max_area: 5000 }
medical:      { very_small_area: 0,    icon_max_area: 0, lowcomplexity_colors: 0 }
engineering:  { very_small_area: 0,    icon_max_area: 0, lowcomplexity_colors: 0 }
conservative: { very_small_area: 0,    icon_max_area: 0, lowcomplexity_colors: 0 }  # fallback
default:      { }                                                                   # = global config
```

Keys map 1:1 onto existing `PREFILTER_*` settings. Universal junk thresholds
(`MIN_DIM`, `MIN_AREA`, `BLANK_STD`, `DUP_HAMMING`) are intentionally **not**
profile-overridable. Global feature flag: `PREFILTER_DOMAIN_PROFILES_ENABLED`
(default **false** for initial rollout — see §8).

## 8. Migration & rollout plan

Schema: **no migration needed** — Stage 0 only changes which in-memory thresholds the
filter uses; the `image_store` tracking columns from Phase 1/2 already record the
outcome. (Optional, later: persist the chosen `domain`/`confidence` on
`document_registry` for fleet-level analytics — separate migration if desired.)

Rollout (low-risk, staged):
1. **Land behind a flag, default OFF.** With the flag off, behaviour is byte-for-byte
   the current production filter. Zero risk to `main`.
2. **Shadow mode.** Add a mode that *computes* the Stage 0 domain + the profile it
   would use and **logs** it, but still runs the global thresholds. Run over a mixed
   corpus; verify detected domains look right and measure how often aggressive
   profiles would fire. No behaviour change.
3. **Enable for safe domains first.** Turn on real profile application for
   finance/legal/policy/research only (the domains the current global config already
   targets), where the downside is bounded. Validate on a real per-domain PDF set
   (same harness used for Phase 1/2): confirm 0 false skips of informative content.
4. **Add medical/engineering** profiles (these only *reduce* skipping → strictly safer)
   once a small validation corpus exists.
5. **Flip default ON** only after steps 2–4 pass.

Rollback: set `PREFILTER_DOMAIN_PROFILES_ENABLED=false` (instant, no deploy) — reverts
to the current global behaviour.

## 9. Test plan (when approved)

- Unit: `detect_domain` per domain (incl. filename signal) + unknown→conservative;
  `profile_for` mapping incl. `0`-disables; confidence/margin gating.
- Integration: same small image → SKIPPED under finance profile, NOT skipped under
  medical/conservative profile (proves the override path).
- Regression: with flag OFF, the full existing prefilter suite passes unchanged.
- Real-PDF: finance PDF → Stage 0 detects finance → identical 66.7% VLM reduction
  (no regression); a (synthetic or real) medical PDF → small images kept.

## 10. Open questions for review

1. Reuse the router's rule-based classifier, or keep Stage 0's keyword sets separate?
2. On Stage 0 ↔ router mismatch: log-only, or auto-reprocess with profiles off?
3. Profiles as code dict vs an ops-editable YAML/JSON file loaded at boot?
4. Default flag state at GA: ON or OFF?
5. Persist `domain`/`confidence` on `document_registry` for analytics (extra migration)?
