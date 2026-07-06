# P2 — AI Knowledge Graph for Context-Aware Governance: PR descriptions

This repo builds two separable deliverables: the P2 satellite (deploys on its
own) and a core-side patch set (`core-side-patch/`, lands in the separate
CompliVibe core backend repo — not available in this workspace, so it's
built here as a self-contained, honestly-flagged patch set; see
`core-side-patch/ASSUMPTIONS.md`). Below is a PR description per workstream,
written as if each were its own PR against its respective repo.

> **Update (production-hardening pass):** PR 7 below covers a follow-up pass
> hardening failure modes, observability, security, and performance across
> everything described in PRs 1-6. It changes the event webhook's wire
> format (breaking, nothing in production depended on the old one) and adds
> a new batch ingest endpoint — see PR 7 for details before integrating PRs
> 4/1's webhook/ingest pieces.

Full repo test count (after the hardening pass): **180 passed** (`pytest`
at repo root, plus `pytest core-side-patch/tests/`) — see PR 7 for the
breakdown of what's new. Original workstream test count: **122 passed** (`pytest` at repo root — 70 satellite
unit tests, 2 live-HTTP integration tests, 10 benchmark tests, 40 core-patch
tests run via `pytest core-side-patch/tests`).

---

## PR 1 — Core: add governance graph tables, patent-export/ingest endpoints (Workstream A)

**Targets:** CompliVibe core backend (`app.complivibe.in`), lands on top of
migration head **0175** → new revision `0176_governance_graph`
(`down_revision` is a placeholder for "0175" — replace with the real head id
from `alembic heads` before merging; see `core-side-patch/MERGE_CHECKLIST.md` §1).

**What this adds:**
- Alembic migration creating `governance_graph_nodes`, `governance_graph_edges`,
  `governance_graph_traversal_results` per PATENT.md, including a pgvector
  `Vector(384)` embedding column.
- Two new scoped, non-human permissions: `patent_export:p2:read`,
  `patent_ingest:p2:write`.
- Three read-only export endpoints (`/api/v1/patent-exports/p2/{ai-systems,
  regulations-catalog,jurisdictions}`) with `changed_since` filtering.
- One ingest endpoint (`/api/v1/patent-ingest/p2/obligation-derivation`)
  implementing PATENT.md's mandatory "Satellites Compute, Core Decides"
  contract in full: catalog re-validation (422 on unknown/inactive ids),
  independent re-derivation via the literal reference CTE, mismatch flagging
  (no silent overwrite), and unconditional audit logging.
- An outbox-style change-event table + emitter for the three watched
  `ai_system` fields (not yet wired to the real update path — TODO for a
  human with access to that code).

**Why self-contained rather than a real diff against core:** the core repo
isn't present in this workspace. Every file is labeled with the assumptions
it had to make (exact `ai_system` field names, `AuditService` signature,
whether an outbox already exists, permission-registry shape) — see
`ASSUMPTIONS.md` (14 items) before merging.

**Tests:** 40 passing (`pytest core-side-patch/tests/`) — migration structure
sanity, auth scoping (401/403), 422 on bad ids, the flagged-mismatch path
proving no `ai_system_obligation_links` write on disagreement, and the
happy path proving links + audit log on agreement.

**Before merge:** read `ASSUMPTIONS.md` and `MERGE_NOTES.md` in full;
`MERGE_CHECKLIST.md` at repo root synthesizes both into one actionable list.

---

## PR 2 — Satellite: graph construction + embeddings (Workstream B)

**Targets:** this repo, `src/p2_satellite/graph_builder.py` + `embeddings.py`.

**What this adds:** an httpx client (tenacity retries, 5xx/timeout only) that
pulls the three core export endpoints, plus a pure `build_graph()` function
that turns the three response payloads into a `networkx.DiGraph` using the
canonical node/edge schema (`src/p2_satellite/schema.py`). `embeddings.py`
wraps `sentence-transformers`/`all-MiniLM-L6-v2` (384-dim) for semantic node
matching, with the model lazily loaded so tests never hit HuggingFace.

**Tests:** 19 + 5 + 10 = 34 tests — node/edge assertions against the shared
fixture, a structural-equivalence check against the independently-built
`tests/fixtures/graph_from_export.py`, and embedding shape/determinism tests
using an injected stub encoder.

**Depends on:** nothing (pure/fetching code, mocked in tests).

---

## PR 3 — Satellite: traversal engine (Workstream C)

**Targets:** this repo, `src/p2_satellite/traversal.py`.

**What this adds:** `derive_obligations()` — an iterative (non-recursive)
NetworkX BFS/DFS mirroring PATENT.md's reference recursive CTE exactly,
including its path-based cycle guard and its `MAX_TRAVERSAL_DEPTH` bound
(read once from `settings.max_traversal_depth`, never hardcoded).

**The load-bearing test:** `tests/unit/test_traversal.py` cross-checks this
implementation byte-for-byte against `tests/fixtures/reference_cte.py` (an
independent SQLite/JSON1 port of the literal Postgres CTE) — this is what
makes the "cross-validated by an independent core-side re-derivation" claim
in PATENT.md's "Novel Patent Claim" actually true rather than asserted.
Also covers depth-limiting (proving the config bound is real) and cycle
safety on a synthetic cyclic graph.

**Tests:** 11 passing.

---

## PR 4 — Satellite: hybrid trigger system (Workstream D)

**Targets:** this repo, `src/p2_satellite/{event_listener,scheduler,ingest_client}.py`.

**What this adds:**
- `event_listener.py`: FastAPI app, HMAC-verified (`X-P2-Signature: sha256=...`,
  constant-time compare) webhook receiver for change events, triggering an
  immediate traversal + push via `BackgroundTasks`.
- `scheduler.py`: APScheduler job re-traversing every ai_system every
  `SAFETY_NET_POLL_HOURS` (default 2), tagged `trigger_reason="scheduled"`.
  Started from `event_listener.py`'s lifespan — one process, not two.
- `ingest_client.py`: pushes to core's ingest endpoint with retries (3
  attempts, 5xx/timeout only) and an idempotency hash (sha256 of
  `{ai_system_id, derived_obligations, derived_controls, methodology_version}`,
  sent as both a body field and `Idempotency-Key` header) so re-pushing an
  unchanged derivation is safe.

**Language check:** no file describes the trigger model as "real-time"
anywhere — the CHANGE LOG's locked language ("event-triggered derivation
with periodic reconciliation") is followed throughout.

**Tests:** 23 passing.

---

## PR 5 — Patent evidence: EU-India biometric benchmark (Workstream E)

**Targets:** this repo, `tests/benchmark/`.

**What this proves** (PATENT.md's "Required Evidence Before Filing"): on a
synthetic AI system modeling the exact novel combination PATENT.md
describes — biometric, joint EU-India deployment, dual purpose (employment
screening + healthcare), split controller/processor role, high-risk —

- A naive static lookup table (representative of PATENT.md's own described
  failure pattern, not a strawman) returns **1 of 11 owed obligations**
  (a 91% miss rate), silently — no error, just a plausible-looking
  incomplete answer.
- Graph traversal (`build_graph()` + `derive_obligations()`, unmodified from
  Workstreams B/C) returns the complete, correctly role-tagged set spanning
  all three regulations (GDPR, EU AI Act high-risk additions, DPDP) and both
  controller- and processor-specific obligations.

**Disclosed limitation:** `build_graph()` doesn't yet ingest ai-system
`properties` into the graph itself; the controller/processor role split is
demonstrated via obligation-level `applies_to_role` tags (fully
graph-driven) rather than node properties. Flagged in
`PATENT_TECHNICAL_EFFECT.md` §4, not hidden.

**Tests:** 10 passing, including a drift guard that reconstructs the
expected obligation set from the raw catalog data rather than trusting a
hand-typed constant.

**Reproduce:** `pip install -r requirements.txt && pytest tests/benchmark/`
— see `tests/benchmark/REPRODUCIBILITY.md`.

---

## PR 6 — Integration, guard rail, and merge documentation (Workstream F)

**Targets:** this repo, `README.md`, `scripts/check_no_core_imports.sh`,
`tests/unit/test_no_core_imports_guard.py`, `MERGE_CHECKLIST.md`.

**What this adds:**
- A rewritten `README.md` describing the satellite's deployment shape (one
  process — FastAPI event listener + in-process APScheduler), its env vars,
  and exactly how it's wired to core (verified against the actual code, not
  guessed).
- A grep-based CI guard (`scripts/check_no_core_imports.sh`) enforcing that
  nothing under `src/p2_satellite/` ever imports `app.*`, with a test proving
  both that it currently passes AND that it correctly catches a deliberately
  planted violation.
- `MERGE_CHECKLIST.md`, synthesizing `core-side-patch/ASSUMPTIONS.md` and
  `MERGE_NOTES.md` into one actionable go-live checklist (migration order,
  permission seeding, env vars split by side, rollback plan, known gaps).

**Tests:** 2 new (the import guard), 112 → 122 full-repo pass count after
Workstream E landed alongside this one.

---

## Integration checkpoints (validated centrally, not by any single workstream)

1. **B+C fixture agreement:** `tests/unit/test_traversal.py` cross-checks
   `traversal.py` against `tests/fixtures/reference_cte.py` byte-for-byte;
   `tests/unit/test_graph_builder_matches_fixture.py` cross-checks
   `graph_builder.build_graph()` against the independently-built
   `tests/fixtures/graph_from_export.py`. Both pass.
2. **A+D end-to-end dry run:** `tests/integration/test_end_to_end_dry_run.py`
   boots a real uvicorn server hosting Workstream A's actual FastAPI routers,
   points the satellite's real `graph_builder`/`traversal`/`ingest_client` at
   it over live HTTP (no mocks), and confirms: the derivation round-trips,
   core's independent re-derivation agrees (`validation_status="validated"`),
   the obligation links are written, exactly one traversal-result row and one
   audit-log entry are produced, and re-pushing computes an identical
   idempotency hash. 2 tests, both passing.

## Definition of done — status (original 6 workstreams)

- [x] `pytest` passes across all workstreams, including the benchmark (122/122).
- [x] No satellite file imports core code (`scripts/check_no_core_imports.sh` passes; enforced by a test).
- [x] PATENT.md's "Required Evidence Before Filing" is satisfied by Workstream E's output.
- [x] A short PR description per workstream (above), noting the core migration
      number (`0175` → new `0176_governance_graph`, placeholder pending the
      real head id).

---

## PR 7 — Production-hardening pass (failure modes, observability, security, performance)

**Targets:** both repos — satellite (`src/p2_satellite/`) and core patch
(`core-side-patch/`). Ran as three parallel sub-passes by file ownership
(export/graph/traversal; trigger path; core-side security/scale), followed
by a sequential batching + performance pass. **Breaking change:** the event
webhook's signature header format changed (see below) — nothing in
production depends on the old format yet, so this is safe to land as part
of the same PR as PRs 1/4 rather than a separate migration.

### What changed, by area

**Failure-mode hardening:**
- `graph_builder.fetch_and_build_graph()` now raises a typed
  `GraphBuildIncompleteError` (naming which of the three export steps
  failed) instead of an undifferentiated httpx exception — it already
  structurally could not return a partial graph; this just makes the
  failure unambiguous to callers/logs.
- `traversal.derive_obligations()` gained a new `incomplete_coverage` key
  distinguishing "this regulation has zero obligations seeded yet" (a data
  gap) from "this ai_system genuinely triggers nothing" (a correct empty
  result) — additive, does not change `derived_obligations`/`derived_controls`.
- `ingest_client.push_derivation()`'s retry/idempotency behavior was
  proven correct with new explicit tests (mid-push connection failure +
  retry, hash stability across retries and repeat calls) rather than just
  assumed.
- The event webhook (`event_listener.py`) now requires a timestamped,
  replay-resistant signature: `X-P2-Signature: t=<unix_epoch>,v1=sha256=<hex
  hmac over "{t}." + raw body>`, rejecting stale timestamps
  (`EVENT_WEBHOOK_MAX_CLOCK_SKEW_SECONDS`, default 300s) and exact replays
  of an already-seen `(t, signature)` pair. The OLD format
  (`sha256=<hex over body alone>`) is no longer accepted.
- A new per-`ai_system_id` non-blocking lock (`src/p2_satellite/concurrency.py`)
  prevents the scheduler's safety-net sweep and an event-triggered
  derivation from racing on the same system — the loser skips and logs
  rather than both proceeding undefined.
- Core's ingest route (`patent_ingest_p2.py`) now emits a WARNING-level
  structured log AND records every outcome in a new
  `mismatch_metrics.MismatchMetrics` counter, so a validation mismatch is
  visible without querying raw `governance_graph_traversal_results` rows.

**Security:**
- Scope isolation (`patent_export:p2:read` vs `patent_ingest:p2:write`)
  confirmed already fully correct in both directions — no gap found, no
  change needed (see `core-side-patch/tests/test_core_patch_ingest_router.py`
  / `test_core_patch_exports_router.py`).
- New `core-side-patch/rate_limiter.py`: a per-scoped-key fixed-window
  limiter (default 100 derivations/60s, single-process stopgap — see
  `ASSUMPTIONS.md` item 16) on the ingest route(s), returning `429` when
  exceeded.
- New optional `EVENT_LISTENER_IP_ALLOWLIST` on the webhook endpoint
  (defense-in-depth alongside HMAC, disabled by default).
- Confirmed via grep + tests that neither the HMAC secret nor either scoped
  API key is ever logged, even at DEBUG level, in any file that handles
  them — `observability.install_secret_redaction()` is attached as a
  backstop everywhere a secret could plausibly leak.

**Performance (new, this PR):**
- New `POST /api/v1/patent-ingest/p2/obligation-derivations/batch` route
  (core) + `ingest_client.push_derivations_batch()` (satellite) — one HTTP
  round-trip for N derivations instead of N. Same per-item validation
  contract as the single-item route (shared `_process_one_derivation`
  helper); one bad item never fails the rest of a batch.
- `scheduler.py`'s safety-net sweep now chunks a large fleet's sweep into
  groups of `INGEST_BATCH_CHUNK_SIZE` (default 50), pacing
  `INGEST_BATCH_PACE_SECONDS` (default 30s) between chunks, specifically so
  an unchunked burst never instantly exceeds core's per-key ingest rate
  limit — this was a real gap caught and fixed during this same pass (an
  unchunked 10,000-item batch would have 429'd every single sweep).
- New pgvector HNSW index on `governance_graph_nodes.embedding`
  (`postgresql_ops={"embedding": "vector_cosine_ops"}`) — previously
  unindexed (full table scan on every similarity search).
- Measured (not assumed) graph-build + traversal time at 1,000 and 10,000
  synthetic AI systems: **0.149s combined at N=1,000**, **1.569s combined
  at N=10,000** — see `PERFORMANCE.md` for full methodology and numbers.
  Export pulls were already batched (one HTTP call per resource type, no
  pagination loop); no change needed there.

**New/changed config (satellite, additive except the webhook format):**
`EVENT_WEBHOOK_MAX_CLOCK_SKEW_SECONDS`, `EVENT_LISTENER_IP_ALLOWLIST`,
`INGEST_BATCH_CHUNK_SIZE`, `INGEST_BATCH_PACE_SECONDS` — all documented in
`.env.example`.

**Documentation:** `README.md` gained an "Observability" section and a
"Failure modes" table (condition → behavior → test, for every failure mode
above); new `RUNBOOK.md` (validation-mismatch response, scheduler-falling-
behind diagnosis, manual single-system re-trigger, webhook rejection
triage, 429 triage); new `PERFORMANCE.md`.

**Tests:** 122 → **180 passed**, all new tests proving behavior rather than
asserting it works: `tests/unit/test_graph_builder_failure_modes.py`,
`tests/unit/test_traversal_coverage_gaps.py`, `tests/unit/test_concurrency.py`,
updated `tests/unit/test_event_listener.py`/`test_scheduler.py`/
`test_ingest_client.py`, new `tests/unit/test_ingest_client_batch.py`,
`core-side-patch/tests/test_core_patch_mismatch_metrics.py`,
`test_core_patch_rate_limiter.py`, `test_core_patch_ingest_batch.py`, updated
`test_core_patch_migration.py`, and a new `tests/performance/test_scale_smoke.py`
regression guard.

**Explicitly out of scope for this pass** (per the hardening prompt):
resolving `core-side-patch/ASSUMPTIONS.md`'s existing gaps (real core field
names, real `AuditService` signature, etc.) — still gated on real core repo
access.

## Definition of done — status (hardening pass)

- [x] All new tests pass alongside the original 122 (180/180 total).
- [x] Every failure mode has an explicit test proving correct behavior (see
      README.md's "Failure modes" table for the condition → test mapping).
- [x] Validation mismatch visibility exists where a human will see it
      (WARNING log + `MismatchMetrics`, not just a DB column).
- [x] Performance numbers at 1,000/10,000 systems are documented
      (`PERFORMANCE.md`), not assumed — and were sub-second at 1,000
      (0.149s), so no profiling/fixing was required.
- [x] `RUNBOOK.md` exists, covering the five most likely 2am scenarios this
      pass's own failure-mode work surfaced.
