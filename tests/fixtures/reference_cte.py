"""
Ground-truth reference implementation of PATENT.md's recursive CTE, run
against SQLite (JSON1) instead of Postgres/pgvector, loaded from the exact
same tests/fixtures/sample_export.py data that feeds the satellite's
NetworkX graph builder.

This is what tests/unit/test_traversal_matches_reference.py cross-checks
src/p2_satellite/traversal.py against — it is the "byte-for-byte-comparable"
requirement from CLAUDE_CODE_GOAL_PROMPT.md's Workstream C / integration
checkpoint 1. Postgres `ARRAY` + `= ANY(path)` is ported to SQLite JSON1's
`json_each` + `NOT EXISTS`, which is semantically identical to the Postgres
guard in PATENT.md's reference CTE (line: "NOT (e.target_node_id = ANY(og.path))").

This module has ZERO dependency on src/p2_satellite — it is intentionally
self-contained so it plays the role of "core's independent implementation"
even though, in this satellite-only repo, both sides happen to live in one
place. See core-side-patch/ for the literal Postgres version of this CTE.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from src.p2_satellite import schema
from tests.fixtures.sample_export import (
    AI_SYSTEMS_EXPORT,
    JURISDICTIONS_EXPORT,
    REGULATIONS_CATALOG_EXPORT,
)

_CTE_SQL = """
WITH RECURSIVE obligation_graph(target_node_id, node_type, node_key, path, depth) AS (
    SELECT e.target_node_id, n.node_type, n.node_key,
           json_array(e.source_node_id), 1
    FROM governance_graph_edges e
    JOIN governance_graph_nodes n ON n.id = e.target_node_id
    WHERE e.source_node_id = :ai_system_node_id
      AND e.is_active = 1

    UNION ALL

    SELECT e.target_node_id, n.node_type, n.node_key,
           json_insert(og.path, '$[#]', e.source_node_id), og.depth + 1
    FROM governance_graph_edges e
    JOIN governance_graph_nodes n ON n.id = e.target_node_id
    JOIN obligation_graph og ON og.target_node_id = e.source_node_id
    WHERE og.depth < :max_traversal_depth
      AND NOT EXISTS (
            SELECT 1 FROM json_each(og.path) WHERE json_each.value = e.target_node_id
      )
      AND e.is_active = 1
)
SELECT DISTINCT target_node_id, node_type, node_key
FROM obligation_graph
WHERE node_type IN ('obligation', 'control_type')
"""


def _build_node_edge_rows() -> tuple[list[tuple], list[tuple]]:
    """Flatten sample_export.py into (nodes, edges) rows keyed by schema.node_id."""
    nodes: dict[str, tuple] = {}
    edges: list[tuple] = []

    def add_node(node_type: str, node_key: str) -> str:
        nid = schema.node_id(node_type, node_key)
        nodes[nid] = (nid, node_type, node_key)
        return nid

    def add_edge(edge_type: str, source_id: str, target_id: str) -> None:
        schema.validate_edge_type(edge_type)
        edges.append((source_id, target_id, edge_type, 1))

    # jurisdictions + jurisdiction_has -> regulation
    for j in JURISDICTIONS_EXPORT["items"]:
        jid = add_node(schema.NODE_JURISDICTION, j["key"])
        for reg_key in j["regulations"]:
            rid = add_node(schema.NODE_REGULATION, reg_key)
            add_edge(schema.EDGE_JURISDICTION_HAS, jid, rid)

    # regulations -> requires -> obligations -> needs -> control_types
    # + data_category -> data_triggers -> regulation
    # + risk_tier -> risk_tier_adds -> obligation
    for reg in REGULATIONS_CATALOG_EXPORT["items"]:
        rid = add_node(schema.NODE_REGULATION, reg["key"])
        for dc_key in reg["triggered_by_data_categories"]:
            dcid = add_node(schema.NODE_DATA_CATEGORY, dc_key)
            add_edge(schema.EDGE_DATA_TRIGGERS, dcid, rid)
        for ob in reg["requires_obligations"]:
            oid = add_node(schema.NODE_OBLIGATION, ob["key"])
            add_edge(schema.EDGE_REGULATION_REQUIRES, rid, oid)
            for ctrl_key in ob["needs_controls"]:
                cid = add_node(schema.NODE_CONTROL_TYPE, ctrl_key)
                add_edge(schema.EDGE_OBLIGATION_NEEDS, oid, cid)

    for tier_key, obligations in REGULATIONS_CATALOG_EXPORT["risk_tier_obligations"].items():
        tid = add_node(schema.NODE_RISK_TIER, tier_key)
        for ob in obligations:
            oid = add_node(schema.NODE_OBLIGATION, ob["key"])
            add_edge(schema.EDGE_RISK_TIER_ADDS, tid, oid)
            for ctrl_key in ob["needs_controls"]:
                cid = add_node(schema.NODE_CONTROL_TYPE, ctrl_key)
                add_edge(schema.EDGE_OBLIGATION_NEEDS, oid, cid)

    # ai systems -> uses/deploys_in/classified_as
    for sysd in AI_SYSTEMS_EXPORT["items"]:
        sid = add_node(schema.NODE_AI_SYSTEM, sysd["id"])
        for dc_key in sysd["data_categories"]:
            dcid = add_node(schema.NODE_DATA_CATEGORY, dc_key)
            add_edge(schema.EDGE_SYSTEM_USES, sid, dcid)
        for geo in sysd["geographic_scope"]:
            jid = add_node(schema.NODE_JURISDICTION, geo)
            add_edge(schema.EDGE_SYSTEM_DEPLOYS_IN, sid, jid)
        tid = add_node(schema.NODE_RISK_TIER, sysd["risk_tier"])
        add_edge(schema.EDGE_SYSTEM_CLASSIFIED_AS, sid, tid)

    return list(nodes.values()), edges


def reference_derive_obligations(ai_system_key: str, max_traversal_depth: int) -> dict[str, Any]:
    """Run the literal reference CTE (ported to SQLite/JSON1) for one ai_system.

    Returns {"derived_obligations": [...], "derived_controls": [...]} — both
    sorted lists of node_keys, deduplicated, matching the shape
    src/p2_satellite/traversal.py must produce.
    """
    nodes, edges = _build_node_edge_rows()
    con = sqlite3.connect(":memory:")
    try:
        con.execute("CREATE TABLE governance_graph_nodes (id TEXT PRIMARY KEY, node_type TEXT, node_key TEXT)")
        con.execute(
            "CREATE TABLE governance_graph_edges ("
            "source_node_id TEXT, target_node_id TEXT, edge_type TEXT, is_active INTEGER)"
        )
        con.executemany("INSERT INTO governance_graph_nodes VALUES (?, ?, ?)", nodes)
        con.executemany("INSERT INTO governance_graph_edges VALUES (?, ?, ?, ?)", edges)

        ai_system_node_id = schema.node_id(schema.NODE_AI_SYSTEM, ai_system_key)
        rows = con.execute(
            _CTE_SQL,
            {
                "ai_system_node_id": ai_system_node_id,
                "max_traversal_depth": max_traversal_depth,
            },
        ).fetchall()
    finally:
        con.close()

    derived_obligations = sorted({key for (_, ntype, key) in rows if ntype == schema.NODE_OBLIGATION})
    derived_controls = sorted({key for (_, ntype, key) in rows if ntype == schema.NODE_CONTROL_TYPE})
    return {"derived_obligations": derived_obligations, "derived_controls": derived_controls}
