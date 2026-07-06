PATENT P2
AI Knowledge Graph for Context-Aware Governance
Status: Active development | Satellite Repo: complivibe-patent-p2-knowledge-graph

================================================================================
CHANGE LOG (decisions locked 2026-07-06)
================================================================================
- Trigger model changed from pure 6-hour polling to HYBRID:
    (a) event-triggered traversal when a watched ai_system property changes
    (b) 2-hour safety-net poll to catch any missed/failed events
  Patent language MUST describe this as "event-triggered derivation with
  periodic reconciliation" — never "real-time." Reconciliation poll interval
  is a tunable config value (SAFETY_NET_POLL_HOURS), NOT a claim element.
- MAX_TRAVERSAL_DEPTH (default 6) is a CONFIGURABLE SAFETY BOUND, not a claim
  element. The patent claim rests on the traversal method, not on depth=6.
- Core validation obligation (see "Satellites Compute, Core Decides" below)
  is now an explicit, testable requirement — not an assumption.
- Benchmark requirement added: reproducible proof case (static lookup vs.
  graph traversal) is a required deliverable before this doc is treated as
  filing-ready. See tests/benchmark/.

================================================================================
The Problem It Solves
================================================================================
Every GRC platform that handles AI governance uses a static lookup table: if
the AI system is deployed in the EU and processes health data, apply GDPR +
EU AI Act. This works for combinations the platform developers anticipated.
It fails for novel combinations — a biometric AI system deployed in a joint
EU-India data center processing data for both jurisdictions, used for both
employment screening and healthcare, by a company that is a PII controller
in one country and a processor in another.

No platform can derive the correct obligation set for a genuinely novel
combination through hardcoded if/else logic. They either give you nothing or
give you everything.

================================================================================
What It Does — Plain English
================================================================================
Instead of a lookup table, P2 builds a property graph where every relevant
entity — AI systems, regulations, jurisdictions, data categories, control
types, obligations — is a node, and every typed relationship between them is
an edge. When you ask "what compliance obligations apply to this specific AI
system?" the answer is computed by traversing the graph from the AI system
node through all relevant edges to reach the complete obligation set. Novel
combinations resolve correctly because the graph handles them through
traversal, not through pre-coded if-else logic.

================================================================================
Graph Structure
================================================================================
Node Types:
  ai_system            each AI system in the inventory
  regulation           each regulation (GDPR, EU AI Act, DPDP, etc.)
  jurisdiction         geographic/legal jurisdiction
  data_category        personal/health/financial/biometric/etc.
  control_type         encryption/access_control/audit_logging/etc.
  obligation           specific compliance requirement
  risk_tier            prohibited/high/limited/minimal

Edge Types:
  system_uses            ai_system -> data_category
  system_deploys_in      ai_system -> jurisdiction
  data_triggers          data_category -> regulation
  jurisdiction_has       jurisdiction -> regulation
  regulation_requires    regulation -> obligation
  obligation_needs       obligation -> control_type
  system_classified_as   ai_system -> risk_tier
  risk_tier_adds         risk_tier -> additional obligations

================================================================================
Traversal Algorithm (core-side recursive CTE reference implementation)
================================================================================
WITH RECURSIVE obligation_graph AS (
    SELECT target_node_id, node_type, node_key,
           ARRAY[source_node_id] as path, 1 as depth
    FROM governance_graph_edges e
    JOIN governance_graph_nodes n ON n.id = e.target_node_id
    WHERE e.source_node_id = :ai_system_node_id
    AND e.is_active = true

    UNION ALL

    SELECT e.target_node_id, n.node_type, n.node_key,
           og.path || e.source_node_id, og.depth + 1
    FROM governance_graph_edges e
    JOIN governance_graph_nodes n ON n.id = e.target_node_id
    JOIN obligation_graph og ON og.target_node_id = e.source_node_id
    WHERE og.depth < :max_traversal_depth   -- config, default 6
    AND NOT (e.target_node_id = ANY(og.path))
    AND e.is_active = true
)
SELECT * FROM obligation_graph
WHERE node_type IN ('obligation', 'control_type')

Satellite mirrors this traversal locally over NetworkX for the same result,
so satellite output can be independently cross-checked by core.

================================================================================
Satellite Architecture (agent-push / inbound-only — matches platform-wide rule)
================================================================================
Core exposes READ-ONLY export endpoints. Satellite calls these on its own
schedule/trigger. Core never calls out to the satellite.

  GET /api/v1/patent-exports/p2/ai-systems
  GET /api/v1/patent-exports/p2/regulations-catalog
  GET /api/v1/patent-exports/p2/jurisdictions
  Auth: dedicated scoped key — permission patent_export:p2:read
        (not a normal user permission; issued once, rotatable,
         lives only in satellite's env config)

Satellite builds a local graph using NetworkX (BSD license):
  import networkx as nx
  G = nx.DiGraph()
  G.add_nodes_from(ai_systems + regulations + jurisdictions)
  G.add_edges_from(typed_relationships)

HYBRID TRIGGER:
  (a) Core emits a change event (internal event/outbox row) when an
      ai_system's deployment_jurisdiction, data_categories, or risk_tier
      changes. Satellite's event listener consumes this — satellite still
      pulls, core does not push directly (see integration note below).
  (b) Independent of events, satellite re-runs traversal for all systems
      every SAFETY_NET_POLL_HOURS (default 2) to catch missed/failed events.

Satellite pushes derived obligations to core (satellite-initiated, inbound
to core):
  POST /api/v1/patent-ingest/p2/obligation-derivation
  {
    ai_system_id, derived_obligations[], derived_controls[],
    graph_path, methodology_version, trigger_reason (event|scheduled)
  }
  Auth: permission patent_ingest:p2:write

================================================================================
Satellites Compute, Core Decides (mandatory validation contract)
================================================================================
Core MUST NOT write satellite output through unchecked. On ingest, core:
  1. Re-validates every obligation_id and control_type_id in the payload
     against its own active regulations/controls catalog — reject unknown
     or inactive references.
  2. Re-derives the obligation set for a random sample (or all, if load
     permits) of ai_system_ids independently using its own reference
     traversal (the recursive CTE above) and flags mismatches for review
     rather than silently overwriting.
  3. Writes only validated results to ai_system_obligation_links.
  4. Audit-logs the full derivation event, including whether it passed
     independent re-validation or was flagged.
This is the boundary violation class previously found in the P4 satellite
rebuild — P2 must not repeat it.

================================================================================
Core Database Tables
================================================================================
governance_graph_nodes
  id, org_id, node_type, node_key, properties JSONB,
  embedding Vector(384), created_at, archived

governance_graph_edges
  id, org_id, source_node_id, target_node_id,
  edge_type, weight, properties JSONB,
  created_at, is_active

governance_graph_traversal_results
  id, org_id, ai_system_id, traversal_at,
  input_context JSONB, derived_obligations JSONB,
  derived_controls JSONB, graph_path JSONB,
  methodology_version, trigger_reason, validation_status

================================================================================
Features Enabled
================================================================================
1. Derive Obligations for AI System
   POST /ai-governance/knowledge-graph/systems/{id}/derive-obligations
2. View AI System Graph
   GET /ai-governance/knowledge-graph/systems/{id}/graph
3. Manual Edge Addition
   POST /ai-governance/knowledge-graph/edges
4. Browse Nodes
   GET /ai-governance/knowledge-graph/nodes?type=regulation
5. Sync on System Change (event-triggered path)
   POST /ai-governance/knowledge-graph/systems/{id}/sync
6. Coverage Gap Detection
   GET /ai-governance/knowledge-graph/gaps

================================================================================
The Novel Patent Claim
================================================================================
Dynamic graph traversal over a property graph to derive jurisdiction-specific
compliance obligations for novel AI system configurations not pre-coded as
lookup rules, triggered by an event-detection mechanism with periodic
reconciliation, and cross-validated by an independent core-side re-derivation
step before being persisted. The inventive step is the traversal method
combined with the validation contract — same inputs through a static lookup
table produce wrong or incomplete results for novel combinations; through
graph traversal, cross-checked by core, they produce correct, auditable
results even for combinations the platform developers never anticipated.

================================================================================
Required Evidence Before Filing
================================================================================
A reproducible benchmark demonstrating: static lookup table fails (returns
nothing or wrong obligations) vs. graph traversal succeeds, on a documented
novel-combination test case (EU-India joint-deployment biometric system,
dual jurisdiction, dual purpose, split controller/processor role). See
tests/benchmark/eu_india_biometric_case.py and
tests/benchmark/PATENT_TECHNICAL_EFFECT.md.

================================================================================
Open Source Tools
================================================================================
Tool                 Purpose                          License
NetworkX             Graph construction/traversal      BSD 3
pgvector             Semantic node matching            PostgreSQL License
sentence-transformers Node embedding generation        Apache 2.0
APScheduler          Safety-net poll scheduling        MIT
FastAPI              Event listener (webhook receiver) MIT
