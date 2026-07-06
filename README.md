# P2 -- AI Knowledge Graph for Context-Aware Governance

This repo is the **satellite implementation for CompliVibe patent P2**
(`complivibe-patent-p2-knowledge-graph`). Most GRC platforms decide which
compliance obligations apply to an AI system with a static lookup table:
"deployed in the EU + processes health data => GDPR + EU AI Act." That
approach works for combinations the platform authors anticipated and
silently breaks for the ones they didn't -- e.g. a biometric system with a
joint EU-India deployment, dual purpose (employment screening and
healthcare), and a split controller/processor role. This satellite instead
builds a property graph out of an organization's AI systems, regulations,
jurisdictions, data categories, control types, obligations, and risk tiers,
then derives the applicable obligation set by *traversing* that graph from
the AI system node rather than by matching pre-coded rules. Novel
combinations resolve correctly because graph traversal generalizes; a
lookup table does not. See `PATENT.md` for the full node/edge schema, the
reference traversal algorithm, and the patent claim language.

Derivation runs on a **hybrid trigger**: an immediate re-derivation when a
watched `ai_system` property changes (event-triggered), plus an independent
safety-net poll every `SAFETY_NET_POLL_HOURS` (default 2) that re-derives
every system to catch any event that was missed or failed to process. This
is deliberately never described as "real-time" -- see `PATENT.md`'s change
log, which locks that language decision.

The satellite computes; it never decides. Every derivation it pushes to
core is independently re-validated and re-derived by core before core
writes anything to its own tables -- see "How it's wired to core" below and
`PATENT.md`'s "Satellites Compute, Core Decides" section.

## Repo layout

```
src/p2_satellite/       The satellite itself. Deploys independently as its
                         own service. This is the only directory whose code
                         actually runs as "the P2 satellite" in production.
core-side-patch/        A patch set to be reviewed and hand-copied into the
                         SEPARATE CompliVibe core backend repo
                         (app.complivibe.in). It is NOT part of this
                         satellite's runtime and does not deploy alongside
                         it -- it lives in this repo only so the two sides
                         could be designed, tested, and cross-validated
                         together. A human on the core team must merge it
                         into the real core repo (see MERGE_CHECKLIST.md).
tests/unit/              Unit tests for src/p2_satellite/ modules, plus the
                         import-boundary guard test (see below).
tests/integration/       A live-HTTP dry run: boots core-side-patch's real
                         FastAPI routers in-process and drives the
                         satellite's real graph_builder -> traversal ->
                         ingest_client call path against them over HTTP.
tests/benchmark/         The patent evidence suite (Workstream E): a
                         reproducible proof case showing graph traversal
                         succeeds where a naive static lookup table fails
                         or is incomplete, on the novel EU-India biometric
                         combination from PATENT.md.
tests/fixtures/          Shared fixture data/expected results used by both
                         the satellite's own tests and core-side-patch's
                         tests, so both sides can be checked against the
                         same graph.
tests/performance/       A small, CI-safe performance regression smoke test
                         (not the full 1,000/10,000-system benchmark -- see
                         scripts/benchmark_scale.py and PERFORMANCE.md).
scripts/                 Operational scripts: the import-boundary guard and
                         the manual 1,000/10,000-system performance
                         benchmark (see below / PERFORMANCE.md).
```

**core-side-patch/ does NOT run.** There is no process anywhere that starts
`core-side-patch/` code as part of "running the satellite." It exists to be
read, reviewed, and copied file-by-file into the real core backend repo by a
human, after resolving the open items listed in
`core-side-patch/ASSUMPTIONS.md` (e.g. the real `ai_system` field names, the
real `AuditService` signature, the real Alembic head revision). Its own test
suite (`core-side-patch/tests/`) runs against in-memory SQLite stand-ins so
that this repo's own reviewers can exercise the validation contract
end-to-end without a live core deployment; it is not a substitute for
testing against the real core codebase before merge.

## How the satellite deploys

The satellite is **one running process**: the `event_listener.py` FastAPI
app. Its `lifespan` context manager calls `scheduler.start_scheduler()` on
startup and `scheduler.stop_scheduler()` on shutdown (see
`src/p2_satellite/event_listener.py`'s `_lifespan()` and
`src/p2_satellite/scheduler.py`) -- so the APScheduler safety-net poll runs
as a background thread *inside* the same process as the webhook receiver,
not as a separate process or separate deployment. Nothing else needs to be
started standalone: run

```
uvicorn src.p2_satellite.event_listener:app --host $EVENT_LISTENER_HOST --port $EVENT_LISTENER_PORT
```

and both halves of the hybrid trigger (event-triggered webhook handling and
the periodic reconciliation poll) are live.

### Environment variables

All satellite configuration is loaded once in `src/p2_satellite/config.py`
via `load_settings()`. Every variable is documented in `.env.example` at the
repo root; briefly:

| Variable | Purpose |
|---|---|
| `CORE_BASE_URL` | Base URL of the core backend the satellite pulls from / pushes to. |
| `CORE_EXPORT_API_KEY` | Scoped key (permission `patent_export:p2:read`) sent as a bearer token on the three export GETs. |
| `CORE_INGEST_API_KEY` | Scoped key (permission `patent_ingest:p2:write`) sent as a bearer token on the ingest POST. |
| `SAFETY_NET_POLL_HOURS` | Reconciliation poll interval (default 2). Tunable config, not a patent claim element. |
| `EVENT_LISTENER_HOST` / `EVENT_LISTENER_PORT` | Bind address for the FastAPI webhook receiver. |
| `EVENT_LISTENER_SHARED_SECRET` | HMAC key used to verify inbound change-event webhooks (see below). |
| `MAX_TRAVERSAL_DEPTH` | Traversal safety bound (default 6), read from exactly one place in `traversal.py`. Configurable, not a claim element. |
| `EMBEDDING_MODEL` / `EMBEDDING_DIM` | sentence-transformers model + dimension for node-text embeddings (`embeddings.py`), matched to core's `pgvector` column width. |
| `METHODOLOGY_VERSION` | Written to every derivation and audit row so methodology changes are traceable. |
| `EVENT_WEBHOOK_MAX_CLOCK_SKEW_SECONDS` | Freshness/replay window (default 300s) for inbound change-event webhooks -- see "How it's wired to core" below. |
| `EVENT_LISTENER_IP_ALLOWLIST` | Optional comma-separated IP allowlist for the webhook endpoint (default empty = disabled; HMAC alone still gates access). |
| `INGEST_BATCH_CHUNK_SIZE` / `INGEST_BATCH_PACE_SECONDS` | How the safety-net poll chunks and paces its batched ingest pushes so a large sweep never bursts past core's ingest rate limit in one shot -- see `PERFORMANCE.md`. |

## How it's wired to core

The satellite is **agent-push / inbound-only**: it calls out to core on its
own schedule/trigger; core never calls into the satellite except to deliver
a change-event webhook notification (the satellite still does all the
pulling of actual data itself).

**Pulls** (`src/p2_satellite/graph_builder.py`), authenticated with
`Authorization: Bearer {CORE_EXPORT_API_KEY}` (permission
`patent_export:p2:read`):

- `GET {CORE_BASE_URL}/api/v1/patent-exports/p2/ai-systems`
- `GET {CORE_BASE_URL}/api/v1/patent-exports/p2/regulations-catalog`
- `GET {CORE_BASE_URL}/api/v1/patent-exports/p2/jurisdictions`

**Pushes** (`src/p2_satellite/ingest_client.py`), authenticated with
`Authorization: Bearer {CORE_INGEST_API_KEY}` (permission
`patent_ingest:p2:write`):

- `POST {CORE_BASE_URL}/api/v1/patent-ingest/p2/obligation-derivation` --
  one derivation per call (used by the event-triggered path).
- `POST {CORE_BASE_URL}/api/v1/patent-ingest/p2/obligation-derivations/batch`
  -- many derivations per call (used by the safety-net poll, which chunks a
  large sweep into groups of `INGEST_BATCH_CHUNK_SIZE`, see `PERFORMANCE.md`).
  Same "Satellites Compute, Core Decides" validation contract per item as
  the single-item route; one bad item in a batch doesn't fail the rest.

Every push (single or per-item within a batch) carries a `derivation_hash`
(sha256 of the canonical JSON of `ai_system_id`, `derived_obligations`,
`derived_controls`, and `methodology_version`) in the request body (and, for
the single-item route, also as the `Idempotency-Key` header), so re-pushing
an unchanged derivation (e.g. from the safety-net poll re-deriving a system
the event path already handled) never duplicates audit rows on the core
side.

**Receives** one HMAC-signed webhook (`src/p2_satellite/event_listener.py`):

- `POST /events/ai-system-changed` on the satellite's own
  `EVENT_LISTENER_HOST:EVENT_LISTENER_PORT`
- Signature header: **`X-P2-Signature`**, formatted as
  `t=<unix_epoch_seconds>,v1=sha256=<hex hmac>` -- the hex digest is
  `HMAC-SHA256(key=EVENT_LISTENER_SHARED_SECRET, message=f"{t}." + <raw
  request body bytes>)`. Verified with a constant-time comparison
  (`hmac.compare_digest`) in `event_listener.verify_signature()`. Rejected
  with `401` if: the header is malformed, the signature doesn't match, the
  timestamp is more than `EVENT_WEBHOOK_MAX_CLOCK_SKEW_SECONDS` away from
  now, or the exact `(t, signature)` pair has already been seen (replay
  protection -- a captured valid signed payload cannot be resent). If
  `EVENT_LISTENER_IP_ALLOWLIST` is set, requests from other source IPs are
  additionally rejected with `403` (opt-in defense-in-depth alongside HMAC,
  not a replacement for it).

Core then independently validates before writing anything (see
`core-side-patch/routers/patent_ingest_p2.py` and `PATENT.md`'s "Satellites
Compute, Core Decides"): it re-checks every obligation/control id against
its live catalog, independently re-derives a sample (or all) of the
affected systems using its own reference recursive CTE
(`core-side-patch/reference_traversal_cte.py`), flags mismatches for human
review instead of silently overwriting, and audit-logs the full event
including `trigger_reason` and `validation_status`. This full round trip
(satellite pulls -> derives -> pushes -> core validates -> core writes ->
core audit-logs) is exercised end to end, over real HTTP against core-side-
patch's actual FastAPI routers (not mocks), by
`tests/integration/test_end_to_end_dry_run.py`.

> **This satellite must never import `app.*` (the core backend package).**
> See the import guard in `scripts/check_no_core_imports.sh`, which enforces
> this in CI via `tests/unit/test_no_core_imports_guard.py` (part of the
> normal `pytest` run -- no separate CI step needed).

## Observability

Every stage that does real work (export pull, graph build, traversal,
ingest push, scheduled sweep) logs structured start/end/duration events via
`src/p2_satellite/observability.py`'s `log_event`/`timed_stage` helpers --
context (`ai_system_id`, `trigger_reason`, `duration_ms`, etc.) is always
carried as `extra=` fields, never string-interpolated into the message, so
it's filterable in any log aggregator that reads structured fields. Every
logger that could plausibly see a secret value (the HMAC shared secret, the
two scoped API keys) has `observability.install_secret_redaction()` attached,
which scrubs any exact occurrence of those values -- including from
structured extra fields, not just the rendered message -- from log output as
a defense-in-depth backstop.

**JSON output:** call `observability.configure_json_logging()` once, early,
in a real deployment's process entrypoint to render every log line as JSON
via `structlog` (Apache-2.0/MIT dual-licensed), layered on top of stdlib
`logging` rather than replacing it -- every existing `log_event`/`timed_stage`
call site, and every `caplog`-based test, is unaffected either way; this is
purely an output-format opt-in. Not called automatically at import time
(whether logs render as JSON vs. plain text for local dev is a deployment
choice). See `src/p2_satellite/observability.py`'s module docstring and
`tests/unit/test_observability_json_logging.py`.

**Metrics:** `GET /metrics` on the satellite's event-listener app exposes
Prometheus text-exposition format (`prometheus-client`, Apache-2.0) --
`src/p2_satellite/metrics.py` defines traversal count/duration (by
`trigger_reason`), ingest-push success/failure counters (by push kind),
the replay-cache size as a live gauge, and a `validation_status` counter
recorded from every ingest response -- the satellite-observable half of the
same "did core validate or flag this" signal core-side-patch's
`mismatch_metrics.py` already tracks independently. Single-process only
(prometheus-client's default in-process registry, same caveat as the
in-process counters/rate-limiters elsewhere in this codebase) -- see
`metrics.py`'s module docstring for what multi-worker deployment would need.

On the core side, `core-side-patch/routers/patent_ingest_p2.py` emits a
WARNING-level structured log line (`governance_graph.obligation_derivation_mismatch`)
whenever core's independent re-derivation disagrees with what the satellite
submitted, and records every ingest outcome (validated or flagged) in
`core-side-patch/mismatch_metrics.py`'s in-process `MismatchMetrics` counter
so the **validation mismatch rate** -- the single most important number for
trusting this system in production -- is queryable without reading raw
`governance_graph_traversal_results` rows. This in-process counter is a
stand-in for a real metrics backend (Prometheus + an alert rule, or whatever
core already uses); see `core-side-patch/ASSUMPTIONS.md` item 15 and
`RUNBOOK.md` for what to do when the rate spikes.

## Failure modes

What actually happens under each of the following conditions (each has an
explicit, passing test -- this is not an assumption):

| Condition | Behavior | Test |
|---|---|---|
| **Export pull fails mid-graph-build** (one of the three export GETs errors) | `fetch_and_build_graph()` raises a typed `GraphBuildIncompleteError` naming which of the three export steps failed -- it can never return a partial/incomplete graph, since `build_graph()` is only ever called after all three fetches succeed. The caller (event listener / scheduler) logs and does not push a derivation for that cycle. | `tests/unit/test_graph_builder_failure_modes.py` |
| **Ingest push fails after a derivation was already computed locally** | The push is retried (tenacity, transient errors / 5xx only) using the SAME `derivation_hash` on every attempt; if all retries are exhausted the failure is logged structurally (with `ai_system_id`/`trigger_reason`/`derivation_hash`) and propagates to the caller, which does not double-write anything. Re-pushing the identical derivation later (e.g. the next safety-net sweep) is always safe -- it hashes identically and core dedupes on that hash. | `tests/unit/test_ingest_client.py` (retry-then-succeed, hash stability, exhausted-retry propagation) |
| **Malformed or replayed webhook** | Rejected `401` if: the signature is missing/malformed/wrong, the timestamp is stale (`> EVENT_WEBHOOK_MAX_CLOCK_SKEW_SECONDS` old), or the exact `(timestamp, signature)` pair has already been processed once (replay of a captured, still-otherwise-valid payload). | `tests/unit/test_event_listener.py` |
| **Scheduler and event listener fire for the same `ai_system_id` at nearly the same time** | A per-`ai_system_id` non-blocking lock (`src/p2_satellite/concurrency.py`) ensures only one of the two paths actually processes that system at a time; the loser skips (logging `*.skipped_in_flight`) rather than racing. Chosen over a dedupe-time-window or last-write-wins because two concurrent fetch+derive+push cycles could see different graph states and produce genuinely different, unordered concurrent writes -- see `concurrency.py`'s docstring for the full reasoning. | `tests/unit/test_concurrency.py`, `tests/unit/test_scheduler.py` |
| **Core's independent re-derivation disagrees with the satellite** | `validation_status="flagged_mismatch"` is set, the submitted obligations/controls are NOT written to `ai_system_obligation_links`, AND a WARNING-level structured log fires plus a mismatch-rate counter increments -- see "Observability" above. This is not just a silent column nobody queries. | `core-side-patch/tests/test_core_patch_ingest_router.py` |
| **A regulation has no obligations yet (mid-onboarding a new framework)** | `derive_obligations()`'s result includes a non-empty `incomplete_coverage` list naming that regulation, distinct from a genuinely-empty `derived_obligations` (which means "nothing applies," not "data isn't seeded yet"). A WARNING log fires when this happens. | `tests/unit/test_traversal_coverage_gaps.py` |

See `RUNBOOK.md` for what an on-call engineer should actually do when one of
these conditions is observed in production.

## Dependencies

`requirements.txt` / `requirements-dev.txt` are compiled, hash-pinned
lockfiles generated by `pip-tools` (MIT) from `requirements.in` /
`requirements-dev.in` -- edit the `.in` files, never the compiled `.txt`
files by hand:

```
pip install pip-tools
pip-compile --generate-hashes --allow-unsafe --output-file=requirements.txt requirements.in
pip-compile --generate-hashes --allow-unsafe --output-file=requirements-dev.txt requirements-dev.in
```

`requirements.in` is the satellite's production runtime dependencies only.
`requirements-dev.in` additionally pulls in the test runner, code-quality
tooling (ruff/mypy/black/pre-commit), security scanners (pip-audit/bandit),
and `core-side-patch/`'s own demo/test-only dependencies (sqlalchemy,
pgvector, alembic, pyvis) -- needed to run this repo's full `pytest.ini`
suite (which colocates `core-side-patch/tests/` alongside the satellite's
own tests), NOT what should be merged into the real core repo (see
`core-side-patch/requirements-additions.txt` for that list instead).

## Running tests

```
pip install -r requirements-dev.txt
pytest                      # unit + integration + a small performance smoke test
pytest tests/benchmark/     # patent evidence suite (the required "static
                             # lookup fails / graph traversal succeeds" proof)
python3 scripts/benchmark_scale.py 1000 10000   # the real 1,000/10,000-system
                                                 # performance numbers -- see
                                                 # PERFORMANCE.md; not part of
                                                 # the default `pytest` run
                                                 # since its timing isn't a
                                                 # reliable CI pass/fail gate
```

No live core deployment or network access is required for any of the above
-- the integration dry run boots `core-side-patch`'s real routers in-process
against an in-memory SQLite database.

**Coverage:** `pytest --cov=src/p2_satellite --cov=core-side-patch --cov-report=term-missing`
(pytest-cov, MIT) -- currently 98% overall, no file below 70%. See
`COVERAGE.md` for the full breakdown and what was/wasn't chased down.

`tests/benchmark/eu_india_biometric_case.py` satisfies `PATENT.md`'s
"Required Evidence Before Filing": on the EU-India joint-deployment
biometric case, a naive static lookup table returns 1 of 11 owed
obligations (silently, not as an error), while graph traversal returns the
complete, correctly role-tagged set spanning GDPR + EU AI Act (high-risk)
+ DPDP. See `tests/benchmark/PATENT_TECHNICAL_EFFECT.md` for the full
inputs/outputs and `REPRODUCIBILITY.md` for the reproduction transcript.
That doc also discloses one known limitation: `build_graph()` does not yet
ingest ai-system `properties` into the graph itself, so the controller/
processor role split is demonstrated via obligation-level role tags rather
than node properties -- see `MERGE_CHECKLIST.md` for the full known-gaps
list before treating this repo as filing-ready.
