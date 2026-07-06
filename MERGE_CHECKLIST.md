# MERGE_CHECKLIST -- Patent P2 (AI Knowledge Graph for Context-Aware Governance)

Actionable checklist for whoever merges `core-side-patch/` into the real
CompliVibe core backend (`app.complivibe.in`) and stands up the
`src/p2_satellite/` satellite in production. This document synthesizes and
cross-references `core-side-patch/ASSUMPTIONS.md` (everything that's
unverified because this patch set was built without access to the real core
repo) and `core-side-patch/MERGE_NOTES.md` (the detailed how-to-merge notes)
-- read both in full before merging; do not rely on this checklist alone.

## 1. Migration order relative to head 0175

- [ ] `core-side-patch/migrations/0176_add_governance_graph_tables.py`
      declares `revision = "0176_governance_graph"`,
      `down_revision = "0175"`. **The `"0175"` value is a placeholder** --
      it stands for "migration head 0175" as named in
      `CLAUDE_CODE_GOAL_PROMPT.md`, not a real Alembic revision id (real ids
      are typically random hex strings). Run `alembic heads` in the real
      core repo, and replace `down_revision` with the actual current head's
      id before this migration will apply.
- [ ] Confirm `revision = "0176_governance_graph"` doesn't collide with
      anything in `alembic history`; rename if needed.
- [ ] This migration adds `governance_graph_nodes`, `governance_graph_edges`,
      `governance_graph_traversal_results` (the `embedding Vector(384)`
      column requires `CREATE EXTENSION IF NOT EXISTS vector`, which the
      migration runs inside `upgrade()` -- confirm the Alembic-running DB
      role has `CREATE EXTENSION` privilege, or move that step to an
      infra-side prerequisite instead).
- [ ] A **second migration** is still needed for
      `governance_graph_change_events` (the outbox table defined in
      `core-side-patch/change_event_outbox.py`) -- it is deliberately not
      included in `0176_...py`. Add it as its own revision on top of 0176,
      or fold it in, per team preference (see `MERGE_NOTES.md` §1).
- [ ] `ai_system_obligation_links` is assumed to already exist and is **not**
      created by this migration (see `ASSUMPTIONS.md` item 7). Confirm it's
      real; if it doesn't exist yet, someone owns writing that migration
      separately.
- [ ] This migration, and both new routers, must land **before** the
      satellite is pointed at a live core export/ingest endpoint.

Full detail: `core-side-patch/MERGE_NOTES.md` §1, `core-side-patch/ASSUMPTIONS.md` items 1, 8, 9, 12.

## 2. Permission seeding

- [ ] Register the two new scoped-key permission strings from
      `core-side-patch/permissions.py`:
      - `patent_export:p2:read`
      - `patent_ingest:p2:write`
- [ ] **These are service/integration-scope permissions, not human RBAC
      permissions.** Do **not** add either to any role-assignment UI, default
      role bundle, or any human user's permission set. They must live in
      whatever scoped-API-key registry core uses for service integrations
      (see `ASSUMPTIONS.md` item 5 -- it's unverified whether core has a
      registry distinct from human RBAC; if not, one may need to be
      introduced rather than overloading the human permission table).
- [ ] Generate and issue two scoped API keys -- one per permission above --
      to the satellite. Their raw values go **only** into the satellite's
      env config (`CORE_EXPORT_API_KEY`, `CORE_INGEST_API_KEY`, see
      `.env.example`); core should store a hash, never the raw key, once the
      real `validate_scoped_api_key` lookup replaces the dev stub in
      `core-side-patch/dependencies.py`.
- [ ] Confirm the rotation process is understood before go-live (generate
      new key -> update satellite env out-of-band -> revoke old key's hash;
      no core-side downtime required, per `MERGE_NOTES.md` §2).

Full detail: `core-side-patch/MERGE_NOTES.md` §2, `core-side-patch/ASSUMPTIONS.md` item 5.

## 3. Env vars

**Core-side** (add to core's deployment config -- see
`core-side-patch/MERGE_NOTES.md` §3 for the full reasoning):
- [ ] A core-side equivalent of `MAX_TRAVERSAL_DEPTH` (default 6) for
      `core-side-patch/reference_traversal_cte.py`'s independent
      re-derivation step. Currently hardcoded to `6` in
      `routers/patent_ingest_p2.py`'s `_resolve_max_traversal_depth()` --
      must be wired to core's real settings/config mechanism and referenced
      from exactly one place, never a literal in the query/loop (per
      `PATENT.md`'s change log).
- [ ] Wherever core stores the two scoped API keys' hashes (secrets
      manager / dedicated table / etc. -- see permission seeding above).
- [ ] **`core-side-patch/rate_limiter.py`'s per-scoped-key ingest rate limit
      (stopgap default: 100 derivations/60s) MUST be coordinated with the
      satellite's `INGEST_BATCH_CHUNK_SIZE`/`INGEST_BATCH_PACE_SECONDS`
      (see below) before go-live at large fleet sizes** -- the satellite
      chunks/paces its safety-net batch pushes assuming roughly this limit;
      if core's real limit ends up different (or if core runs multiple
      replicas, see `ASSUMPTIONS.md` item 16 on this limiter being
      single-process only), the satellite's defaults need retuning to match,
      or a sweep will either take far longer than necessary (limit raised,
      satellite still paces conservatively) or start 429ing (satellite's
      chunk size exceeds core's actual limit). See `PERFORMANCE.md`.

**Satellite-side** (already fully documented in `.env.example` at the repo
root -- these do **not** get added to core's deployment, they configure the
satellite process only):
- [ ] `CORE_BASE_URL`, `CORE_EXPORT_API_KEY`, `CORE_INGEST_API_KEY` -- how
      the satellite finds and authenticates to core.
- [ ] `SAFETY_NET_POLL_HOURS` (default 2), `MAX_TRAVERSAL_DEPTH` (default
      6) -- tunable config values, not patent claim elements.
- [ ] `EVENT_LISTENER_HOST`, `EVENT_LISTENER_PORT`,
      `EVENT_LISTENER_SHARED_SECRET` -- the webhook receiver's bind address
      and HMAC key. `EVENT_LISTENER_SHARED_SECRET` must match whatever core
      uses to sign the `X-P2-Signature` header on outbound change-event
      webhooks (see README.md "How it's wired to core") -- this is a shared
      secret both sides must agree on out-of-band; rotate it as a
      coordinated two-sided change, not independently.
  - [ ] Core needs to actually wire `change_event_outbox.emit_change_event()`
        into the real `ai_system` update path (it's currently a standalone,
        unwired helper -- see `MERGE_NOTES.md` §4) and send the signed
        webhook to the satellite's `EVENT_LISTENER_HOST:EVENT_LISTENER_PORT`.
- [ ] `EMBEDDING_MODEL` / `EMBEDDING_DIM` -- must match the dimension of the
      `pgvector` column core creates in the migration (`Vector(384)`).
- [ ] `METHODOLOGY_VERSION` -- bump on both sides in lockstep whenever the
      derivation algorithm changes; it's written to every audit row.
- [ ] `EVENT_WEBHOOK_MAX_CLOCK_SKEW_SECONDS` (default 300s) -- the webhook's
      replay/freshness window; requires reasonably synced clocks between
      core and the satellite host (NTP).
- [ ] `EVENT_LISTENER_IP_ALLOWLIST` (default empty/disabled) -- optional;
      only set this if core's real egress uses stable, known IPs.
- [ ] `INGEST_BATCH_CHUNK_SIZE` / `INGEST_BATCH_PACE_SECONDS` (defaults 50 /
      30s) -- see the rate-limiter coordination note above; do not tune
      independently of core's real ingest rate limit.

## 4. Rollback plan if the validation-mismatch rate is too high post-launch

`core-side-patch/MERGE_NOTES.md` §5 already specifies this; summarized here:

1. **Do not disable core's re-validation step.** Turning it off to "unblock"
   ingestion would reintroduce the exact boundary violation PATENT.md warns
   against (the "P4 satellite rebuild" class of bug) -- "satellites
   compute, core decides" is not negotiable.
2. Instead, pause the **satellite's** ingest push (a satellite-side
   feature flag/config change -- the satellite is agent-push, so this is
   satellite-side, not core-side) while flagged-mismatch rows accumulate in
   `governance_graph_traversal_results` for human review.
3. Diagnose flagged mismatches before re-enabling: most likely causes are a
   `MAX_TRAVERSAL_DEPTH` mismatch between the satellite's and core's config,
   a stale/out-of-sync graph on one side (export/ingest lag), or the
   untested Postgres-literal-CTE path disagreeing with the satellite's
   NetworkX traversal in a way the SQLite fallback path didn't surface (see
   `ASSUMPTIONS.md` item 12) -- not necessarily an algorithm bug.
4. Full rollback, if needed: run the migration's `downgrade()` (drops the
   three new tables in dependency order; deliberately does **not** drop the
   `vector` Postgres extension), remove the two new routers from the app,
   and revoke both scoped API keys.
5. `ai_system_obligation_links` rows already written with
   `validation_status="validated"` are **not** automatically undone by the
   migration downgrade (that table predates this patch and isn't created by
   it) -- undoing already-validated writes needs a separate data-cleanup
   step scoped by `methodology_version`, not a schema migration.

Full detail: `core-side-patch/MERGE_NOTES.md` §5.

## 5. Known gaps before this is truly production-ready

Pulled from `core-side-patch/ASSUMPTIONS.md` -- read that file in full for
the complete list (18 items after the production-hardening pass);
highest-severity first:

- [ ] **Real `AuditService` import path + `write_audit_log()` signature is
      unverified.** `core-side-patch/audit_service_stub.py` is an in-memory
      stub; `routers/patent_ingest_p2.py`'s call site must be updated to
      match the real signature before merge. (ASSUMPTIONS.md item 4)
- [ ] **Real scoped-API-key storage/validation is unverified.**
      `dependencies.py`'s `validate_scoped_api_key` defaults to a trivial
      in-memory plaintext registry -- fine for this repo's own tests, **must
      never run in production as-is** (no hashing, no rotation support). A
      per-key rate limit now exists (`rate_limiter.py`, see below) but it is
      a single-process in-memory stopgap, not a substitute for real key
      storage. (ASSUMPTIONS.md item 5)
- [ ] **Real `ai_system` field names are unverified.** The export endpoints
      assume `id, name, geographic_scope, data_categories, risk_tier,
      deployment_status`; the watched-field list for the change-event
      trigger assumes `deployment_jurisdiction, data_categories, risk_tier`.
      Whether `geographic_scope` (plural/list, export-side) and
      `deployment_jurisdiction` (singular, trigger-side) are the same
      underlying column or two different ones is unresolved. Also unverified:
      whether `ai_system.id` is a string or integer PK (this patch assumes
      string throughout). (ASSUMPTIONS.md items 1, 2)
- [ ] **No Postgres integration test exists yet.** The literal
      `REFERENCE_CTE_SQL_POSTGRES` path in
      `core-side-patch/reference_traversal_cte.py` has only been exercised
      against the SQLite/ORM fallback path in this environment (no live
      Postgres was available). A human must add a real Postgres integration
      test (e.g. `pytest-postgresql` or a CI Postgres service container)
      before trusting that code path in production. (ASSUMPTIONS.md item 12)
- [ ] **`ai_system_obligation_links`'s real schema is unverified** -- assumed
      to already exist with columns `id, org_id, ai_system_id,
      obligation_id, control_type_id, created_at`, and the write logic
      (one row per obligation, one row per control, both with the other FK
      null) is a judgment call pending confirmation of the real table's
      semantics. The current "upsert" is dedup-then-insert, not a real
      `INSERT ... ON CONFLICT`, and is not safe under concurrent writes as
      written. (ASSUMPTIONS.md item 7)
- [ ] **Outbox pattern may be duplicative.** `change_event_outbox.py` adds a
      new `governance_graph_change_events` table on the assumption core has
      no existing generic outbox; if one exists, reconcile into it instead
      of shipping a second mechanism. `emit_change_event()` is also not yet
      wired to the real `ai_system` update path -- that wiring is a TODO,
      not implemented. (ASSUMPTIONS.md item 6, MERGE_NOTES.md §4)
- [ ] **Permission-registry integration is unverified** -- the two scoped
      permissions are plain string constants; if core has a typed/enum
      permission registry or an established P6-P9 satellite-integration
      pattern, register them there instead (this patch set had no access to
      that code either). (ASSUMPTIONS.md item 5)
- [ ] **`core-side-patch/` module layout does not match core's real package
      structure.** Every file uses flat sibling imports (e.g. `from
      permissions import ...`) because `core-side-patch/` itself is a
      hyphenated, non-importable directory name; imports must be rewritten
      to match wherever these files actually land in the real core package
      (e.g. `from app.permissions import ...`) during merge.
      (ASSUMPTIONS.md item 14)
- [ ] **`mismatch_metrics.MismatchMetrics` and `rate_limiter.FixedWindowRateLimiter`
      are both in-process, single-replica stopgaps** (added in the
      production-hardening pass) -- they reset on restart and don't share
      state across multiple core replicas. Before production go-live with
      more than one core replica, both need a shared backing store (a real
      metrics backend + alert rule for mismatch rate; Redis or equivalent
      for the rate limiter). (ASSUMPTIONS.md items 15, 16)
- [ ] **The satellite's `INGEST_BATCH_CHUNK_SIZE`/`INGEST_BATCH_PACE_SECONDS`
      defaults (50 / 30s) are not verified against a real, tuned core rate
      limit** -- they're sized to comfortably fit under `rate_limiter.py`'s
      own stopgap default (100/60s), not a production-tuned value. See §3
      above and `PERFORMANCE.md`.
- [x] **The Workstream E benchmark suite has landed.**
      `tests/benchmark/eu_india_biometric_case.py` (10 passing tests),
      `PATENT_TECHNICAL_EFFECT.md`, and `REPRODUCIBILITY.md` now satisfy
      `PATENT.md`'s "Required Evidence Before Filing": naive static lookup
      returns 1 of 11 owed obligations (91% miss rate, silent — not an
      empty/error result) on the EU-India joint-deployment biometric case,
      while graph traversal returns the complete, correctly role-tagged set
      spanning GDPR + EU AI Act high-risk + DPDP. See that doc's disclosed
      "Known limitation" section: `build_graph()` does not yet ingest
      ai-system `properties` into the graph itself (the role split is
      demonstrated via obligation-level `applies_to_role` tagging instead,
      which is fully graph-driven) — a real, flagged gap, not a blocker to
      the benchmark's validity.

None of the above are TODOs to skip -- `core-side-patch/ASSUMPTIONS.md`
frames them correctly: anything not listed there that later turns out to be
wrong is a bug in that document, not evidence the patch set was more
verified than it says.
