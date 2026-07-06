"""
Small, CI-safe regression guard for graph_builder.build_graph() +
traversal.derive_obligations() performance -- NOT the full 1,000/10,000
benchmark (see scripts/benchmark_scale.py and PERFORMANCE.md for those real
numbers, measured manually since 10,000-system timing isn't a reliable
pass/fail gate on shared/noisy CI hardware).

This test runs at a much smaller N (200) with a generous threshold, purely to
catch an accidental algorithmic regression (e.g. someone reintroducing
O(n^2) behavior) between full benchmark runs -- not to assert a tight
performance SLA.
"""

from __future__ import annotations

import time

from src.p2_satellite import schema
from src.p2_satellite.graph_builder import build_graph
from src.p2_satellite.traversal import derive_obligations
from tests.benchmark.fixtures import JURISDICTIONS_EXPORT, REGULATIONS_CATALOG_EXPORT

N = 200
# Generous on purpose -- this is a regression smoke test, not a tight SLA.
# The real benchmark (scripts/benchmark_scale.py) measured ~0.15s combined
# for N=1,000 on reference hardware; 5 seconds for N=200 leaves enormous
# headroom for slow/shared CI runners while still catching an O(n^2) blowup.
MAX_COMBINED_SECONDS = 5.0


def _make_synthetic_ai_systems(n: int) -> dict:
    items = []
    for i in range(n):
        items.append(
            {
                "id": f"sys-smoke-{i:04d}",
                "name": f"Smoke Test System {i}",
                "geographic_scope": ["EU"] if i % 2 == 0 else ["IN"],
                "data_categories": ["biometric", "health"] if i % 3 == 0 else ["personal"],
                "risk_tier": "high" if i % 5 == 0 else "limited",
                "deployment_status": "active",
            }
        )
    return {"items": items}


def test_graph_build_and_traversal_stay_fast_at_moderate_scale():
    ai_systems = _make_synthetic_ai_systems(N)

    start = time.perf_counter()
    graph = build_graph(ai_systems, REGULATIONS_CATALOG_EXPORT, JURISDICTIONS_EXPORT)

    ai_system_node_ids = [
        node_id for node_id, data in graph.nodes(data=True) if data.get("node_type") == schema.NODE_AI_SYSTEM
    ]
    assert len(ai_system_node_ids) == N

    for node_id in ai_system_node_ids:
        derive_obligations(graph, node_id)

    elapsed = time.perf_counter() - start
    assert elapsed < MAX_COMBINED_SECONDS, (
        f"build_graph() + {N} derive_obligations() calls took {elapsed:.2f}s, "
        f"expected well under {MAX_COMBINED_SECONDS}s -- possible performance regression"
    )
