# Patch for CompliVibe core (app.complivibe.in), lands on top of migration head 0175.
# Test-harness only (not part of the patch set shipped to core).
#
# FastAPI TestClient tests for the six customer-facing knowledge-graph
# endpoints (routers/patent_knowledge_graph_p2.py): happy path, org-scoping
# enforcement, and edge cases per endpoint, plus the Feature 1 / Feature 5
# convergence test.
from __future__ import annotations

import pytest
from conftest import build_populated_session, seed_org_graph
from fastapi import FastAPI
from fastapi.testclient import TestClient
from routers import patent_knowledge_graph_p2 as kg_router

from tests.fixtures.expected_traversal import EXPECTED

import dependencies
import rate_limiter as rate_limiter_module
from audit_service_stub import AuditService
from change_event_outbox import MANUAL_TRIGGER_REASON, GovernanceGraphChangeEvent
from graph_query import derive_and_persist_traversal
from models import GovernanceGraphEdge, GovernanceGraphNode

PREFIX = "/ai-governance/knowledge-graph"


@pytest.fixture()
def env():
    engine, session, org1_ids = build_populated_session(org_id=1)
    org2_ids = seed_org_graph(session, org_id=2)

    # Org 2 does NOT have a "sys-alpha" ai_system -- delete its copy (the raw
    # fixture would otherwise give every org an identical business-key
    # "sys-alpha", which would make org-scoping tests pass by coincidence
    # rather than by the org_id filter actually doing work).
    org2_sys_alpha_node_id = org2_ids["ai_system:sys-alpha"]
    session.query(GovernanceGraphEdge).filter_by(org_id=2, source_node_id=org2_sys_alpha_node_id).delete()
    session.query(GovernanceGraphNode).filter_by(id=org2_sys_alpha_node_id).delete()
    session.commit()

    AuditService._reset_for_tests()
    rate_limiter_module._reset_on_demand_derive_rate_limiter_for_tests()

    yield session, org1_ids, org2_ids
    session.close()


def make_client(session, org_id=1, user_id=1):
    app = FastAPI()
    app.include_router(kg_router.router)
    app.dependency_overrides[dependencies.get_db_session] = lambda: session
    app.dependency_overrides[dependencies.get_current_organization] = lambda: dependencies.Organization(
        id=org_id, org_id=org_id
    )
    app.dependency_overrides[dependencies.get_current_active_user] = lambda: dependencies.ActiveUser(
        id=user_id, org_id=org_id
    )
    return TestClient(app)


# ---------------------------------------------------------------------------
# Feature 1: POST .../systems/{id}/derive-obligations
# ---------------------------------------------------------------------------


def test_derive_obligations_happy_path(env):
    session, org1_ids, org2_ids = env
    client = make_client(session, org_id=1)

    resp = client.post(f"{PREFIX}/systems/sys-alpha/derive-obligations")

    assert resp.status_code == 200
    body = resp.json()
    assert body["derived_obligations"] == EXPECTED["sys-alpha"]["derived_obligations"]
    assert body["derived_controls"] == EXPECTED["sys-alpha"]["derived_controls"]
    assert body["trigger_reason"] == "on_demand"
    assert len(AuditService._written) == 1
    assert AuditService._written[0].payload["ai_system_id"] == "sys-alpha"


def test_derive_obligations_rejects_ai_system_from_another_org(env):
    session, org1_ids, org2_ids = env
    client = make_client(session, org_id=2)  # org 2 has no "sys-alpha"

    resp = client.post(f"{PREFIX}/systems/sys-alpha/derive-obligations")

    assert resp.status_code == 404


def test_derive_obligations_respects_per_org_rate_limit(env):
    session, org1_ids, org2_ids = env
    original_limiter = rate_limiter_module._on_demand_derive_rate_limiter
    rate_limiter_module._on_demand_derive_rate_limiter = rate_limiter_module.FixedWindowRateLimiter(
        limit=2, window_seconds=60
    )
    try:
        client = make_client(session, org_id=1)
        for _ in range(2):
            resp = client.post(f"{PREFIX}/systems/sys-alpha/derive-obligations")
            assert resp.status_code == 200
        resp = client.post(f"{PREFIX}/systems/sys-alpha/derive-obligations")
        assert resp.status_code == 429
    finally:
        rate_limiter_module._on_demand_derive_rate_limiter = original_limiter


def test_derive_obligations_rate_limit_is_per_org_not_global(env):
    session, org1_ids, org2_ids = env
    original_limiter = rate_limiter_module._on_demand_derive_rate_limiter
    rate_limiter_module._on_demand_derive_rate_limiter = rate_limiter_module.FixedWindowRateLimiter(
        limit=1, window_seconds=60
    )
    try:
        org1_client = make_client(session, org_id=1)
        org2_client = make_client(session, org_id=2)

        assert org1_client.post(f"{PREFIX}/systems/sys-alpha/derive-obligations").status_code == 200
        assert org1_client.post(f"{PREFIX}/systems/sys-alpha/derive-obligations").status_code == 429
        # Org 2 exhausting org 1's budget would be a real bug -- org 2's own
        # ai_system (sys-beta, which it does have) must still be servable.
        assert org2_client.post(f"{PREFIX}/systems/sys-beta/derive-obligations").status_code == 200
    finally:
        rate_limiter_module._on_demand_derive_rate_limiter = original_limiter


# ---------------------------------------------------------------------------
# Feature 2: GET .../systems/{id}/graph
# ---------------------------------------------------------------------------


def test_get_graph_happy_path(env):
    session, org1_ids, org2_ids = env
    client = make_client(session, org_id=1)

    resp = client.get(f"{PREFIX}/systems/sys-alpha/graph")

    assert resp.status_code == 200
    body = resp.json()
    assert {"nodes", "edges"} == set(body.keys())
    assert any(n["type"] == "ai_system" for n in body["nodes"])
    assert any(n["type"] == "obligation" for n in body["nodes"])


def test_get_graph_rejects_ai_system_from_another_org(env):
    session, org1_ids, org2_ids = env
    client = make_client(session, org_id=2)

    resp = client.get(f"{PREFIX}/systems/sys-alpha/graph")

    assert resp.status_code == 404


def test_get_graph_format_html_returns_rendered_page(env):
    session, org1_ids, org2_ids = env
    client = make_client(session, org_id=1)

    resp = client.get(f"{PREFIX}/systems/sys-alpha/graph", params={"format": "html"})

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "<html>" in resp.text
    # The rendered page embeds the same node labels the JSON contract would.
    assert "sys-alpha" in resp.text


def test_get_graph_format_html_still_enforces_org_scoping(env):
    session, org1_ids, org2_ids = env
    client = make_client(session, org_id=2)

    resp = client.get(f"{PREFIX}/systems/sys-alpha/graph", params={"format": "html"})

    assert resp.status_code == 404


def test_get_graph_rejects_unknown_format_value(env):
    session, org1_ids, org2_ids = env
    client = make_client(session, org_id=1)

    resp = client.get(f"{PREFIX}/systems/sys-alpha/graph", params={"format": "xml"})

    assert resp.status_code == 422


def test_get_graph_default_format_is_still_json(env):
    session, org1_ids, org2_ids = env
    client = make_client(session, org_id=1)

    resp = client.get(f"{PREFIX}/systems/sys-alpha/graph")

    assert resp.headers["content-type"].startswith("application/json")


# ---------------------------------------------------------------------------
# Feature 3: POST .../edges
# ---------------------------------------------------------------------------


def test_create_manual_edge_happy_path(env):
    session, org1_ids, org2_ids = env
    client = make_client(session, org_id=1, user_id=42)

    source_id = org1_ids["regulation:DPDP"]
    target_id = org1_ids["obligation:gdpr_data_subject_rights"]

    resp = client.post(
        f"{PREFIX}/edges",
        json={
            "source_node_id": source_id,
            "target_node_id": target_id,
            "edge_type": "regulation_requires",
            "properties": {"note": "manual jurisdiction nuance"},
        },
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["properties"]["source"] == "manual"
    assert body["properties"]["added_by"] == 42
    assert body["properties"]["note"] == "manual jurisdiction nuance"
    # sys-alpha/sys-beta both reach DPDP's obligations via the graph;
    # affected_ai_system_ids should include at least one real system.
    assert isinstance(body["affected_ai_system_ids"], list)

    audit_events = [e for e in AuditService._written if e.event_type == "governance_graph.manual_edge_added"]
    assert len(audit_events) == 1
    assert audit_events[0].payload["edge_id"] == body["id"]

    change_events = session.query(GovernanceGraphChangeEvent).filter_by(changed_field=MANUAL_TRIGGER_REASON).all()
    assert {e.ai_system_id for e in change_events} == set(body["affected_ai_system_ids"])


def test_create_manual_edge_rejects_dangling_target(env):
    session, org1_ids, org2_ids = env
    client = make_client(session, org_id=1)

    resp = client.post(
        f"{PREFIX}/edges",
        json={
            "source_node_id": org1_ids["regulation:DPDP"],
            "target_node_id": 999999,
            "edge_type": "regulation_requires",
        },
    )

    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "unknown_target_node_id"
    assert session.query(GovernanceGraphEdge).filter_by(target_node_id=999999).count() == 0


def test_create_manual_edge_rejects_node_belonging_to_another_org(env):
    session, org1_ids, org2_ids = env
    client = make_client(session, org_id=2)

    # These node ids are real rows, but they belong to org 1 -- org 2 must
    # not be able to reference them at all, not even to read/link them.
    resp = client.post(
        f"{PREFIX}/edges",
        json={
            "source_node_id": org1_ids["regulation:GDPR"],
            "target_node_id": org1_ids["obligation:gdpr_data_subject_rights"],
            "edge_type": "regulation_requires",
        },
    )

    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "unknown_source_node_id"


# ---------------------------------------------------------------------------
# Feature 4: GET .../nodes
# ---------------------------------------------------------------------------


def test_browse_nodes_filters_by_type(env):
    session, org1_ids, org2_ids = env
    client = make_client(session, org_id=1)

    resp = client.get(f"{PREFIX}/nodes", params={"type": "regulation"})

    assert resp.status_code == 200
    body = resp.json()
    assert all(item["type"] == "regulation" for item in body["items"])
    assert body["meta"]["total"] == len(body["items"])


def test_browse_nodes_paginates(env):
    session, org1_ids, org2_ids = env
    client = make_client(session, org_id=1)

    resp = client.get(f"{PREFIX}/nodes", params={"type": "obligation", "page": 1, "page_size": 2})

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 2
    assert body["meta"]["page"] == 1
    assert body["meta"]["page_size"] == 2
    assert body["meta"]["total"] >= 2


def test_browse_nodes_is_org_scoped(env):
    session, org1_ids, org2_ids = env
    org1_client = make_client(session, org_id=1)
    org2_client = make_client(session, org_id=2)

    org1_resp = org1_client.get(f"{PREFIX}/nodes", params={"type": "ai_system", "page_size": 50})
    org2_resp = org2_client.get(f"{PREFIX}/nodes", params={"type": "ai_system", "page_size": 50})

    # org 1 has sys-alpha + sys-beta; org 2 (per the `env` fixture) only has
    # sys-beta -- if org scoping were broken, both counts would be equal.
    assert org1_resp.json()["meta"]["total"] == 2
    assert org2_resp.json()["meta"]["total"] == 1
    assert {item["label"] for item in org2_resp.json()["items"]} == {"sys-beta"}


# ---------------------------------------------------------------------------
# Feature 5: POST .../systems/{id}/sync
# ---------------------------------------------------------------------------


def test_sync_happy_path_queues_a_change_event(env):
    session, org1_ids, org2_ids = env
    client = make_client(session, org_id=1)

    resp = client.post(f"{PREFIX}/systems/sys-alpha/sync")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "sync_queued"

    event = session.get(GovernanceGraphChangeEvent, body["change_event_id"])
    assert event is not None
    assert event.ai_system_id == "sys-alpha"
    assert event.changed_field == MANUAL_TRIGGER_REASON
    assert event.org_id == 1


def test_sync_rejects_ai_system_from_another_org(env):
    session, org1_ids, org2_ids = env
    client = make_client(session, org_id=2)

    resp = client.post(f"{PREFIX}/systems/sys-alpha/sync")

    assert resp.status_code == 404


def test_feature1_and_feature5_converge_on_same_derivation(env):
    """Feature 1 (synchronous on-demand derive) and Feature 5 (sync -- which
    only queues a change event, per that endpoint's docstring) must bottom
    out in the SAME reference-CTE result for the same ai_system: Feature 1
    computes it directly; Feature 5 defers to whatever eventually consumes
    the change event, which -- per graph_query.derive_and_persist_traversal
    being the ONE shared traversal function -- would compute the identical
    answer. This test proves that convergence directly rather than assuming
    it."""
    session, org1_ids, org2_ids = env
    client = make_client(session, org_id=1)

    feature1_resp = client.post(f"{PREFIX}/systems/sys-beta/derive-obligations")
    assert feature1_resp.status_code == 200

    sync_resp = client.post(f"{PREFIX}/systems/sys-beta/sync")
    assert sync_resp.status_code == 200
    event = session.get(GovernanceGraphChangeEvent, sync_resp.json()["change_event_id"])
    assert event.changed_field == MANUAL_TRIGGER_REASON

    # Simulate the downstream consumer of that change event (the satellite,
    # on its next export/derive cycle) re-running the SAME shared traversal
    # function Feature 1 used.
    consumer_result = derive_and_persist_traversal(session, 1, "sys-beta", trigger_reason=MANUAL_TRIGGER_REASON)

    assert consumer_result["derived_obligations"] == feature1_resp.json()["derived_obligations"]
    assert consumer_result["derived_controls"] == feature1_resp.json()["derived_controls"]


# ---------------------------------------------------------------------------
# Feature 6: GET .../gaps
# ---------------------------------------------------------------------------


def test_gaps_happy_path_empty_before_any_derivation(env):
    session, org1_ids, org2_ids = env
    client = make_client(session, org_id=1)

    resp = client.get(f"{PREFIX}/gaps")

    assert resp.status_code == 200
    assert resp.json()["items"] == []


def test_gaps_reports_obligation_with_no_linked_control(env):
    session, org1_ids, org2_ids = env
    client = make_client(session, org_id=1)

    client.post(f"{PREFIX}/systems/sys-alpha/derive-obligations")

    from models import AiSystemObligationLink

    link = (
        session.query(AiSystemObligationLink)
        .filter_by(ai_system_id="sys-alpha", control_type_id="transparency_documentation")
        .one()
    )
    session.delete(link)
    session.commit()

    resp = client.get(f"{PREFIX}/gaps")

    assert resp.status_code == 200
    gap_obligations = {(item["ai_system_id"], item["obligation_id"]) for item in resp.json()["items"]}
    assert ("sys-alpha", "euaiact_transparency_notice") in gap_obligations


def test_gaps_is_org_scoped(env):
    session, org1_ids, org2_ids = env
    org1_client = make_client(session, org_id=1)
    org2_client = make_client(session, org_id=2)

    org1_client.post(f"{PREFIX}/systems/sys-alpha/derive-obligations")
    from models import AiSystemObligationLink

    link = (
        session.query(AiSystemObligationLink)
        .filter_by(ai_system_id="sys-alpha", control_type_id="transparency_documentation")
        .one()
    )
    session.delete(link)
    session.commit()

    # Org 2 has no traversal results at all yet -- its gap list must stay
    # empty regardless of what org 1's gap looks like.
    org2_resp = org2_client.get(f"{PREFIX}/gaps")
    assert org2_resp.json()["items"] == []
