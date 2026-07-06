# MERGE_NOTES -- core-side-patch/ (Patent P2, Workstream A)

Read `core-side-patch/ASSUMPTIONS.md` first -- it lists everything in this
patch set that is unverified because the real core repo isn't accessible from
where this was built. This file is the practical "how to actually merge this"
checklist.

## 1. Migration order relative to head 0175

- `migrations/0176_add_governance_graph_tables.py` declares
  `down_revision = "0175"`. **This is almost certainly a placeholder** -- see
  ASSUMPTIONS.md item 9. Before this migration can run:
  1. Find the real Alembic revision ID string of core's actual current head
     (`alembic heads` in the real repo).
  2. Replace `down_revision = "0175"` with that real ID.
  3. Rename the file / pick a fresh unique `revision` value if `"0176_governance_graph"`
     collides with anything (unlikely, but check `alembic history`).
- This migration must land **before** any code path that queries
  `governance_graph_nodes` / `governance_graph_edges` /
  `governance_graph_traversal_results` (i.e. before deploying the two new
  routers) and **before** the satellite is pointed at a live export endpoint.
- A **second migration** is needed for `governance_graph_change_events`
  (defined in `change_event_outbox.py` but not included in
  `migrations/0176_add_governance_graph_tables.py`) -- either fold it into
  the same migration or add `0177_add_governance_graph_change_events.py` on
  top of it. Not included as a separate file here to keep the PATENT.md
  "Core Database Tables" migration focused on exactly the three tables it
  names; this is a judgment call, revisit if the core team prefers one
  migration for the whole feature.
- `ai_system_obligation_links` is assumed to already exist (see
  ASSUMPTIONS.md item 7) and is deliberately **not** created by any migration
  here. If it turns out not to exist yet, a migration for it needs to be
  written by whoever owns that table's real design.
- Requires the Postgres `pgvector` extension to be installable
  (`CREATE EXTENSION IF NOT EXISTS vector`, run inside `upgrade()`). If core's
  database role running migrations lacks `CREATE EXTENSION` privilege (common
  on managed Postgres), this needs to be pre-provisioned by infra instead and
  the `op.execute(...)` line removed.

## 2. Permissions to seed

- Two new scoped-key permission strings (`permissions.py`):
  - `patent_export:p2:read`
  - `patent_ingest:p2:write`
- These are **not** human user/role permissions -- do not add them to any
  role-assignment UI or default role bundle. They should be registered
  wherever core keeps its scoped/service-key permission catalog (if a
  separate registry from human RBAC exists -- see ASSUMPTIONS.md item 5).
- Two scoped API keys need to be **generated and issued once** to the P2
  satellite:
  - one carrying `patent_export:p2:read`
  - one carrying `patent_ingest:p2:write`
  Their raw values go into the satellite's env config
  (`CORE_EXPORT_API_KEY`, `CORE_INGEST_API_KEY` in
  `src/p2_satellite/config.py` / `.env.example`) and **only there** -- core
  should store a hash, never the raw key, once the real
  `validate_scoped_api_key` lookup replaces the dev stub in
  `dependencies.py`.
- Rotation process (core side): generate a new key with the same permission,
  update the satellite's env config out-of-band, then revoke the old key's
  hash from core's store. Because both the export and ingest paths are
  read-only-to-core / satellite-initiated respectively, rotation does not
  require any core-side downtime -- only a window during which the satellite
  must have the new key before the old one is revoked.

## 3. Env vars to add to core's deployment config

- None of this patch's *core-side* code reads new env vars directly (the
  `MAX_TRAVERSAL_DEPTH` default is currently hardcoded in
  `routers/patent_ingest_p2.py`'s `_resolve_max_traversal_depth()` -- see
  ASSUMPTIONS.md item 11). Before merge, decide how core's own
  settings/config system should expose:
  - `P2_MAX_TRAVERSAL_DEPTH` (or whatever naming convention core uses) --
    mirrors the satellite's `MAX_TRAVERSAL_DEPTH` (default 6). Must be a
    single source of truth, never a literal inline in the query/loop, per
    PATENT.md's CHANGE LOG.
  - Whatever secret-storage mechanism holds the two scoped API keys' hashes
    (see section 2).
- The satellite-side env vars (`CORE_BASE_URL`, `CORE_EXPORT_API_KEY`,
  `CORE_INGEST_API_KEY`, etc., already documented in this repo's
  `.env.example`) are NOT core-side config and don't need to be added to
  core's deployment -- they're how the satellite finds and authenticates to
  core, not the reverse (core never calls the satellite).

## 4. Wiring change_event_outbox.py into the real ai_system update path

`change_event_outbox.py`'s `emit_change_event()` is a standalone helper, not
yet wired to anything. To complete the hybrid trigger's event-triggered path
(PATENT.md "HYBRID TRIGGER" (a)):

1. Locate wherever `ai_system.deployment_jurisdiction` (or whatever the real
   column is named -- see ASSUMPTIONS.md item 2), `data_categories`, and
   `risk_tier` get updated in the real core codebase.
2. Add a SQLAlchemy `after_update` event listener (or equivalent hook if core
   uses a different ORM/update pattern) scoped to the `AiSystem` model that:
   - inspects `sqlalchemy.inspect(instance).attrs.<field>.history` for
     **exactly** `deployment_jurisdiction`, `data_categories`, `risk_tier`
     (`change_event_outbox.WATCHED_AI_SYSTEM_FIELDS`)
   - calls `emit_change_event(session, ai_system_id, changed_field, org_id)`
     once per changed watched field (not once per update, and never for any
     other column)
3. Do **not** fire this on any other column change -- this is a narrow,
   intentional trigger surface, not a general audit log.
4. Confirm (per ASSUMPTIONS.md item 6) whether core already has a generic
   outbox/event-log pattern elsewhere; if so, prefer wiring into that instead
   of `governance_graph_change_events`, and delete this file's table.

## 5. Rollback plan if the validation-mismatch rate is too high post-launch

PATENT.md's validation contract means a high `flagged_mismatch` rate is
*expected instrumentation*, not automatically a bug -- but if it's high
enough to indicate the satellite's traversal and core's reference
implementation have drifted (rather than genuinely disagreeing on edge
cases), the safe rollback sequence is:

1. **Do not disable the validation step.** The whole point of "Satellites
   Compute, Core Decides" is that core never writes satellite output
   unchecked -- turning off re-validation to "unblock" ingestion would
   reintroduce exactly the boundary violation PATENT.md calls out (the "P4
   satellite rebuild" class of bug it explicitly warns against).
2. Instead, **pause the satellite's ingest push** (satellite-side config /
   feature flag -- the satellite is agent-push, so this is a satellite-side
   change, not a core-side one) while `governance_graph_traversal_results`
   rows accumulate with `validation_status="flagged_mismatch"` for human
   review.
3. Compare a sample of flagged mismatches: if core's reference
   re-derivation and the satellite's NetworkX traversal disagree
   systematically (not just on this patch's untested Postgres-vs-SQLite
   path -- see ASSUMPTIONS.md item 12), that's most likely a `MAX_TRAVERSAL_DEPTH`
   mismatch between the two sides' config, a stale/out-of-sync graph on one
   side (export/ingest lag), or a genuine algorithm bug in one
   implementation -- diagnose before re-enabling.
4. If the new tables/endpoints need to be fully rolled back: revert the
   migration (`downgrade()` drops the three tables in dependency order, but
   deliberately does **not** drop the `vector` Postgres extension -- see
   ASSUMPTIONS.md item 9), remove the two routers from the app, and revoke
   both scoped API keys.
5. `ai_system_obligation_links` rows written while `validation_status="validated"`
   are NOT automatically rolled back by the migration downgrade (that table
   is assumed pre-existing, not created by this patch) -- a rollback that
   needs to undo already-validated writes requires a separate data-cleanup
   step scoped by `methodology_version` (every write carries this, per the
   audit contract), not a schema migration.

## 6. What's NOT done / needs a human before this is production-ready

See `ASSUMPTIONS.md` for the full list; the highest-risk items to resolve
first:
- Real `AuditService` import + signature (item 4)
- Real scoped-API-key storage/validation (item 5)
- Real `ai_system` / `ai_system_obligation_links` field names (items 2, 7)
- Real Alembic `down_revision` (item 9)
- A Postgres integration test for the literal reference CTE (item 12) --
  this repo could only test the SQLite/ORM fallback path.
- Wire `mismatch_metrics.MismatchMetrics` into a real metrics backend and
  alert rule, and `rate_limiter.FixedWindowRateLimiter` into a shared
  (Redis-backed or equivalent) store if core runs more than one replica --
  both are in-process stopgaps by design; see ASSUMPTIONS.md items 15/16.
- Confirm pgvector >= 0.5.0 is available wherever this migration actually
  runs, since `migrations/0176_add_governance_graph_tables.py` now creates an
  HNSW index (added in pgvector 0.5.0) on `governance_graph_nodes.embedding`.

## 7. Hardening pass additions (this pass -- production-hardening of the patch set)

On top of the original Workstream A patch, this pass added, all within
`core-side-patch/` and covered by `core-side-patch/tests/`:

- `mismatch_metrics.py` (new) -- in-process `MismatchMetrics` counter,
  recorded on every ingest (validated or flagged) by
  `routers/patent_ingest_p2.py`. See ASSUMPTIONS.md item 15.
- A `logger.warning("governance_graph.obligation_derivation_mismatch", ...)`
  line in `routers/patent_ingest_p2.py`, fired only when a mismatch is
  flagged, using stdlib `logging` directly (no dependency on
  `src/p2_satellite/observability.py`, which remains off-limits). See
  ASSUMPTIONS.md item 17.
- `rate_limiter.py` (new) -- a per-scoped-key `FixedWindowRateLimiter`
  (default: 100 req/60s) wired in as a FastAPI dependency
  (`_rate_limited_ingest_scope`) ahead of the ingest route handler, raising
  HTTP 429 once exceeded. See ASSUMPTIONS.md item 16.
- `migrations/0176_add_governance_graph_tables.py` now creates (and, in
  `downgrade()`, drops) an HNSW index
  (`ix_governance_graph_nodes_embedding_hnsw`, `vector_cosine_ops`) on
  `governance_graph_nodes.embedding` -- previously this column had no index
  at all, meaning every similarity search was a full table scan.
- Confirmed (did not need to add, already present in both directions):
  `patent_export:p2:read` is rejected by the ingest endpoint
  (`test_ingest_rejects_wrong_scope`) AND `patent_ingest:p2:write` is
  rejected by the export endpoint (`test_ai_systems_rejects_wrong_scope`,
  pre-existing in `test_core_patch_exports_router.py`) -- scope isolation was
  already fully correct, not a gap.
- Grepped `dependencies.py` (and the rest of `core-side-patch/`) for raw
  API-key/token logging -- found none; `_extract_bearer_token` and
  `validate_scoped_api_key` never log the token value, and HTTPException
  detail messages never echo it back either. No fix was needed here.

## 8. Six customer-facing knowledge-graph endpoints (this pass)

Adds `graph_query.py` (new, shared traversal/query layer) and
`routers/patent_knowledge_graph_p2.py` (new, the six endpoints from
PATENT.md's "Features Enabled" section), plus small additions to
`change_event_outbox.py` (`emit_manual_change_event`), `rate_limiter.py`
(per-org on-demand-derive limiter), `permissions.py` /`dependencies.py`
(human-RBAC permission stub), and `models.py` (`get_node`,
`create_manual_edge`). `routers/patent_ingest_p2.py`'s private
`_resolve_max_traversal_depth()` was removed and replaced by
`graph_query.resolve_max_traversal_depth()`, now the single place this
default lives on the core side (imported by both routers).

**Read `ASSUMPTIONS.md` items 19-33 before merging any of this** -- three
are flagged as things needing a human PRODUCT decision, not just an
engineering verification pass:
  - item 27: should on-demand derivation immediately overwrite
    `ai_system_obligation_links`, or is that a preview-only result?
  - item 30 (highest risk): Feature 6's "coverage gap" is defined against
    structural graph links, NOT a real controls-implementation-status
    concept -- none could be found in this repo. If one exists elsewhere in
    core's four-pillar architecture, Feature 6 needs rewriting against it,
    not merging as shipped.
  - item 31: are `regulation`/`jurisdiction` nodes meant to be duplicated
    per org (current schema/implementation) or de-duplicated globally?

**Item 22 is the load-bearing blocker for all of Features 1/2/3/6**: this
repo could not find ANY code path, in either Workstream A's ingest router or
the satellite, that actually writes to `governance_graph_nodes` /
`governance_graph_edges`. These four endpoints will return empty/404 for
everything in production until a human confirms how those two tables
actually get populated (core-native ETL from its own ai_system/regulation/
jurisdiction tables? A not-yet-built satellite push endpoint?) and wires
that up -- this pass only confirms the six endpoints correctly READ/WRITE
whatever is already in those tables, it does not populate them.

## 9. Item 22 closed: satellite pushes graph structure (open-source-tooling pass)

New `POST /api/v1/patent-ingest/p2/graph-structure` route (in
`routers/patent_ingest_p2.py`) + `models.upsert_graph_structure` close the
biggest gap from section 8 above: the satellite now pushes its whole built
graph (`src/p2_satellite/graph_builder.serialize_graph_structure`) after
every fetch, upserted by natural key, idempotent by construction (repeat
pushes of an unchanged graph create/update nothing). **Read ASSUMPTIONS.md
item 22's "Resolution applied" block before merging** -- seven new
sub-assumptions, most notably: edge upsert has no DB-level unique
constraint yet (application-level dedup only, same non-atomic caveat as
item 7), and both push call sites push the WHOLE graph on every single
event (not just a per-system subset), which may need a client-side
last-pushed-hash cache if real fleet traffic makes this too chatty.

Also still open, not new to this pass:
- `dependencies.require_permission()` is an always-allow stub (item 21) --
  as shipped, ANY authenticated user in an org can call all six endpoints,
  including the two writes (manual edge, sync). Must be replaced with a
  real permission check before these are user-reachable.
- The route prefix (`/ai-governance/knowledge-graph/...`) does not match the
  `/api/v1/patent-*/p2` convention the rest of this patch set uses (item
  20) -- confirm against core's real routing convention.
- No migration changes were needed this pass (all three tables these
  endpoints touch already exist per migration 0176); `governance_graph_change_events`
  writes from `emit_manual_change_event` land in the same not-yet-migrated
  table flagged in section 1 above.
