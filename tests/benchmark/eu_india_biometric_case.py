"""
Workstream E core deliverable: the reproducible benchmark proving
PATENT.md's "Required Evidence Before Filing" claim.

    "A reproducible benchmark demonstrating: static lookup table fails
    (returns nothing or wrong obligations) vs. graph traversal succeeds,
    on a documented novel-combination test case (EU-India joint-deployment
    biometric system, dual jurisdiction, dual purpose, split
    controller/processor role)."

Run with:  pytest tests/benchmark/
(see tests/benchmark/REPRODUCIBILITY.md for the full walkthrough).

This file runs BOTH methods against the exact same input
(fixtures.AI_SYSTEMS_EXPORT's single item, sys-globalid-biometric) and
asserts:
  1. The regulations catalog in fixtures.py actually encodes what its own
     EXPECTED_* constants claim (a drift guard -- if someone edits
     fixtures.py's obligations without updating the EXPECTED_* constants,
     this test catches it before it silently invalidates the benchmark).
  2. naive_static_lookup() -- representative of every GRC platform's
     hardcoded if/else per PATENT.md -- returns a demonstrably incomplete
     obligation set: it drops DPDP entirely, drops all EU AI Act high-risk
     additions, and drops the processor-specific obligations.
  3. graph_builder.build_graph() + traversal.derive_obligations() --
     completely unmodified, imported as-is -- returns the full, correct
     obligation set: all three regulations, both controller- and
     processor-tagged obligations, and the high-risk additions.
  4. The graph traversal result is a strict superset of the naive result,
     closing exactly the gap PATENT.md predicts a lookup table cannot
     close for a genuinely novel combination.
"""

from __future__ import annotations

import networkx as nx
import pytest

from src.p2_satellite import schema
from src.p2_satellite.graph_builder import build_graph
from src.p2_satellite.traversal import derive_obligations
from tests.benchmark.fixtures import (
    AI_SYSTEM_KEY,
    AI_SYSTEMS_EXPORT,
    EXPECTED_COMPLETE_CONTROLS,
    EXPECTED_COMPLETE_OBLIGATIONS,
    EXPECTED_CONTROLLER_OBLIGATIONS,
    EXPECTED_OBLIGATIONS_BY_REGULATION,
    EXPECTED_PROCESSOR_OBLIGATIONS,
    JURISDICTIONS_EXPORT,
    REGULATIONS_CATALOG_EXPORT,
)
from tests.benchmark.naive_static_lookup import naive_static_lookup

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ai_system_record() -> dict:
    (record,) = (item for item in AI_SYSTEMS_EXPORT["items"] if item["id"] == AI_SYSTEM_KEY)
    return record


def _obligation_role_map() -> dict[str, str]:
    """key -> properties.applies_to_role, read directly off
    fixtures.REGULATIONS_CATALOG_EXPORT (not off fixtures.EXPECTED_* -- this
    independently re-derives the role tags from the raw catalog so the test
    is not just checking the fixture's constants against themselves)."""
    roles: dict[str, str] = {}
    for reg in REGULATIONS_CATALOG_EXPORT["items"]:
        for ob in reg["requires_obligations"]:
            roles[ob["key"]] = ob["properties"]["applies_to_role"]
    for obligations in REGULATIONS_CATALOG_EXPORT["risk_tier_obligations"].values():
        for ob in obligations:
            roles[ob["key"]] = ob["properties"]["applies_to_role"]
    return roles


def _obligations_reachable_from_regulation_catalog() -> set[str]:
    """Independently reconstruct the full obligation-key set the catalog
    encodes, by reading requires_obligations + risk_tier_obligations
    directly (mirrors what regulation_requires/risk_tier_adds edges do in
    build_graph, but computed without networkx at all, as a cross-check)."""
    obligations: set[str] = set()
    for reg in REGULATIONS_CATALOG_EXPORT["items"]:
        for ob in reg["requires_obligations"]:
            obligations.add(ob["key"])
    for tier_obligations in REGULATIONS_CATALOG_EXPORT["risk_tier_obligations"].values():
        for ob in tier_obligations:
            obligations.add(ob["key"])
    return obligations


@pytest.fixture(scope="module")
def benchmark_graph() -> nx.DiGraph:
    return build_graph(AI_SYSTEMS_EXPORT, REGULATIONS_CATALOG_EXPORT, JURISDICTIONS_EXPORT)


@pytest.fixture(scope="module")
def naive_result() -> list[str]:
    return naive_static_lookup(_ai_system_record())


@pytest.fixture(scope="module")
def traversal_result(benchmark_graph: nx.DiGraph) -> dict:
    ai_system_node_id = schema.node_id(schema.NODE_AI_SYSTEM, AI_SYSTEM_KEY)
    return derive_obligations(benchmark_graph, ai_system_node_id, max_traversal_depth=6)


# ---------------------------------------------------------------------------
# 1. Fixture self-consistency (drift guard)
# ---------------------------------------------------------------------------


def test_fixture_expected_set_matches_the_catalog_it_claims_to_summarize() -> None:
    """If fixtures.py's EXPECTED_COMPLETE_OBLIGATIONS ever drifts from what
    REGULATIONS_CATALOG_EXPORT actually encodes, this fails first -- so a
    passing benchmark always means "the catalog produces what the docs say
    it produces", not "two hand-typed lists happen to agree"."""
    assert set(EXPECTED_COMPLETE_OBLIGATIONS) == _obligations_reachable_from_regulation_catalog()

    # Every regulation's obligation list adds up to the same complete set.
    union_by_regulation: set[str] = set()
    for keys in EXPECTED_OBLIGATIONS_BY_REGULATION.values():
        union_by_regulation.update(keys)
    assert union_by_regulation == set(EXPECTED_COMPLETE_OBLIGATIONS)

    # Role tags: exactly the controller/processor obligations we claim.
    roles = _obligation_role_map()
    controller_obligations = {k for k, role in roles.items() if role == "controller"}
    processor_obligations = {k for k, role in roles.items() if role == "processor"}
    assert controller_obligations == set(EXPECTED_CONTROLLER_OBLIGATIONS)
    assert processor_obligations == set(EXPECTED_PROCESSOR_OBLIGATIONS)


# ---------------------------------------------------------------------------
# 2. Naive static lookup: demonstrably incomplete
# ---------------------------------------------------------------------------


def test_naive_lookup_returns_nonempty_but_incomplete(naive_result: list[str]) -> None:
    """The naive table is not a strawman that returns nothing -- it
    confidently returns a plausible-looking, non-empty, WRONG answer,
    which is the more dangerous failure mode PATENT.md describes."""
    assert naive_result  # non-empty: it does not fail loudly
    assert set(naive_result) < set(EXPECTED_COMPLETE_OBLIGATIONS)  # strict subset: it fails silently


def test_naive_lookup_drops_dpdp_entirely(naive_result: list[str]) -> None:
    """Because the naive table only consults geographic_scope[0] == 'EU',
    the entire India/DPDP leg of this joint deployment is invisible to it."""
    dpdp_obligations = set(EXPECTED_OBLIGATIONS_BY_REGULATION["DPDP"])
    assert dpdp_obligations.isdisjoint(set(naive_result))


def test_naive_lookup_drops_high_risk_additions(naive_result: list[str]) -> None:
    """The naive table has no risk-tier concept, so conformity assessment /
    human oversight / biometric accuracy testing never appear."""
    high_risk_additions = {
        "euaiact_conformity_assessment",
        "euaiact_human_oversight",
        "euaiact_biometric_accuracy_and_bias_testing",
    }
    assert high_risk_additions.isdisjoint(set(naive_result))


def test_naive_lookup_drops_both_role_specific_obligations(naive_result: list[str]) -> None:
    """The naive table has no controller/processor role dimension at all --
    it drops BOTH the controller-specific and processor-specific duties."""
    role_specific = set(EXPECTED_CONTROLLER_OBLIGATIONS) | set(EXPECTED_PROCESSOR_OBLIGATIONS)
    assert role_specific.isdisjoint(set(naive_result))


# ---------------------------------------------------------------------------
# 3. Graph traversal: complete and correct
# ---------------------------------------------------------------------------


def test_graph_traversal_returns_the_complete_correct_set(traversal_result: dict) -> None:
    assert traversal_result["ai_system_id"] == AI_SYSTEM_KEY
    assert sorted(traversal_result["derived_obligations"]) == EXPECTED_COMPLETE_OBLIGATIONS
    assert sorted(traversal_result["derived_controls"]) == EXPECTED_COMPLETE_CONTROLS


def test_graph_traversal_spans_all_three_regulations(traversal_result: dict) -> None:
    derived = set(traversal_result["derived_obligations"])
    for regulation, obligations in EXPECTED_OBLIGATIONS_BY_REGULATION.items():
        missing = set(obligations) - derived
        assert not missing, f"{regulation} obligations missing from traversal result: {missing}"


def test_graph_traversal_includes_both_controller_and_processor_obligations(
    traversal_result: dict,
) -> None:
    derived = set(traversal_result["derived_obligations"])
    assert set(EXPECTED_CONTROLLER_OBLIGATIONS) <= derived
    assert set(EXPECTED_PROCESSOR_OBLIGATIONS) <= derived


def test_graph_traversal_methodology_and_shape(traversal_result: dict) -> None:
    from src.p2_satellite.config import settings

    assert traversal_result["methodology_version"] == settings.methodology_version
    assert isinstance(traversal_result["graph_path"], list)
    assert all(isinstance(p, list) for p in traversal_result["graph_path"])


# ---------------------------------------------------------------------------
# 4. The gap: graph traversal strictly closes what naive lookup misses
# ---------------------------------------------------------------------------


def test_graph_traversal_is_a_strict_superset_of_naive_lookup(naive_result: list[str], traversal_result: dict) -> None:
    naive_set = set(naive_result)
    full_set = set(traversal_result["derived_obligations"])
    assert naive_set < full_set

    gap = full_set - naive_set
    # The gap must include at least: all of DPDP, all high-risk additions,
    # and both role-specific obligations -- i.e. every dimension the naive
    # table structurally cannot see.
    assert set(EXPECTED_OBLIGATIONS_BY_REGULATION["DPDP"]) <= gap
    assert set(EXPECTED_CONTROLLER_OBLIGATIONS) <= gap
    assert set(EXPECTED_PROCESSOR_OBLIGATIONS) <= gap
