#!/usr/bin/env python3
"""
Performance benchmark: graph build + traversal time at realistic scale
(1,000 / 10,000 synthetic AI systems), per the production-hardening pass's
"Performance at realistic scale" requirement.

Not part of the default `pytest` run (see tests/performance/ for a small,
CI-safe regression guard at a much smaller N) -- this script is meant to be
run manually/in CI-on-demand to produce the numbers documented in
PERFORMANCE.md, since 10,000-system timing on shared/noisy CI hardware is not
a reliable pass/fail gate.

Usage:
    python3 scripts/benchmark_scale.py [N1 N2 ...]
    (defaults to 1000 10000 if no sizes given)

Synthetic data design: the regulations/jurisdictions catalog stays FIXED size
(3 regulations, ~11 obligations, ~7 control types -- the same shape as
tests/benchmark/fixtures.py's EU-India case) since real-world regulatory
catalogs don't scale with the number of AI systems; only the number of
ai_system nodes and their edges grows with N. This mirrors the actual
production shape: a large, growing system inventory traversing a
comparatively small, slow-changing regulatory graph.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.p2_satellite import schema  # noqa: E402
from src.p2_satellite.graph_builder import build_graph  # noqa: E402
from src.p2_satellite.traversal import derive_obligations  # noqa: E402
from tests.benchmark.fixtures import JURISDICTIONS_EXPORT, REGULATIONS_CATALOG_EXPORT  # noqa: E402

JURISDICTION_CYCLE = ["EU", "IN"]
DATA_CATEGORY_CYCLE = [
    ["biometric", "health"],
    ["personal"],
    ["biometric", "employment_data", "health"],
]
RISK_TIER_CYCLE = ["high", "limited", "minimal"]


def _make_synthetic_ai_systems(n: int) -> dict:
    items = []
    for i in range(n):
        items.append(
            {
                "id": f"sys-synth-{i:06d}",
                "name": f"Synthetic System {i}",
                "geographic_scope": [JURISDICTION_CYCLE[i % len(JURISDICTION_CYCLE)]],
                "data_categories": DATA_CATEGORY_CYCLE[i % len(DATA_CATEGORY_CYCLE)],
                "risk_tier": RISK_TIER_CYCLE[i % len(RISK_TIER_CYCLE)],
                "deployment_status": "active",
            }
        )
    return {"items": items}


def run_benchmark(n: int) -> None:
    ai_systems = _make_synthetic_ai_systems(n)

    t0 = time.perf_counter()
    graph = build_graph(ai_systems, REGULATIONS_CATALOG_EXPORT, JURISDICTIONS_EXPORT)
    build_time = time.perf_counter() - t0

    ai_system_node_ids = [
        node_id for node_id, data in graph.nodes(data=True) if data.get("node_type") == schema.NODE_AI_SYSTEM
    ]

    t0 = time.perf_counter()
    for node_id in ai_system_node_ids:
        derive_obligations(graph, node_id)
    total_traversal_time = time.perf_counter() - t0

    print(f"\n=== N = {n:,} AI systems ===")
    print(f"graph: {graph.number_of_nodes():,} nodes, {graph.number_of_edges():,} edges")
    print(f"build_graph():            {build_time:.4f}s")
    print(
        f"derive_obligations() x{n}: {total_traversal_time:.4f}s total, "
        f"{(total_traversal_time / n) * 1000:.4f}ms/system avg"
    )
    print(f"combined (build + all traversals): {build_time + total_traversal_time:.4f}s")


if __name__ == "__main__":
    sizes = [int(arg) for arg in sys.argv[1:]] or [1000, 10000]
    for n in sizes:
        run_benchmark(n)
