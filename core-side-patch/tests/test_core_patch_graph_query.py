# Patch for CompliVibe core (app.complivibe.in), lands on top of migration head 0175.
# Test-harness only (not part of the patch set shipped to core).
#
# Unit tests for graph_query.py -- the shared traversal/query layer behind
# the six customer-facing knowledge-graph endpoints
# (routers/patent_knowledge_graph_p2.py). No FastAPI/HTTP layer here; see
# test_core_patch_knowledge_graph_router.py for the endpoint-level tests.
from __future__ import annotations

import pytest
from conftest import build_populated_session, seed_org_graph

from tests.fixtures.expected_traversal import EXPECTED

from graph_query import (
    SELF_DERIVED_VALIDATION_STATUS,
    UnknownAiSystemError,
    derive_and_persist_traversal,
    find_coverage_gaps,
    find_upstream_ai_systems,
    get_subgraph,
    list_nodes,
    render_subgraph_html,
)
from models import AiSystemObligationLink, GovernanceGraphTraversalResult


@pytest.fixture()
def org1():
    engine, session, string_id_to_pk = build_populated_session(org_id=1)
    yield session, string_id_to_pk
    session.close()


@pytest.fixture()
def two_orgs():
    engine, session, org1_ids = build_populated_session(org_id=1)
    org2_ids = seed_org_graph(session, org_id=2)
    yield session, org1_ids, org2_ids
    session.close()


# ---------------------------------------------------------------------------
# derive_and_persist_traversal (Feature 1's shared traversal call)
# ---------------------------------------------------------------------------


def test_derive_and_persist_traversal_matches_expected_fixture(org1):
    session, _ids = org1

    result = derive_and_persist_traversal(session, 1, "sys-alpha", trigger_reason="on_demand")

    assert result["derived_obligations"] == EXPECTED["sys-alpha"]["derived_obligations"]
    assert result["derived_controls"] == EXPECTED["sys-alpha"]["derived_controls"]
    assert result["trigger_reason"] == "on_demand"
    assert result["validation_status"] == SELF_DERIVED_VALIDATION_STATUS

    row = session.query(GovernanceGraphTraversalResult).filter_by(ai_system_id="sys-alpha").one()
    assert row.trigger_reason == "on_demand"
    assert row.validation_status == SELF_DERIVED_VALIDATION_STATUS
    assert sorted(row.derived_obligations) == EXPECTED["sys-alpha"]["derived_obligations"]


def test_derive_and_persist_traversal_writes_obligation_links_by_default(org1):
    session, _ids = org1

    derive_and_persist_traversal(session, 1, "sys-alpha", trigger_reason="on_demand")

    linked_obligations = {
        row.obligation_id
        for row in session.query(AiSystemObligationLink).filter_by(ai_system_id="sys-alpha")
        if row.obligation_id is not None
    }
    assert linked_obligations == set(EXPECTED["sys-alpha"]["derived_obligations"])


def test_derive_and_persist_traversal_preview_only_skips_links(org1):
    session, _ids = org1

    derive_and_persist_traversal(session, 1, "sys-alpha", trigger_reason="on_demand", persist_links=False)

    assert session.query(AiSystemObligationLink).filter_by(ai_system_id="sys-alpha").count() == 0


def test_derive_and_persist_traversal_unknown_ai_system_raises(org1):
    session, _ids = org1

    with pytest.raises(UnknownAiSystemError):
        derive_and_persist_traversal(session, 1, "sys-does-not-exist", trigger_reason="on_demand")


def test_feature1_and_manual_sync_path_converge_on_same_result(org1):
    """Cross-validation checkpoint (same spirit as the original build's B+C
    convergence test): Feature 1's synchronous on-demand path and the
    reference traversal a downstream consumer of Feature 5's change event
    would eventually run are the SAME function -- calling it twice for the
    same ai_system, once tagged 'on_demand' and once tagged 'manual_sync',
    must produce byte-for-byte identical derived obligations/controls."""
    session, _ids = org1

    on_demand_result = derive_and_persist_traversal(session, 1, "sys-beta", trigger_reason="on_demand")
    manual_sync_result = derive_and_persist_traversal(session, 1, "sys-beta", trigger_reason="manual_sync")

    assert on_demand_result["derived_obligations"] == manual_sync_result["derived_obligations"]
    assert on_demand_result["derived_controls"] == manual_sync_result["derived_controls"]


# ---------------------------------------------------------------------------
# get_subgraph (Feature 2)
# ---------------------------------------------------------------------------


def test_get_subgraph_includes_intermediate_and_terminal_node_types(org1):
    session, _ids = org1

    subgraph = get_subgraph(session, 1, "sys-alpha")

    node_types = {node["type"] for node in subgraph["nodes"]}
    assert "ai_system" in node_types
    assert "jurisdiction" in node_types  # intermediate -- NOT a terminal derive_obligations_reference type
    assert "obligation" in node_types
    assert "control_type" in node_types

    for edge in subgraph["edges"]:
        assert set(edge.keys()) == {"source", "target", "type"}
    for node in subgraph["nodes"]:
        assert set(node.keys()) == {"id", "type", "label", "properties"}


def test_get_subgraph_respects_max_depth(org1):
    session, _ids = org1

    shallow = get_subgraph(session, 1, "sys-beta", max_depth=1)
    deep = get_subgraph(session, 1, "sys-beta", max_depth=6)

    # depth=1 can only reach ai_system's direct neighbors -- no obligations
    # (which sit several hops downstream through regulation/jurisdiction).
    assert "obligation" not in {n["type"] for n in shallow["nodes"]}
    assert "obligation" in {n["type"] for n in deep["nodes"]}
    assert len(deep["nodes"]) > len(shallow["nodes"])


def test_get_subgraph_unknown_ai_system_raises(org1):
    session, _ids = org1

    with pytest.raises(UnknownAiSystemError):
        get_subgraph(session, 1, "sys-does-not-exist")


def test_get_subgraph_is_org_scoped(two_orgs):
    session, org1_ids, org2_ids = two_orgs

    subgraph = get_subgraph(session, 1, "sys-alpha")

    org1_node_ids = set(org1_ids.values())
    for node in subgraph["nodes"]:
        assert node["id"] in org1_node_ids  # never an org-2 row, even though same fixture shape


# ---------------------------------------------------------------------------
# render_subgraph_html (Feature 2's additive ?format=html)
# ---------------------------------------------------------------------------


def test_render_subgraph_html_is_self_contained_and_touches_no_files(org1, tmp_path, monkeypatch):
    session, _ids = org1
    subgraph = get_subgraph(session, 1, "sys-alpha")

    monkeypatch.chdir(tmp_path)
    html = render_subgraph_html(subgraph)

    assert isinstance(html, str)
    assert "<html>" in html
    # No file written as a side effect -- see render_subgraph_html's docstring
    # on why cdn_resources="in_line" was chosen specifically for this.
    assert list(tmp_path.iterdir()) == []


def test_render_subgraph_html_embeds_every_node_label(org1):
    session, _ids = org1
    subgraph = get_subgraph(session, 1, "sys-alpha")

    html = render_subgraph_html(subgraph)

    for node in subgraph["nodes"]:
        assert node["label"] in html


# ---------------------------------------------------------------------------
# list_nodes (Feature 4)
# ---------------------------------------------------------------------------


def test_list_nodes_filters_by_type(org1):
    session, _ids = org1

    items, total = list_nodes(session, 1, node_type="regulation", page=1, page_size=50)

    assert total == len(items)
    assert all(item["type"] == "regulation" for item in items)
    assert total > 0


def test_list_nodes_paginates(org1):
    session, _ids = org1

    all_items, total = list_nodes(session, 1, node_type="obligation", page=1, page_size=1000)
    assert total >= 2

    page1, total1 = list_nodes(session, 1, node_type="obligation", page=1, page_size=2)
    page2, total2 = list_nodes(session, 1, node_type="obligation", page=2, page_size=2)

    assert total1 == total2 == total
    assert len(page1) == 2
    assert {item["id"] for item in page1}.isdisjoint({item["id"] for item in page2})


def test_list_nodes_is_org_scoped(two_orgs):
    session, org1_ids, org2_ids = two_orgs

    items, total = list_nodes(session, 1, node_type="ai_system", page=1, page_size=50)

    org1_node_ids = set(org1_ids.values())
    assert all(item["id"] in org1_node_ids for item in items)
    assert total == 2  # sys-alpha + sys-beta, org 1 only -- not doubled by org 2's identical fixture rows


# ---------------------------------------------------------------------------
# find_upstream_ai_systems (Feature 3's "who's affected by this new edge")
# ---------------------------------------------------------------------------


def test_find_upstream_ai_systems_finds_all_systems_reaching_shared_control(org1):
    session, ids = org1

    # access_control is a derived control for BOTH sys-alpha and sys-beta
    # (see tests/fixtures/expected_traversal.py) -- both systems' forward
    # traversal reaches this control_type node.
    access_control_node_id = ids["control_type:access_control"]

    upstream = find_upstream_ai_systems(session, 1, access_control_node_id)

    assert upstream == ["sys-alpha", "sys-beta"]


def test_find_upstream_ai_systems_finds_only_the_system_using_a_dpdp_specific_node(org1):
    session, ids = org1

    # consent_management is only a derived control for sys-beta (DPDP-only).
    consent_node_id = ids["control_type:consent_management"]

    upstream = find_upstream_ai_systems(session, 1, consent_node_id)

    assert upstream == ["sys-beta"]


def test_find_upstream_ai_systems_is_org_scoped(two_orgs):
    session, org1_ids, org2_ids = two_orgs

    org2_access_control_id = org2_ids["control_type:access_control"]

    # Querying with org_id=1 must never see org 2's node id at all.
    upstream = find_upstream_ai_systems(session, 1, org2_access_control_id)

    assert upstream == []


# ---------------------------------------------------------------------------
# find_coverage_gaps (Feature 6)
# ---------------------------------------------------------------------------


def test_find_coverage_gaps_empty_when_all_derived_obligations_have_linked_controls(org1):
    session, _ids = org1

    derive_and_persist_traversal(session, 1, "sys-alpha", trigger_reason="on_demand")
    derive_and_persist_traversal(session, 1, "sys-beta", trigger_reason="on_demand")

    gaps = find_coverage_gaps(session, 1)

    assert gaps == []


def test_find_coverage_gaps_reports_obligation_missing_its_required_control(org1):
    session, _ids = org1

    derive_and_persist_traversal(session, 1, "sys-alpha", trigger_reason="on_demand")

    # Simulate an incomplete rollout: remove the one linked control_type row
    # that satisfies euaiact_transparency_notice (needs
    # transparency_documentation, per tests/fixtures/reference_cte.py's
    # graph shape) so that obligation is no longer "covered."
    link = (
        session.query(AiSystemObligationLink)
        .filter_by(ai_system_id="sys-alpha", control_type_id="transparency_documentation")
        .one()
    )
    session.delete(link)
    session.commit()

    gaps = find_coverage_gaps(session, 1)

    gap_obligations = {(gap["ai_system_id"], gap["obligation_id"]) for gap in gaps}
    assert ("sys-alpha", "euaiact_transparency_notice") in gap_obligations


def test_find_coverage_gaps_skips_ai_systems_with_no_traversal_result(org1):
    session, _ids = org1
    # Neither sys-alpha nor sys-beta has a traversal_results row yet.
    assert find_coverage_gaps(session, 1) == []
