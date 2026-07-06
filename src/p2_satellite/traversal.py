"""
NetworkX equivalent of the reference recursive CTE in PATENT.md's
"Traversal Algorithm (core-side recursive CTE reference implementation)".

This module mirrors the CTE's semantics exactly, including its cycle guard
(`NOT (e.target_node_id = ANY(og.path))`) and its depth bound
(`og.depth < :max_traversal_depth`), so satellite output can be independently
cross-checked by core (see PATENT.md "Satellites Compute, Core Decides").

MAX_TRAVERSAL_DEPTH is config, not a magic number: this module reads
`settings.max_traversal_depth` from src.p2_satellite.config in exactly one
place (the default-argument resolution inside derive_obligations). Every
loop/recursion bound downstream references the local `max_traversal_depth`
variable, never settings directly and never a bare literal.
"""

from __future__ import annotations

import logging
from typing import Any

import networkx as nx

from src.p2_satellite import observability, schema
from src.p2_satellite.config import settings

logger = observability.get_logger(__name__)


def _edge_is_active(edge_data: dict[str, Any]) -> bool:
    """Missing is_active is treated as active, matching the CTE's implicit
    assumption that only explicitly inactive edges (is_active = false) are
    excluded."""
    return bool(edge_data.get("is_active", True))


def derive_obligations(
    graph: nx.DiGraph,
    ai_system_node_id: str,
    max_traversal_depth: int | None = None,
) -> dict[str, Any]:
    """Traverse `graph` from `ai_system_node_id`, following active directed
    edges, mirroring PATENT.md's reference recursive CTE.

    Returns a dict shaped like the reference CTE's cross-checkable output:
        {
            "ai_system_id": <node_key of ai_system_node_id>,
            "derived_obligations": sorted unique obligation node_keys,
            "derived_controls": sorted unique control_type node_keys,
            "graph_path": list of distinct paths (list of node_ids) that led
                          to a terminal node,
            "methodology_version": settings.methodology_version,
            "incomplete_coverage": list of {"node_type": "regulation",
                          "node_key": <key>} dicts, one per regulation node
                          reached during this traversal that has zero active
                          outgoing regulation_requires edges. This flags
                          "regulation not fully seeded yet" as distinct from
                          "genuinely nothing applies" -- both look like an
                          empty derived_obligations list otherwise. Scoped to
                          regulation nodes only: a risk_tier with no
                          risk_tier_adds edges is normal (e.g. "minimal"/
                          "limited" tiers), not a data gap.
        }
    """
    # The ONE place this module reads settings.max_traversal_depth.
    if max_traversal_depth is None:
        max_traversal_depth = settings.max_traversal_depth

    _, ai_system_key = schema.split_node_id(ai_system_node_id)

    with observability.timed_stage(logger, "traversal", ai_system_id=ai_system_key):
        result = _traverse(graph, ai_system_node_id, ai_system_key, max_traversal_depth)

    if result["incomplete_coverage"]:
        observability.log_event(
            logger,
            logging.WARNING,
            "traversal.coverage_gap_detected",
            ai_system_id=ai_system_key,
            incomplete_coverage=result["incomplete_coverage"],
        )

    return result


def _traverse(
    graph: nx.DiGraph,
    ai_system_node_id: str,
    ai_system_key: str,
    max_traversal_depth: int,
) -> dict[str, Any]:
    """The actual traversal walk, split out from derive_obligations() only so
    the public function can wrap it in a single timed_stage + do the
    post-traversal coverage-gap log without an extra indentation level."""
    obligation_keys: set[str] = set()
    control_keys: set[str] = set()
    terminal_paths: list[tuple[str, ...]] = []
    seen_terminal_paths: set[tuple[str, ...]] = set()
    # Regulation nodes actually reached while walking (present in some
    # walked path), regardless of whether they were obligation/control
    # terminals themselves -- used to compute incomplete_coverage below.
    visited_regulation_ids: set[str] = set()

    # Iterative stack of (current_node, path_so_far, depth) — path_so_far is
    # the sequence of node ids from ai_system_node_id up to and including
    # current_node, matching the CTE's per-path `path` array semantics
    # (CTE's `path` holds source ids visited before reaching target; here we
    # track the full walked path, which is equivalent for the cycle guard:
    # "is target already in this path").
    #
    # depth 1 == the first hop out of ai_system_node_id, matching the CTE's
    # base case (`1 as depth`).
    stack: list[tuple[str, tuple[str, ...], int]] = [(ai_system_node_id, (ai_system_node_id,), 0)]

    if ai_system_node_id not in graph:
        # Nothing reachable; still return a well-shaped empty result.
        return {
            "ai_system_id": ai_system_key,
            "derived_obligations": [],
            "derived_controls": [],
            "graph_path": [],
            "methodology_version": settings.methodology_version,
            "incomplete_coverage": [],
        }

    while stack:
        current_node, path, depth = stack.pop()

        for _, target_node, edge_data in graph.out_edges(current_node, data=True):
            if not _edge_is_active(edge_data):
                continue
            # Cycle guard: mirrors CTE's `NOT (e.target_node_id = ANY(og.path))`.
            if target_node in path:
                continue

            new_depth = depth + 1
            new_path = path + (target_node,)

            node_data = graph.nodes[target_node]
            node_type = node_data.get("node_type")
            node_key = node_data.get("node_key")

            if node_type == schema.NODE_REGULATION:
                visited_regulation_ids.add(target_node)

            if node_type in schema.TERMINAL_NODE_TYPES:
                if node_type == schema.NODE_OBLIGATION:
                    obligation_keys.add(node_key)
                elif node_type == schema.NODE_CONTROL_TYPE:
                    control_keys.add(node_key)
                if new_path not in seen_terminal_paths:
                    seen_terminal_paths.add(new_path)
                    terminal_paths.append(new_path)

            # Continuation condition: mirrors CTE's `og.depth < :max_traversal_depth`.
            if new_depth < max_traversal_depth:
                stack.append((target_node, new_path, new_depth))

    incomplete_coverage: list[dict[str, str]] = []
    for reg_node_id in sorted(visited_regulation_ids):
        has_requires_edge = any(
            _edge_is_active(edge_data) and edge_data.get("edge_type") == schema.EDGE_REGULATION_REQUIRES
            for _, _, edge_data in graph.out_edges(reg_node_id, data=True)
        )
        if not has_requires_edge:
            _, reg_key = schema.split_node_id(reg_node_id)
            incomplete_coverage.append({"node_type": schema.NODE_REGULATION, "node_key": reg_key})

    return {
        "ai_system_id": ai_system_key,
        "derived_obligations": sorted(obligation_keys),
        "derived_controls": sorted(control_keys),
        "graph_path": [list(p) for p in terminal_paths],
        "methodology_version": settings.methodology_version,
        "incomplete_coverage": incomplete_coverage,
    }
