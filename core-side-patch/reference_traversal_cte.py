"""
Patch for CompliVibe core (app.complivibe.in), lands on top of migration head 0175.
Field names / existing-pattern assumptions below are best-effort from PATENT.md and
must be verified against the real ai_system model / AuditService / permission system
before merging -- see core-side-patch/ASSUMPTIONS.md.

Core's independent re-derivation implementation, required by PATENT.md's
"Satellites Compute, Core Decides" contract step 2 ("Re-derives the obligation
set ... independently using its own reference traversal (the recursive CTE
above)"). This is what routers/patent_ingest_p2.py calls to cross-check every
satellite-submitted derivation.

Two execution paths, selected automatically by SQLAlchemy dialect:

  * Postgres (production): executes REFERENCE_CTE_SQL_POSTGRES, the LITERAL
    recursive CTE from PATENT.md ("Traversal Algorithm" section), verbatim --
    ARRAY / = ANY(path) Postgres syntax included.
  * Anything else (e.g. SQLite, used by this repo's own tests since no live
    Postgres is available here): falls back to `_derive_obligations_pure_python`,
    a dependency-free re-implementation of the EXACT SAME algorithm (same
    seed/recursive-step/guard/depth-bound/terminal-filter semantics as the SQL
    above), driven off nodes/edges loaded from the same governance_graph_nodes/
    governance_graph_edges tables via the ORM. This is intentionally NOT a
    "simplified" traversal -- it mirrors the CTE's path-based cycle guard
    exactly (including the same subtle bug/feature the CTE has: a path guard
    checked against the row's OLD path, before the current node is appended --
    see tests/fixtures/reference_cte.py's SQLite/JSON1 port, which encodes the
    identical guard via `NOT EXISTS (SELECT 1 FROM json_each(og.path) ...)`).

This module has ZERO import on src/p2_satellite or tests/fixtures -- it must
not depend on the satellite. core-side-patch/tests/test_core_patch_reference_
traversal_cte.py is the only place in this repo that cross-checks this module's
pure-Python fallback against tests/fixtures/reference_cte.py's SQLite port and
tests/fixtures/expected_traversal.py's hand-computed expectations -- that
cross-check is test-only scaffolding standing in for "core's independent
implementation was validated against the same fixture graph as the satellite's
traversal.py", per CLAUDE_CODE_GOAL_PROMPT.md's integration checkpoint 1. It is
NOT something this shipped module relies on at runtime.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from models import GovernanceGraphEdge, GovernanceGraphNode

# Verbatim from PATENT.md's "Traversal Algorithm (core-side recursive CTE
# reference implementation)" section. Do not "clean up" this string -- fidelity
# to the patent document's literal claim language matters here.
REFERENCE_CTE_SQL_POSTGRES = """
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
"""


def derive_obligations_reference(
    session: Session, ai_system_node_id: Any, max_traversal_depth: int
) -> dict[str, list[str]]:
    """Independently re-derive {"derived_obligations": [...], "derived_controls": [...]}
    for one ai_system's graph node, per PATENT.md's reference CTE.

    On a real Postgres session this executes REFERENCE_CTE_SQL_POSTGRES
    verbatim. On any other dialect (this repo's own tests use SQLite, since no
    live Postgres is available -- see ASSUMPTIONS.md) it falls back to an
    algorithmically-identical pure-Python traversal over the same tables,
    loaded via the ORM. A human merging this patch should add a Postgres-only
    integration test against a real database before trusting this in prod.
    """
    bind = session.get_bind()
    dialect_name = bind.dialect.name if bind is not None else "postgresql"

    if dialect_name == "postgresql":
        rows = session.execute(
            text(REFERENCE_CTE_SQL_POSTGRES),
            {"ai_system_node_id": ai_system_node_id, "max_traversal_depth": max_traversal_depth},
        ).fetchall()
        derived_obligations = sorted({row.node_key for row in rows if row.node_type == "obligation"})
        derived_controls = sorted({row.node_key for row in rows if row.node_type == "control_type"})
        return {"derived_obligations": derived_obligations, "derived_controls": derived_controls}

    nodes, edges = _load_active_nodes_edges_via_orm(session)
    return _derive_obligations_pure_python(nodes, edges, ai_system_node_id, max_traversal_depth)


def _load_active_nodes_edges_via_orm(
    session: Session,
) -> tuple[list[tuple[Any, str, str]], list[tuple[Any, Any, str, bool]]]:
    node_rows = session.query(GovernanceGraphNode.id, GovernanceGraphNode.node_type, GovernanceGraphNode.node_key).all()
    edge_rows = session.query(
        GovernanceGraphEdge.source_node_id,
        GovernanceGraphEdge.target_node_id,
        GovernanceGraphEdge.edge_type,
        GovernanceGraphEdge.is_active,
    ).all()
    nodes = [(nid, ntype, nkey) for (nid, ntype, nkey) in node_rows]
    edges = [(src, tgt, etype, bool(active)) for (src, tgt, etype, active) in edge_rows]
    return nodes, edges


def _derive_obligations_pure_python(
    nodes: Iterable[tuple[Any, str, str]],
    edges: Iterable[tuple[Any, Any, str, bool]],
    ai_system_node_id: Any,
    max_traversal_depth: int,
) -> dict[str, list[str]]:
    """Dependency-free re-implementation of REFERENCE_CTE_SQL_POSTGRES's exact
    semantics: seed = direct outgoing edges from ai_system_node_id (depth 1),
    recursive step follows outgoing edges of previously-reached nodes while
    depth < max_traversal_depth, guarding against revisiting any node already
    on the current path (this mirrors the CTE's `path` accumulator, including
    that the guard is evaluated against the path BEFORE the current node is
    appended -- see module docstring). Terminal node types 'obligation' and
    'control_type' are collected and deduplicated by node_key, matching the
    CTE's final `SELECT * FROM obligation_graph WHERE node_type IN (...)`.
    """
    node_lookup: dict[Any, tuple[str, str]] = {nid: (ntype, nkey) for nid, ntype, nkey in nodes}
    outgoing: dict[Any, list[Any]] = {}
    for source_id, target_id, _edge_type, is_active in edges:
        if is_active:
            outgoing.setdefault(source_id, []).append(target_id)

    derived_obligations: set[str] = set()
    derived_controls: set[str] = set()

    # Stack entries: (current_node_id, path_of_ancestors_excluding_current, depth_reached)
    stack: list[tuple[Any, list[Any], int]] = [(ai_system_node_id, [], 0)]
    while stack:
        current_id, path, depth = stack.pop()
        if depth >= max_traversal_depth:
            continue
        for target_id in outgoing.get(current_id, []):
            if target_id in path:
                continue  # cycle guard, matches CTE's NOT (target = ANY(path))
            node_type, node_key = node_lookup.get(target_id, (None, None))
            if node_key is not None and node_type == "obligation":
                derived_obligations.add(node_key)
            elif node_key is not None and node_type == "control_type":
                derived_controls.add(node_key)
            stack.append((target_id, path + [current_id], depth + 1))

    return {
        "derived_obligations": sorted(derived_obligations),
        "derived_controls": sorted(derived_controls),
    }
