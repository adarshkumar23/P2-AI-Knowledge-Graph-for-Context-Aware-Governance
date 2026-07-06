"""
Patch for CompliVibe core (app.complivibe.in), lands on top of migration head 0175.
Field names / existing-pattern assumptions below are best-effort from PATENT.md and
must be verified against the real ai_system model / AuditService / permission system
before merging -- see core-side-patch/ASSUMPTIONS.md.

New permission constants for the P2 knowledge-graph satellite integration.

IMPORTANT: these two keys are SCOPED API-KEY PERMISSIONS, not normal user/role
permissions. They must never appear in a human user's role/permission set and
must never be assignable through the normal admin "assign role to user" UI.
Per PATENT.md's "Satellite Architecture" section:
  - each is issued ONCE to the P2 satellite (not per-user)
  - each is rotatable via core's admin/secret-rotation tooling
  - the corresponding raw key value lives only in the satellite's env config
    (CORE_EXPORT_API_KEY / CORE_INGEST_API_KEY in src/p2_satellite/config.py)
  - core stores/validates a hash of the key, not the key itself (see
    dependencies.py's require_patent_export_scope / require_patent_ingest_scope
    stubs and ASSUMPTIONS.md for what is unverified about that lookup)

If core already has a distinct concept of "service/integration scope" separate
from human RBAC permissions (most mature multi-tenant apps do), these two
constants should be registered in *that* system, not the human permission
table. We could not verify which system exists in the real core -- see
ASSUMPTIONS.md.
"""

from __future__ import annotations

PATENT_EXPORT_P2_READ = "patent_export:p2:read"
PATENT_INGEST_P2_WRITE = "patent_ingest:p2:write"

# Convenience set for wiring into whatever scoped-key registry core actually uses.
SCOPED_API_KEY_PERMISSIONS = frozenset({PATENT_EXPORT_P2_READ, PATENT_INGEST_P2_WRITE})


# ---------------------------------------------------------------------------
# Customer-facing knowledge-graph endpoints (routers/patent_knowledge_graph_p2.py)
# ---------------------------------------------------------------------------
# UNLIKE the two scoped-API-key constants above, these ARE meant to be normal
# human RBAC permissions -- a compliance officer using the CompliVibe UI/API
# needs these, not the satellite (the satellite never calls these six
# endpoints; they are core-native, human- or human-triggered-automation-facing
# surface per PATENT.md's "Features Enabled" section). We do not have access
# to core's real permission-registry/role system to confirm the naming
# convention (colon-namespaced strings, like the scoped-key ones above, vs.
# some other RBAC permission-string format core already uses elsewhere) --
# see ASSUMPTIONS.md.
GOVERNANCE_GRAPH_READ = "governance_graph:read"
GOVERNANCE_GRAPH_WRITE = "governance_graph:write"

GOVERNANCE_GRAPH_PERMISSIONS = frozenset({GOVERNANCE_GRAPH_READ, GOVERNANCE_GRAPH_WRITE})
