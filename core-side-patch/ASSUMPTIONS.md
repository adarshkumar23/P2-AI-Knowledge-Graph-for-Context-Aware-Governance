# ASSUMPTIONS -- core-side-patch/ (Patent P2, Workstream A)

This patch set was built with **zero access to the real CompliVibe core
backend repo** (`app.complivibe.in`, migration head 0175, 297 tables). Only
this satellite-only repo exists in this environment. Every place below where
this patch had to guess at a real core convention is listed here. **Nothing
in this list should be treated as verified.** A human on the core team must
check each item against the real codebase before merging any file under
`core-side-patch/`.

This file is the honesty mechanism the task explicitly asked for -- if
something isn't listed here but turns out to be wrong, that's a bug in this
document, not evidence the patch set is more verified than it is.

---

## 1. Primary keys / column types

- **Assumed `BigInteger` autoincrement primary keys** for all new tables
  (`governance_graph_nodes`, `governance_graph_edges`,
  `governance_graph_traversal_results`, `governance_graph_change_events`).
  Could not verify whether core's convention is integer/bigint or UUID PKs.
  If core uses UUIDs, every FK reference in this patch (edges ->nodes,
  ORM helpers in `models.py`) needs to change type accordingly.
- In `core-side-patch/models.py`, the ORM `id` columns use
  `sa.BigInteger().with_variant(sa.Integer(), "sqlite")` purely so this
  repo's own tests can run against SQLite (no live Postgres is available
  here -- SQLite only auto-increments a primary key whose type affinity is
  exactly `INTEGER`). The real migration (`migrations/0176_...py`) still
  declares plain `sa.BigInteger()` for Postgres. This dual-typing is a
  testing artifact, not a design recommendation -- flag it during review.
- **`ai_system_id` is modeled as a string everywhere** in this patch
  (`GovernanceGraphTraversalResult.ai_system_id`, the ingest payload's
  `ai_system_id` field, `AiSystemObligationLink.ai_system_id`), matching
  `tests/fixtures/sample_export.py`'s `"sys-alpha"` / `"sys-beta"` style
  keys. **Real core's `ai_system.id` is very likely an integer primary
  key.** If so: change the Pydantic field type, the ORM column types, and
  the `resolve_ai_system_node_id()` stringification (`node_key=str(ai_system_id)`)
  consistently, or this patch will silently produce string/int mismatches.

## 2. `ai_system` model field names (export endpoints)

- `routers/patent_exports_p2.py`'s `/ai-systems` endpoint is specified (by
  CLAUDE_CODE_GOAL_PROMPT.md / PATENT.md / `tests/fixtures/sample_export.py`)
  to return `id, name, geographic_scope, data_categories, risk_tier,
  deployment_status`. We could not check the real `ai_system` ORM model to
  confirm these are the actual column names (vs., e.g., `jurisdiction` singular,
  `data_category` singular, a `risk_level` enum with different values, etc).
  `data_providers.py`'s `_AssumedAiSystem` placeholder model uses exactly
  these names as a stand-in -- **it is not the real model and must not be
  merged as a new table.** Replace `SQLAlchemyExportDataSource` with queries
  against the real `AiSystem`/equivalent model before merging.
- CLAUDE_CODE_GOAL_PROMPT.md's Workstream A section separately names the
  watched fields as `deployment_jurisdiction`, `data_categories`, `risk_tier`
  (singular `deployment_jurisdiction` vs. the export field `geographic_scope`,
  which is plural/list-shaped in `sample_export.py`). We could not reconcile
  whether these are the same underlying column under two different names, or
  genuinely two different columns (e.g. one normalized "primary jurisdiction"
  used for the change-event trigger, one list-shaped export field for the
  graph). `change_event_outbox.py`'s `WATCHED_AI_SYSTEM_FIELDS` uses the
  CLAUDE_CODE_GOAL_PROMPT.md names (`deployment_jurisdiction`, `data_categories`,
  `risk_tier`) verbatim; this needs reconciling against the real model.

## 3. `regulation` / `jurisdiction` catalog data

- Assumed regulations/obligations/jurisdictions are close to **static,
  near-global reference data** (not per-org, rarely changing), based on
  nothing more than "this is what most GRC platforms do" -- not verified.
  `SQLAlchemyExportDataSource.list_regulations_catalog` / `list_jurisdictions`
  in `data_providers.py` don't apply `changed_since` filtering at all; if
  these ARE per-org and outbox-tracked in the real core, that filtering needs
  to be added.
- The exact nesting (`requires_obligations[].needs_controls[]`,
  `risk_tier_obligations` keyed by tier) mirrors `tests/fixtures/sample_export.py`
  precisely, on the assumption that the shared-fixture contract IS the real
  contract other workstreams (and the real core team) already agreed to. If
  the real regulation model doesn't shape data this way, `data_providers.py`'s
  interface (return already-shaped dicts) still holds -- only the concrete
  query implementation needs to change.

## 4. `AuditService`

- **We do not have the real `AuditService` class, its import path, or its
  `write_audit_log()` signature.** `core-side-patch/audit_service_stub.py` is
  a STUB with an in-memory sink (`AuditService._written`) so
  `routers/patent_ingest_p2.py` is independently testable here.
  CLAUDE_CODE_GOAL_PROMPT.md is explicit that the real method is named
  `write_audit_log` (not `.log()`) -- that's the only thing about the real
  signature we could anchor on. Our stub's kwargs (`session, org_id, actor_id,
  event_type, payload`) are a best guess; the real signature must be checked
  and the call site in `patent_ingest_p2.py` updated to match before merge.

## 5. Permission / scoped-API-key system

- We do not know whether core has a separate "service/integration scope"
  concept distinct from human RBAC permissions. `permissions.py`'s two new
  constants (`patent_export:p2:read`, `patent_ingest:p2:write`) are written
  as plain string constants; if core already has a typed/enum permission
  registry, these need to be registered there instead, in whatever pattern
  P6-P9's satellite integrations already established (which we also don't
  have access to -- CLAUDE_CODE_GOAL_PROMPT.md references "P6-P9 conventions"
  but the actual P6-P9 code isn't in this repo either).
- **We do not know how core stores/validates issued scoped API keys**
  (hashed in a dedicated table? a secrets manager? something else?).
  `dependencies.py`'s `validate_scoped_api_key` is a pluggable function
  defaulting to a trivial in-memory plaintext-comparison registry
  (`_DEV_SCOPED_KEYS`) for this repo's own dev/test use only. **This must
  never be used in production as-is** -- it does not hash keys, does not
  rate-limit, and does not support real rotation. A human must replace it
  with a real lookup against wherever core actually stores these.
- `get_current_organization()` / `get_current_active_user()` in
  `dependencies.py` are hardcoded stubs returning a fixed dev
  `Organization(id=1, org_id=1)` / `ActiveUser(id=1, org_id=1)`.
  CLAUDE_CODE_GOAL_PROMPT.md requires "dependency style returns objects, not
  dicts" and names both `.id` and `.org_id` -- we gave `Organization` both
  fields even though it's plausible the real `Organization` model only has
  `.id` and the `.org_id` requirement was really describing the *User*
  object's foreign key to its org. Verify which is which against the real
  models.

## 6. Outbox / change-event pattern

- **We could not check whether core already has a generic outbox pattern
  elsewhere in its 297 tables** (e.g. for webhooks, search indexing, or other
  integrations). `change_event_outbox.py` is written *as if no such pattern
  exists yet* and adds a new, narrowly-scoped table
  (`governance_graph_change_events`) instead. If core already has a generic
  outbox, this should be reconciled/merged into that instead of adding a
  second mechanism -- this is explicitly called out as a TODO in
  `change_event_outbox.py`'s docstring and in `MERGE_NOTES.md`.
- `emit_change_event()` is a standalone helper with a `TODO` comment; we have
  no access to the real code path where `ai_system.deployment_jurisdiction` /
  `data_categories` / `risk_tier` actually get updated, so the wiring
  (e.g. a SQLAlchemy `after_update` event listener scoped to exactly these
  three columns) is **not implemented**, only documented as a TODO.
- No migration is included for `governance_graph_change_events` itself (only
  the three PATENT.md-named tables have a migration in
  `migrations/0176_add_governance_graph_tables.py`). A human merging this
  needs to add a second migration for the outbox table (or fold it into the
  same migration) -- see `MERGE_NOTES.md`.

## 7. `ai_system_obligation_links`

- **This table is assumed to already exist in the real core** (PATENT.md's
  "Satellites Compute, Core Decides" section says core "writes only
  validated results to ai_system_obligation_links", implying it predates this
  patch). We do not have its real schema. `models.py`'s
  `AiSystemObligationLink` assumes columns `id, org_id, ai_system_id,
  obligation_id, control_type_id, created_at` and is **not created by the
  migration** (on the assumption it already exists) -- if it doesn't actually
  exist yet, a migration needs to be added for it too.
- The ingest payload carries two independent flat lists
  (`derived_obligations[]`, `derived_controls[]`), not paired
  (obligation, control) tuples. Since we don't know the real table's
  semantics, `upsert_ai_system_obligation_links()` writes one row per
  obligation (`control_type_id` left null) and one row per control
  (`obligation_id` left null) -- a judgment call, not a verified mapping.
  If the real table expects one row per (obligation, control) pair (e.g.
  derived from `graph_path`), this write logic needs to change completely.
- The "upsert" is a dedup-then-insert (query existing rows, skip duplicates),
  **not** a real `INSERT ... ON CONFLICT DO NOTHING/UPDATE`, because we don't
  know the real table's unique constraint (if any). Not safe under concurrent
  writes as-is.

## 8. `governance_graph_nodes` uniqueness

- Added a `UNIQUE(org_id, node_type, node_key)` constraint in the migration.
  This is **not** literally required by PATENT.md's column list -- it's a
  judgment call so re-ingesting the same export can't duplicate graph nodes.
  Flag for review; it may conflict with a real requirement to version/history
  node changes instead of upserting in place.

## 9. Alembic / migration framework details

- `down_revision = "0175"` assumes the real head revision's Alembic ID is
  literally the string `"0175"`. Real Alembic revision IDs are usually
  random 12-character hex strings (or similar); `"0175"` is almost certainly
  just this migration's *sequence number* as described in
  CLAUDE_CODE_GOAL_PROMPT.md ("migration head 0175"), not the actual
  revision ID string Alembic needs. **This migration will not apply until a
  human replaces `down_revision` with the real head revision ID.**
- We do not know core's exact Alembic conventions (single `migrations/`
  directory vs. per-app directories, naming convention beyond
  `<revision>_<slug>.py`, whether `alembic.ini` has multiple script
  locations, whether there's a script-generation template with additional
  boilerplate/header requirements). This migration is written in the most
  common/vanilla Alembic style.
- `CREATE EXTENSION IF NOT EXISTS vector` is executed inside `upgrade()`.
  This assumes the database role Alembic runs migrations as has
  `CREATE EXTENSION` privilege (often requires superuser on managed Postgres
  unless the extension was pre-provisioned by infra). If core's deployment
  process provisions `pgvector` separately (e.g. via Terraform/RDS parameter
  groups) this line may need to be removed and replaced with an
  infra-side prerequisite -- see `MERGE_NOTES.md`.

## 10. `pgvector-python` dependency

- `pgvector` (the Python package providing `pgvector.sqlalchemy.Vector`) is
  assumed **not** already a core dependency; added to
  `core-side-patch/requirements-additions.txt`. If core already uses
  pgvector elsewhere (plausible, given PATENT.md lists it as an "Open Source
  Tool" for this portfolio generally), this may be a no-op version bump
  rather than a net-new dependency -- verify against core's actual
  requirements file.
- Confirmed empirically in this environment that `pgvector.sqlalchemy.Vector`
  columns compile fine under SQLite too (used for `core-side-patch`'s own
  tests here) -- this is incidental, not something to rely on; the real
  column only works with the Postgres pgvector extension enabled.

## 11. `MAX_TRAVERSAL_DEPTH` on the core side

- The satellite has `src/p2_satellite/config.py`'s `settings.max_traversal_depth`
  (default 6, env `MAX_TRAVERSAL_DEPTH`). Core needs its own equivalent
  settings value for `reference_traversal_cte.derive_obligations_reference()`
  -- we have no access to core's settings/config system, so
  `routers/patent_ingest_p2.py`'s `_resolve_max_traversal_depth()` is a
  hardcoded stub returning `6`. Must be wired to core's real settings
  mechanism (env var / feature flag / whatever core uses) before merge, and
  per PATENT.md's CHANGE LOG, must remain "referenced from one place" once
  wired, never hardcoded elsewhere.

## 12. Postgres-only literal CTE not exercised against a real Postgres

- `reference_traversal_cte.py`'s `derive_obligations_reference()` executes
  `REFERENCE_CTE_SQL_POSTGRES` verbatim only when the SQLAlchemy session's
  dialect is `"postgresql"`. **No live Postgres instance exists in this
  environment**, so that code path is untested here -- only the SQLite/ORM
  pure-Python fallback path is exercised by
  `core-side-patch/tests/test_core_patch_reference_traversal_cte.py`. A human
  merging this patch MUST add an integration test against a real Postgres
  database (e.g. via `pytest-postgresql` or a CI Postgres service container)
  before trusting the literal-SQL path in production.

## 13. Test-only imports of `tests/fixtures/*` and `src/p2_satellite`

- Per the hard rule ("must NOT import anything from `src/p2_satellite/`"),
  none of the shipped patch files (`permissions.py`, `models.py`,
  `validation.py`, `reference_traversal_cte.py`, `change_event_outbox.py`,
  `dependencies.py`, `data_providers.py`, `audit_service_stub.py`,
  `routers/*.py`, `migrations/*.py`) import from `src/p2_satellite` or
  `tests/fixtures`. Only files under `core-side-patch/tests/` do, and only
  because this satellite-only repo happens to colocate the "core" and
  "satellite" sides for demonstration/cross-validation purposes (proving the
  reference CTE reimplementation agrees with the shared fixture contract).
  A real core repo's own test suite would have its own fixtures for this;
  the algorithmic point being tested is unaffected either way.

## 14. Package layout / hyphenated directory name

- `core-side-patch/` (as required by the task) contains a hyphen, which is
  not a valid Python dotted-import segment. Rather than nesting an
  underscore-named package one level deeper (which would misalign with the
  literal file paths named in the spec, e.g. `permissions.py` directly under
  `core-side-patch/`), every module under `core-side-patch/` uses **flat
  sibling imports** (e.g. `from permissions import ...`, not
  `from .permissions import ...` or `from core_side_patch.permissions import
  ...`). `core-side-patch/tests/conftest.py` puts `core-side-patch/` itself
  onto `sys.path` so this resolves in this repo's test run. When a human
  merges these files into the real core repo, each file's imports will need
  to be rewritten to match wherever core's actual package puts them (e.g.
  `from app.permissions import ...`) -- this patch set does not attempt to
  guess core's real package/module naming convention.

## 15. `mismatch_metrics.MismatchMetrics` -- in-process counter, not a real metrics pipeline

- Added in this hardening pass to make the validation-mismatch rate
  (PATENT.md's "Satellites Compute, Core Decides" contract, step 2)
  queryable/testable rather than only discoverable by hand-querying
  `governance_graph_traversal_results` rows for `validation_status =
  'flagged_mismatch'`.
- **This is an in-memory, single-process class-level list** (same pattern as
  `audit_service_stub.AuditService._written`). It:
  - resets on process restart (loses history across deploys/restarts),
  - does not aggregate across multiple core replicas/workers (each replica
    has its own independent counter, so the "rate" it reports is only that
    one process's local view, not the org-wide/fleet-wide rate),
  - is a simple count-based ratio, not a time-windowed rate against a real
    time series (its optional `window` parameter is a recency window over
    however many records happen to still be in memory, not a wall-clock
    window).
  A human merging this MUST replace/augment `MismatchMetrics.record()` with
  an emission to whichever real metrics backend core already runs (Prometheus
  counter + alert rule is the most likely fit, but we do not have access to
  core's actual observability stack from this satellite-only repo -- unknown
  the same way AuditService's real backend is unknown, see item 4) before
  this rate can be trusted as a production signal. See
  `core-side-patch/mismatch_metrics.py`'s module docstring for the same
  caveat in code.

## 16. `rate_limiter.FixedWindowRateLimiter` -- in-process, single-replica only

- Added in this hardening pass to bound how fast a single satellite instance
  (compromised, buggy, or stuck in a retry loop) can flood
  `POST /api/v1/patent-ingest/p2/obligation-derivation` with writes. Scoped
  per patent_ingest:p2:write scoped-key token (not per-IP -- PATENT.md's
  "Satellite Architecture" section establishes the satellite as the sole,
  agent-push caller of this endpoint, always presenting its dedicated scoped
  key, so per-IP limiting would not add meaningful protection here).
- Fixed-window counter (not token bucket) chosen for simplicity; documented
  in `core-side-patch/rate_limiter.py`'s module docstring alongside the
  tradeoff (a burst can straddle a window boundary -- accepted for a stopgap).
- Default: 100 requests / 60-second window per scoped key
  (`rate_limiter.DEFAULT_LIMIT` / `DEFAULT_WINDOW_SECONDS`) -- a rough
  stopgap, not a value tuned against real satellite traffic (unknown from
  this satellite-only repo; PATENT.md's hybrid trigger implies normal traffic
  should be far below this, but actual event-storm/backfill volume is
  unverified). Tune before relying on this bound in production.
- **This is in-process, single-replica state** (a module-level singleton,
  same category of limitation as item 15 above). It:
  - resets on process restart (briefly generous after a restart -- the safe
    failure direction for a rate limiter, never briefly stingy),
  - does NOT share state across multiple core replicas/workers -- if core
    runs N replicas behind a load balancer, the effective limit is
    approximately N times the configured per-process limit, since each
    replica's counter is independent and a given request could land on any
    replica.
  A human deploying this behind more than one core replica MUST replace this
  with a shared store (Redis `INCR`/`EXPIRE`, or whatever core's real
  rate-limiting infrastructure already is) before this bound is meaningful at
  scale.

## 17. `patent_ingest_p2.py`'s new WARNING-level mismatch log line

- Added `logger.warning("governance_graph.obligation_derivation_mismatch",
  extra={...})` using Python's stdlib `logging` directly (module-level
  `logging.getLogger("core_side_patch.patent_ingest_p2")`), specifically
  because this satellite-only repo's core-side-patch/ must not import
  `src/p2_satellite/observability.py` (satellite-only, off-limits per the
  hard cross-boundary rule) and we have no access to whatever structured-
  logging convention core's real codebase already uses (JSON formatter?
  correlation-id middleware? a wrapping helper around stdlib `logging`?
  unknown). A human merging this should replace the plain stdlib logger with
  core's real logging convention if one already exists, keeping the same
  event name and `extra` fields (`org_id`, `ai_system_id`,
  `methodology_version`, `trigger_reason`) so any existing log-based alerting
  patterns can match on them.

## 18. `POST /api/v1/patent-ingest/p2/obligation-derivations/batch` -- new route, added for scale

- Added in the performance-hardening pass so the satellite's safety-net poll
  (which can sweep thousands of `ai_system`s per run) sends one HTTP request
  instead of one per system. Shares the EXACT SAME per-item validation
  contract as the single-item route (`_process_one_derivation`, extracted as
  a shared helper both routes call) -- this is a transport-level batching
  optimization only, not a second implementation of "Satellites Compute,
  Core Decides".
- A bad item fails only that item (`{"ok": false, "error": {...}}` in its
  slot in the response); the batch does not roll back as a whole. Each
  item's DB write (`session.commit()` inside `_process_one_derivation`) is
  independent, so a later item's failure cannot undo an earlier item's
  successful commit within the same batch.
- Rate limiting (`rate_limiter.require_ingest_rate_limit_n`) charges the
  WHOLE batch size in one atomic check against the scoped key's budget --
  deliberately NOT the single-item route's flat-1-unit-per-HTTP-call
  dependency, which would let a batch of thousands bypass the limit almost
  for free.
- This is a NEW endpoint/permission surface on top of everything else in
  this patch set -- it needs the same permission (`patent_ingest:p2:write`,
  no new permission introduced) and the same migration prerequisites as the
  single-item route; no additional migration/permission work is needed
  beyond what's already listed above.

## 19. Six customer-facing knowledge-graph endpoints (this pass) -- overview

This pass added `graph_query.py` (shared traversal/query layer) and
`routers/patent_knowledge_graph_p2.py` (the six endpoints from PATENT.md's
"Features Enabled" section: on-demand derive, view graph, manual edge
addition, browse nodes, sync, coverage gaps). Unlike the two routers above
(satellite-only, scoped-API-key auth), these are reached by normal
CompliVibe users -- everything below is new, unverified surface on top of
items 1-18. See that router file's and `graph_query.py`'s docstrings for the
in-code version of each item below.

## 20. Route prefix diverges from the established `/api/v1/patent-*/p2` convention

- The task spec names these six routes literally under
  `/ai-governance/knowledge-graph/...`, not
  `/api/v1/patent-{exports,ingest}/p2/...` like the two existing routers in
  this patch set. We have no way to check whether core's real ~1,609-endpoint
  API surface actually uses one single prefix convention (plausible) or
  whether `/ai-governance/...` is itself an existing convention for a
  different feature family within core that these should join. Used the
  task's literal paths verbatim; flag for review before merge.

## 21. Human RBAC permission check is an unverified, always-allow stub

- Unlike `patent_export:p2:read` / `patent_ingest:p2:write` (satellite
  scoped-API-key permissions, item 5), the two new constants
  `permissions.GOVERNANCE_GRAPH_READ` / `GOVERNANCE_GRAPH_WRITE` are meant to
  be normal HUMAN role/permission strings -- a compliance officer, not the
  satellite, calls these six endpoints. We have no access to core's real
  RBAC permission-check dependency (how permissions attach to a user/role,
  what the check call site looks like). `dependencies.require_permission()`
  is a stub dependency factory that ALWAYS ALLOWS regardless of which
  permission string it's given -- it exists only to pin the call-site shape
  (`Depends(require_permission(GOVERNANCE_GRAPH_READ))`) in all six routes so
  a human wiring the real check later edits one function, not six routes.
  **This must be replaced with a real permission check before these
  endpoints are reachable by real users** -- as shipped, any authenticated
  user in the org can call all six, including the two write endpoints
  (manual edge addition, sync).

## 22. [RESOLVED, open-source-tooling pass] No existing mechanism in this repo populates `governance_graph_nodes`/`governance_graph_edges` at all

**Resolution applied:** option (b) below was chosen and implemented --
`POST /api/v1/patent-ingest/p2/graph-structure` (new route in
`routers/patent_ingest_p2.py`, backed by `models.upsert_graph_structure`) now
accepts the satellite's full node/edge set
(`src/p2_satellite/graph_builder.serialize_graph_structure()`, pushed by
`src/p2_satellite/ingest_client.push_graph_structure()`, wired into both
`event_listener.process_ai_system_changed` and
`scheduler._run_safety_net_poll` right after every `fetch_and_build_graph()`
call). The satellite is now the sole source of truth for graph structure in
this patch set -- this is a STANDALONE resolution (does not require core to
have any separate ETL process), chosen specifically so Features 1/2/3/4/6
have real data the moment the satellite runs once, per the polish-pass
prompt's explicit instruction.

This resolution is still new, satellite-authored surface, not verified
against real core conventions -- seven NEW sub-assumptions it introduces:

- **New route/permission surface**: `/graph-structure` reuses the exact same
  `patent_ingest:p2:write` scope and per-scoped-key rate limiter
  (`_rate_limited_ingest_scope`) as `/obligation-derivation` -- one push
  costs 1 rate-limit unit, same as one derivation push, even though a
  structure push typically carries a MUCH larger payload (the whole graph,
  not one ai_system's result). Unverified whether core's real rate-limiting
  convention would want a separate/larger budget for this route -- revisit
  once real satellite traffic/payload-size data exists.
- **Node upsert-by-natural-key is real** (reuses `governance_graph_nodes`'
  existing `UNIQUE(org_id, node_type, node_key)` DB constraint from
  migration 0176 -- see item 8), but **edge upsert-by-natural-key is
  application-level only** (`models.upsert_graph_structure` does
  query-then-decide on `(org_id, source_node_id, target_node_id, edge_type)`,
  the same non-atomic style already flagged for
  `upsert_ai_system_obligation_links` in item 7) -- there is no DB-level
  unique constraint on `governance_graph_edges` enforcing this. Not safe
  under concurrent structure pushes for the same org (e.g. an event-
  triggered push racing the safety-net poll's push) as shipped -- a human
  merging this should add a real unique constraint via a follow-up migration
  before trusting this under concurrent load.
- **Un-archiving on re-push**: if a node was previously archived
  (`archived=True`, e.g. by some future admin/cleanup action not in this
  patch set) and the satellite pushes it again, `upsert_graph_structure`
  silently un-archives it. This is a judgment call (the satellite pushing it
  again implies "still in the live export"), not a verified product
  decision -- flag for review if "archived" is meant to be a sticky/manual
  state that a structure push should never override.
- **Dangling edge references within one push are silently skipped**, not
  rejected -- if an edge's source/target `(node_type, node_key)` isn't found
  in that push's own `nodes` list or already persisted from a prior push,
  `upsert_graph_structure` drops that one edge and continues (no error
  surfaced to the satellite, no partial-failure signal in the response body
  beyond the edge simply not appearing in `edges_created`/`edges_updated`
  counts). Acceptable for now since the satellite always serializes a
  graph's own edges alongside its own nodes in one call (this path isn't
  expected to be exercised), but a human should decide whether a real
  partial-failure signal is needed before this is relied on with partial/
  incremental pushes.
- **Both push call sites (event-triggered AND safety-net) push the WHOLE
  graph unconditionally**, not just a per-ai_system subset -- a design
  choice (see `event_listener.py`/`scheduler.py` comments) trading extra
  HTTP payload size/frequency for "one code path, no separate incremental-
  push logic to get subtly wrong." A human should confirm this trade-off is
  acceptable at real fleet scale (a large graph pushed on every single
  watched-field event, not just periodically) -- revisit with a client-side
  last-pushed-hash cache (skip the HTTP call entirely when
  `compute_structure_hash()` matches the last successful push) if this
  proves too chatty in practice.
- **A structure-push failure never blocks that event's own derivation
  push** -- both call sites catch and log (`logger.exception(...)`) rather
  than propagate, on the reasoning that a stale graph-structure snapshot in
  core is a visibility/staleness problem (affecting Features 2/3/4/6's read
  freshness), not a correctness problem for the ai_system's own
  re-derivation (which core still cross-checks independently). Unverified
  against what a human on the core/satellite team would actually want here
  (e.g. alerting specifically on structure-push failures, distinct from
  derivation-push failures) -- no such distinct alerting exists yet.
- Resolution (a) (a core-native ETL from its own ai_system/regulation/
  jurisdiction tables into the graph tables) was NOT implemented -- if the
  real core already has such a process, this satellite-push resolution
  would need reconciling with it (likely: pick one source of truth, not
  both) rather than running both simultaneously.

Original (still-relevant) framing of the gap, kept for the audit trail:

- This is a bigger gap than it first appears, and all four of Features
  1/2/3/6 (everything except the two that only touch
  `governance_graph_traversal_results` or fire a change event) depend on it.
  Grepping this entire repo confirms: `routers/patent_ingest_p2.py` (the
  satellite's ONLY write path into core) writes ONLY to
  `governance_graph_traversal_results` and `ai_system_obligation_links` -- it
  never writes a single row to `governance_graph_nodes` or
  `governance_graph_edges`. `src/p2_satellite/graph_builder.py` builds the
  graph as an in-memory NetworkX object for the satellite's OWN traversal
  computation and never pushes it back to core at all (core never calls the
  satellite, and the satellite has no push-graph-structure endpoint to call
  even if it could). So: nothing in this repo, in either Workstream A or the
  satellite, actually populates the two tables these six endpoints read from.
  The task prompt's framing ("that data already lives in core, populated by
  the satellite's ingest pipeline") does not match what this repo's own
  ingest pipeline actually does. Two plausible real-world resolutions,
  neither of which we can verify from here:
    (a) core has its own ETL/sync job that mirrors its NATIVE ai_system /
        regulation / jurisdiction / data_category tables into the graph
        node/edge tables (consistent with "Core Decides" -- core, not the
        satellite, owns the graph's structural data; the satellite only
        reads it via the export endpoints and computes derivations from its
        own copy), or
    (b) a not-yet-built ingest endpoint accepts satellite-submitted
        node/edge structure directly (which would be a new agent-push
        surface, analogous to `/obligation-derivation` but for graph
        structure instead of derived results).
  This patch set (all of Workstream A plus this pass) is written under
  resolution (a) implicitly -- `reference_traversal_cte.py`,
  `models.py`'s `resolve_ai_system_node_id`, and every function in
  `graph_query.py` all assume the tables are simply "already there" and
  never write structural nodes/edges themselves (Feature 3's manual edge
  addition is the ONE exception, and it's explicitly human/one-edge-at-a-
  time, not a bulk sync). A human on the core team MUST resolve which of (a)
  or (b) (or something else) is real before any of these endpoints can
  return non-empty data in production. This repo's own tests build fixture
  graphs directly into the tables (see `tests/conftest.py`'s
  `seed_org_graph`), which is the same kind of test-only stand-in as the
  rest of this patch set's fixture-backed tests.

## 23. Pagination convention (Feature 4) is guessed

- `page` / `page_size` query params (default 50, max 200), a
  `{"items": [...], "meta": {"page", "page_size", "total", "count"}}`
  envelope. This repo has NO existing paginated endpoint to check a real
  convention against (the two export endpoints return everything in one
  response, unpaginated) -- limit/offset, cursor-based, or a different
  param-naming scheme are all equally plausible for core's real ~1,609
  endpoints. Verify and rename before merge if core's convention differs.

## 24. Response envelope for the six new endpoints reuses the exports' `items` + `meta` shape

- `graph_query.envelope()` mirrors `routers/patent_exports_p2.py`'s private
  `_envelope()` helper (the only real precedent in this repo) for Feature
  4's node listing and Feature 6's gap listing. Feature 2's graph-view
  response is `{"nodes": [...], "edges": [...]}` instead (a different shape,
  because the task spec pins this exact shape for graph-visualization
  consumption) -- these two envelope styles coexisting in one router is a
  deliberate reflection of "the shape is dictated by what each response
  actually is," not an inconsistency to resolve by forcing one envelope
  everywhere. Still, neither shape is verified against core's other ~1,609
  endpoints -- see item 23.

## 25. New `governance_graph_traversal_results.validation_status` value: `"self_derived"`

- Rows written by `graph_query.derive_and_persist_traversal()` (Feature 1's
  on-demand derive, and whatever eventually consumes a Feature-5/manual-edge
  change event) use `validation_status="self_derived"`, distinct from
  `routers/patent_ingest_p2.py`'s `"validated"` / `"flagged_mismatch"` (which
  describe the satellite-submission cross-check contract -- there is nothing
  submitted to cross-check here, core's own reference computation is
  authoritative by construction). If `validation_status` is backed by a real
  Postgres/DB enum type in core (not a free varchar, as this patch's models
  assume -- see item 1), this new value needs registering there too.

## 26. New `governance_graph_traversal_results.methodology_version` constant for core-native traversals

- `graph_query.CORE_REFERENCE_METHODOLOGY_VERSION = "core-reference-v1.0.0"`
  tags rows produced directly by core's own reference CTE (Feature 1 and
  whatever consumes Feature 5's change event), so they're never confused
  with whatever `methodology_version` string the satellite stamps on its own
  ingest payloads. Real core may already have its own versioning convention
  for this -- unverified.

## 27. DESIGN QUESTION (not just an assumption): should on-demand derivation immediately overwrite `ai_system_obligation_links`?

- `graph_query.derive_and_persist_traversal(..., persist_links=True)` (the
  default used by Feature 1) upserts into `ai_system_obligation_links`
  immediately, treating core's own on-demand computation as equally
  authoritative as a satellite-submitted-and-validated one. This is a
  genuine product decision, not an engineering guess: an alternative design
  would treat an on-demand/human-triggered result as a PREVIEW that doesn't
  become the org's system-of-record links table until corroborated by the
  satellite's own methodology (mirroring the "Satellites Compute, Core
  Decides" two-implementation-agreement spirit, even though there's nothing
  to agree with here since only core computed anything). `persist_links` is
  exposed as a parameter specifically so a human can flip this default
  without touching the traversal logic itself. **Needs a product decision,
  not a code review.**

## 28. Feature 3's "affected ai_systems" reverse-reachability walk is synchronous and unbounded by ai_system count

- `graph_query.find_upstream_ai_systems()` does a full reverse-BFS over the
  org's edge graph (bounded by `max_traversal_depth` hops, not by how many
  ai_systems exist) synchronously inline in the manual-edge-addition request
  path. Correct, but a scalability concern for an org with a very large
  graph/many ai_systems -- a production implementation might instead queue a
  background full-org rescan rather than compute this inline per edge
  addition. Flagged for review, not fixed here (this repo has no real-scale
  graph to benchmark against; see `tests/performance/` for the kind of scale
  test that would need to be re-run against this function specifically
  before trusting it inline at production graph sizes).

## 29. Feature 6 gap-detection duplicates the satellite's `obligation_needs` edge-type string as a literal

- `graph_query.OBLIGATION_NEEDS_CONTROL_EDGE_TYPE = "obligation_needs"`
  mirrors `src/p2_satellite/schema.py`'s `EDGE_OBLIGATION_NEEDS` constant
  VALUE, but is not imported from it (core-side-patch/ must not import
  `src/p2_satellite`, per the hard rule -- see item 13). If whatever
  mechanism actually populates `governance_graph_edges` in the real core
  (see item 22 -- this repo doesn't know what that mechanism is) uses a
  different edge_type string for "this obligation needs this control_type,"
  this constant must be updated to match, or Feature 6 will silently report
  zero gaps for every obligation (the "obligation has zero
  `obligation_needs` edges -> not reportable" branch in
  `find_coverage_gaps()`'s docstring would swallow the mismatch instead of
  erroring).

## 30. DESIGN QUESTION (not just an assumption): Feature 6's "covered" has no real implementation-status backing

- The task explicitly asked us to check for an existing
  controls-implementation-status concept elsewhere in the codebase before
  inventing one, given CompliVibe's four-pillar architecture likely already
  has this as a cross-cutting concern. We could not find one anywhere in
  this repo (`ai_system_obligation_links`'s assumed schema -- item 7 -- has
  no per-row status field at all, just bare obligation_id/control_type_id
  presence). `graph_query.find_coverage_gaps()` therefore defines "covered"
  as "at least one control_type the GRAPH says this obligation needs is ALSO
  present in this ai_system's linked control_type_ids" -- i.e.
  presence-of-a-structurally-linked-control, NOT
  implemented-vs-not-implemented status. **This is the single highest-risk
  design gap in this pass**: if a real controls-implementation-status field
  exists elsewhere in core (a `control_implementations` table, an
  `implementation_status` enum column, etc.), Feature 6 as shipped here will
  produce a plausible-looking but semantically wrong "gap" list (it reports
  structural absence, not un-implemented-ness) and should be rewritten
  against the real concept, not merged as-is. Needs a human decision from
  someone who knows CompliVibe's actual controls-tracking pillar.

## 31. DESIGN QUESTION (not just an assumption): Feature 4's global-vs-org-scoped node types

- `governance_graph_nodes.org_id` is `NOT NULL` (migration 0176), so every
  node row -- including `regulation` / `jurisdiction` rows that are
  conceptually GLOBAL reference data (a regulation's text doesn't differ per
  customer) -- is modeled as belonging to exactly one org. `graph_query.list_nodes()`
  filters strictly by `org_id` for every node_type, including
  regulation/jurisdiction, because that is what the current schema supports.
  This almost certainly means the (unverified, see item 22) real
  graph-population mechanism duplicates identical regulation/jurisdiction
  nodes once per org rather than storing them once globally. A human needs
  to decide: is that duplication intentional/acceptable (e.g. for per-tenant
  archival independence, letting one org's regulation catalog snapshot drift
  from another's over time), or should reference-data node types be
  de-duplicated behind a global sentinel org_id (or a nullable org_id
  meaning "global"), which would change this function's query for those
  node types specifically? **Needs a product/data-model decision, not a
  code fix** -- flagged exactly as the task anticipated.

## 32. 404 vs. 422 for "unknown ai_system" on the six new routes

- `routers/patent_ingest_p2.py` returns 422 for an unknown `ai_system_id`
  (rejecting a satellite-SUBMITTED payload referencing an unknown system).
  The six new customer-facing routes return 404 instead, for the same
  underlying condition (`graph_query.UnknownAiSystemError` /
  `resolve_ai_system_node_id() is None`), reasoning that these resolve a URL
  PATH parameter (`.../systems/{ai_system_id}/...`), where 404 is the more
  conventional REST status for "this resource doesn't exist" than 422
  ("this request body is semantically invalid"). Unverified against core's
  actual convention for path-parameter resource resolution across its other
  ~1,609 endpoints -- flag for review.

## 33. Graph node `label` (Feature 2 / Feature 4 response shape) falls back to `node_key`

- `governance_graph_nodes` has no dedicated human-readable `label` column
  (see `models.py`) -- `graph_query._node_to_dict()` uses
  `properties.get("label", node.node_key)`. If core's real graph-
  visualization frontend needs a distinct label convention (e.g. a
  regulation's full display name rather than its short catalog key), this
  needs revisiting once the real node-population mechanism (item 22) is
  known, since that's what would actually populate a `properties.label`
  value in production.

## 34. [open-source-tooling pass] Feature 2's `?format=html` pyvis view -- new optional dependency, no change to the JSON contract

- `GET .../systems/{id}/graph?format=html` (new query param, default
  `json`, unchanged behavior) renders the exact same subgraph via `pyvis`
  (BSD-3) as a self-contained interactive HTML page instead of the JSON
  body -- purely additive, for debugging/demo use before a real
  graph-visualization frontend consumes the JSON contract directly. See
  `requirements-additions.txt` for the new dependency and
  `graph_query.render_subgraph_html`'s docstring for why
  `cdn_resources="in_line"` was chosen specifically (fully self-contained
  string output, zero filesystem writes from inside an HTTP request
  handler).
- Required `response_model=None` on this route (FastAPI can't build a
  Pydantic response model from a `dict | HTMLResponse` return-type union)
  -- unverified whether core's real routing convention has an established
  pattern for dual-format endpoints like this one; flag for review if core
  already has one.
- Node colors in the rendered view (`graph_query._NODE_TYPE_COLORS`) are a
  small, purely cosmetic fixed palette invented for this patch -- not a
  claim about any real design system core may already have for graph/
  network visualizations elsewhere in the product.
