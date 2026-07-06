# /goal — Build Patent P2: AI Knowledge Graph for Context-Aware Governance

Paste this whole prompt into Claude Code (Sonnet 5) at the root of the
`complivibe-patent-p2-knowledge-graph` repo (this scaffold: PATENT.md,
config.py, requirements.txt, .env.example already present — build on them,
don't restart).

---

## Goal

Build the P2 satellite end-to-end AND the minimal core-side surface it needs
to integrate — as two cleanly separated workstreams that merge into the main
CompliVibe backend (`app.complivibe.in`, migration head 0175, 297 tables)
without touching unrelated code. Ship something that is correct, tested,
and provably better than a static lookup table — not something that merely
claims to be.

Use your Task tool to run the workstreams below **in parallel as
sub-agents**, since they have clean, pre-defined interface contracts (given
in this doc) and don't need to wait on each other except at the two
integration checkpoints marked below. Each sub-agent should work in its own
directory scope and report back a diff/PR-ready branch.

## Non-negotiable architectural constraints (read PATENT.md first — it has the full spec and the rationale)

1. **Agent-push / inbound-only.** The satellite NEVER imports core code and
   NEVER writes to core's database directly. Core NEVER calls the satellite.
   Satellite pulls from core's read-only export endpoints, and pushes
   results to core's ingest endpoint. If any generated code imports
   `app.*` from inside the satellite repo, that's a bug — stop and fix it.
2. **Satellites compute, core decides.** Core must independently re-validate
   satellite output before persisting it (see PATENT.md § "Satellites
   Compute, Core Decides"). This is not optional and not a TODO — implement
   it in the first pass.
3. **Hybrid trigger, not real-time.** Event-triggered traversal on
   `ai_system` property change (jurisdiction, data_categories, risk_tier) +
   a `SAFETY_NET_POLL_HOURS` (default 2) reconciliation poll. Never describe
   or log this as "real-time."
4. **Traversal depth is config, not a magic number.** `MAX_TRAVERSAL_DEPTH`
   (default 6) must be a settings value read at runtime, referenced from one
   place, never hardcoded in a query or loop.
5. **Everything derived is auditable.** Every ingest write must produce an
   audit log row with `methodology_version`, `trigger_reason`
   (`event`/`scheduled`), and `validation_status`.
6. **Naming and patterns match the existing satellite repos** (P6–P9
   conventions already established) — same auth header pattern, same
   response envelope shape, same audit service call
   (`AuditService.write_audit_log()`, not `.log()`), same dependency style
   (`get_current_organization` / `get_current_active_user` return objects,
   not dicts).

---

## Workstream A — Core-side surface (own PR, targets main backend repo)

- Alembic migration (new revision, head 0175 → next) adding:
  `governance_graph_nodes`, `governance_graph_edges`,
  `governance_graph_traversal_results` exactly per PATENT.md schema,
  including `embedding Vector(384)` via pgvector.
- Three read-only export endpoints under `/api/v1/patent-exports/p2/`:
  `ai-systems`, `regulations-catalog`, `jurisdictions`. New scoped
  permission `patent_export:p2:read` — not a normal user permission. Return
  only the fields the graph needs (id, name, geographic_scope,
  data_categories, deployment_status — check existing `ai_system` model
  for exact field names, don't invent new ones).
- One ingest endpoint: `POST /api/v1/patent-ingest/p2/obligation-derivation`.
  New permission `patent_ingest:p2:write`. On receipt: re-validate every
  obligation/control ID against the live catalog, independently re-derive
  a sample using the reference recursive CTE (PATENT.md has it verbatim),
  flag mismatches instead of silently overwriting, write to
  `ai_system_obligation_links`, audit log.
- An internal change-event mechanism: when `ai_system.deployment_jurisdiction`,
  `data_categories`, or `risk_tier` changes, write a row to an outbox-style
  table the satellite's export endpoints can filter on (`changed_since`
  query param) — check whether an outbox pattern already exists elsewhere
  in the codebase before inventing a new one; reuse it if so.
- Tests: migration up/down, endpoint auth scoping, ingest validation
  rejecting bad obligation IDs, mismatch-flagging path.

## Workstream B — Satellite: graph construction + embeddings

- `graph_builder.py`: pulls from the three export endpoints (httpx client,
  retries via tenacity), builds a `networkx.DiGraph` per PATENT.md node/edge
  types.
- `embeddings.py`: wraps `sentence-transformers` (`all-MiniLM-L6-v2`,
  384-dim) to embed node text (regulation names/descriptions, data category
  labels) for semantic node matching via pgvector on the core side.
- Unit tests with a fixture dataset (small synthetic set of AI systems,
  regulations, jurisdictions) — no live core dependency required for tests.

## Workstream C — Satellite: traversal engine

- `traversal.py`: Python/NetworkX equivalent of the reference recursive
  CTE — BFS/DFS from an `ai_system` node, respecting `MAX_TRAVERSAL_DEPTH`,
  cycle-safe (track visited path, matching the CTE's `NOT (target = ANY(path))`
  guard), returns the same shape as the SQL version
  (`derived_obligations`, `derived_controls`, `graph_path`).
- Must produce byte-for-byte-comparable results to the core reference
  implementation on the same fixture graph — this is what makes the
  cross-validation in Workstream A actually meaningful. Write a shared
  test fixture both sides can run against.

## Workstream D — Satellite: hybrid trigger system

- `event_listener.py`: small FastAPI app receiving change-event
  notifications (HMAC-signed with `EVENT_LISTENER_SHARED_SECRET`), enqueues
  an immediate traversal for the affected `ai_system_id`.
- `scheduler.py`: APScheduler job running every `SAFETY_NET_POLL_HOURS`,
  re-traversing all systems, tagging `trigger_reason=scheduled`.
- `ingest_client.py`: pushes results to core's ingest endpoint with retries
  and idempotency (safe to re-push the same derivation without duplicating
  audit rows — use a derivation content hash).

## Workstream E — Benchmark & patent evidence (do this one carefully — it's the proof, not a formality)

- `tests/benchmark/eu_india_biometric_case.py`: construct the exact novel
  combination from PATENT.md — biometric AI system, joint EU-India
  deployment, dual purpose (employment screening + healthcare), split
  controller/processor role. Implement a naive static lookup table
  covering only anticipated single-jurisdiction/single-purpose combos, run
  it against this case, show it returns nothing or an incomplete/wrong
  set. Run the graph traversal against the same case, show it returns the
  correct complete obligation set (GDPR + EU AI Act high-risk obligations
  + DPDP, both controller and processor duties).
- `tests/benchmark/PATENT_TECHNICAL_EFFECT.md`: document the setup, exact
  inputs, exact outputs of both methods, and why the difference matters —
  mirror the format used in the P8/A3.6 benchmark docs
  (`PATENT_TECHNICAL_EFFECT.md` / `REPRODUCIBILITY.md` pattern already
  established for this portfolio).
- Everything here must be re-runnable by someone else with no manual setup
  beyond `pip install -r requirements.txt && pytest tests/benchmark/`.

## Workstream F — Integration & merge readiness

- README.md: how this satellite deploys, its env vars, how it's wired to
  core, explicit note that it must never import `app.*`.
- A lint/test guard (simple grep-based test is fine) that fails CI if any
  file in `src/p2_satellite/` imports from the core backend package.
- A merge checklist doc: migration order relative to head 0175, permission
  seeding steps, env vars to add to core's deployment config, rollback plan
  if validation-mismatch rate is too high post-launch.

---

## Integration checkpoints (sync points between sub-agents)

1. **After B + C**: confirm the fixture graph and expected traversal output
   are agreed and identical on both the core reference (Workstream A) and
   satellite (Workstream C) sides before either is considered done.
2. **After A + D**: run an end-to-end dry run — satellite pulls from a
   local/mock core export, derives, pushes to a local/mock core ingest,
   confirm validation and audit logging fire correctly.

## Definition of done

- `pytest` passes across all workstreams, including the benchmark.
- No satellite file imports core code (Workstream F guard passes).
- PATENT.md's "Required Evidence Before Filing" is satisfied by
  Workstream E's output.
- A short PR description per workstream, ready for Ares/Saransh review,
  noting exactly which core migration number this lands on top of.

Start by reading PATENT.md and this file in full, then spin up the
sub-agents for A–F. Report back a summary of what each produced before
attempting the merge integration checkpoints.
