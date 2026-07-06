"""
Shared graph schema conventions for the P2 satellite.

This is the single source of truth for node/edge type names and the node-id
convention, imported by graph_builder.py, traversal.py, embeddings.py, and
every test/fixture. Do not redefine these strings elsewhere.
"""

from __future__ import annotations

# --- Node types (PATENT.md "Graph Structure") ---------------------------------
NODE_AI_SYSTEM = "ai_system"
NODE_REGULATION = "regulation"
NODE_JURISDICTION = "jurisdiction"
NODE_DATA_CATEGORY = "data_category"
NODE_CONTROL_TYPE = "control_type"
NODE_OBLIGATION = "obligation"
NODE_RISK_TIER = "risk_tier"

NODE_TYPES = frozenset(
    {
        NODE_AI_SYSTEM,
        NODE_REGULATION,
        NODE_JURISDICTION,
        NODE_DATA_CATEGORY,
        NODE_CONTROL_TYPE,
        NODE_OBLIGATION,
        NODE_RISK_TIER,
    }
)

# --- Edge types (PATENT.md "Graph Structure") ----------------------------------
EDGE_SYSTEM_USES = "system_uses"  # ai_system -> data_category
EDGE_SYSTEM_DEPLOYS_IN = "system_deploys_in"  # ai_system -> jurisdiction
EDGE_DATA_TRIGGERS = "data_triggers"  # data_category -> regulation
EDGE_JURISDICTION_HAS = "jurisdiction_has"  # jurisdiction -> regulation
EDGE_REGULATION_REQUIRES = "regulation_requires"  # regulation -> obligation
EDGE_OBLIGATION_NEEDS = "obligation_needs"  # obligation -> control_type
EDGE_SYSTEM_CLASSIFIED_AS = "system_classified_as"  # ai_system -> risk_tier
EDGE_RISK_TIER_ADDS = "risk_tier_adds"  # risk_tier -> obligation

EDGE_TYPES = frozenset(
    {
        EDGE_SYSTEM_USES,
        EDGE_SYSTEM_DEPLOYS_IN,
        EDGE_DATA_TRIGGERS,
        EDGE_JURISDICTION_HAS,
        EDGE_REGULATION_REQUIRES,
        EDGE_OBLIGATION_NEEDS,
        EDGE_SYSTEM_CLASSIFIED_AS,
        EDGE_RISK_TIER_ADDS,
    }
)

# Node types that a traversal result is filtered down to, per the reference CTE:
#   "SELECT * FROM obligation_graph WHERE node_type IN ('obligation', 'control_type')"
TERMINAL_NODE_TYPES = frozenset({NODE_OBLIGATION, NODE_CONTROL_TYPE})


def node_id(node_type: str, node_key: str) -> str:
    """Canonical NetworkX node identifier: '{node_type}:{node_key}'.

    node_key is the natural/business key (e.g. 'GDPR', 'EU', 'biometric'),
    stable across syncs so re-ingesting the same export never duplicates nodes.
    """
    if node_type not in NODE_TYPES:
        raise ValueError(f"unknown node_type: {node_type!r}")
    return f"{node_type}:{node_key}"


def split_node_id(nid: str) -> tuple[str, str]:
    node_type, _, node_key = nid.partition(":")
    return node_type, node_key


def edge_key(edge_type: str, source_id: str, target_id: str) -> str:
    return f"{edge_type}:{source_id}->{target_id}"


def validate_edge_type(edge_type: str) -> None:
    if edge_type not in EDGE_TYPES:
        raise ValueError(f"unknown edge_type: {edge_type!r}")
