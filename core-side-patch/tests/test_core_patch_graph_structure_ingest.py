# Patch for CompliVibe core (app.complivibe.in), lands on top of migration head 0175.
# Test-harness only (not part of the patch set shipped to core).
#
# FastAPI TestClient tests for POST /api/v1/patent-ingest/p2/graph-structure
# -- the endpoint that closes ASSUMPTIONS.md item 22 by letting the
# satellite push its whole built graph (nodes + edges), upserted by natural
# key. Cross-validates against the satellite's own
# graph_builder.serialize_graph_structure() using tests/fixtures/sample_export.py
# -- same cross-boundary test-only convention already established by
# test_core_patch_ingest_router.py (see ASSUMPTIONS.md item 13).
from __future__ import annotations

import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from routers import patent_ingest_p2
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from src.p2_satellite import graph_builder, ingest_client
from tests.fixtures.sample_export import (
    AI_SYSTEMS_EXPORT,
    JURISDICTIONS_EXPORT,
    REGULATIONS_CATALOG_EXPORT,
)

import dependencies
from audit_service_stub import AuditService
from models import Base, GovernanceGraphEdge, GovernanceGraphNode


def _built_structure() -> dict:
    graph = graph_builder.build_graph(AI_SYSTEMS_EXPORT, REGULATIONS_CATALOG_EXPORT, JURISDICTIONS_EXPORT)
    return graph_builder.serialize_graph_structure(graph)


def _payload_from_structure(structure: dict) -> dict:
    body = dict(structure)
    body["structure_hash"] = ingest_client.compute_structure_hash(structure)
    return body


@pytest.fixture()
def session():
    engine = sa.create_engine("sqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine, tables=[GovernanceGraphNode.__table__, GovernanceGraphEdge.__table__])
    s = Session(engine)
    yield s
    s.close()


@pytest.fixture()
def client(session):
    AuditService._reset_for_tests()
    import rate_limiter as _rate_limiter_module

    _rate_limiter_module._reset_rate_limiter_for_tests()

    app = FastAPI()
    app.include_router(patent_ingest_p2.router)
    app.dependency_overrides[dependencies.get_db_session] = lambda: session

    test_client = TestClient(app)
    yield test_client
    app.dependency_overrides.clear()


def _post(client, payload, org_id=1, headers=None):
    headers = headers or {"Authorization": "Bearer dev-ingest-key"}
    app = client.app
    app.dependency_overrides[dependencies.get_current_organization] = lambda: dependencies.Organization(
        id=org_id, org_id=org_id
    )
    app.dependency_overrides[dependencies.get_current_active_user] = lambda: dependencies.ActiveUser(
        id=1, org_id=org_id
    )
    return client.post("/api/v1/patent-ingest/p2/graph-structure", json=payload, headers=headers)


def test_first_push_creates_all_nodes_and_edges(client, session):
    structure = _built_structure()
    payload = _payload_from_structure(structure)

    resp = _post(client, payload)

    assert resp.status_code == 200
    body = resp.json()
    assert body["nodes_created"] == len(structure["nodes"])
    assert body["edges_created"] == len(structure["edges"])
    assert body["nodes_updated"] == 0
    assert body["edges_updated"] == 0

    assert session.query(GovernanceGraphNode).filter_by(org_id=1).count() == len(structure["nodes"])
    assert session.query(GovernanceGraphEdge).filter_by(org_id=1).count() == len(structure["edges"])

    assert len(AuditService._written) == 1
    assert AuditService._written[0].event_type == "governance_graph.structure_ingest"


def test_repeat_push_of_identical_structure_does_not_duplicate_rows(client, session):
    """Run the satellite's build+serialize twice against the same fixture
    (as if the satellite polled again with nothing changed) and push both
    times -- the second push must be a no-op: same row counts, zero
    created/updated."""
    structure = _built_structure()
    payload = _payload_from_structure(structure)

    first = _post(client, payload)
    assert first.status_code == 200

    node_count_after_first = session.query(GovernanceGraphNode).filter_by(org_id=1).count()
    edge_count_after_first = session.query(GovernanceGraphEdge).filter_by(org_id=1).count()

    second = _post(client, payload)
    assert second.status_code == 200
    body = second.json()

    assert body["nodes_created"] == 0
    assert body["nodes_updated"] == 0
    assert body["edges_created"] == 0
    assert body["edges_updated"] == 0

    assert session.query(GovernanceGraphNode).filter_by(org_id=1).count() == node_count_after_first
    assert session.query(GovernanceGraphEdge).filter_by(org_id=1).count() == edge_count_after_first


def test_pushing_a_changed_edge_updates_in_place_without_duplicating(client, session):
    graph = graph_builder.build_graph(AI_SYSTEMS_EXPORT, REGULATIONS_CATALOG_EXPORT, JURISDICTIONS_EXPORT)
    structure = graph_builder.serialize_graph_structure(graph)
    _post(client, _payload_from_structure(structure))

    edge_count_before = session.query(GovernanceGraphEdge).filter_by(org_id=1).count()

    # Flip one edge inactive (simulating the satellite re-pulling an export
    # where that relationship no longer holds) and push again.
    from src.p2_satellite import schema

    nid = schema.node_id(schema.NODE_AI_SYSTEM, "sys-alpha")
    target = next(
        t for _, t, edge_type in graph.out_edges(nid, data="edge_type") if edge_type == schema.EDGE_SYSTEM_CLASSIFIED_AS
    )
    graph.edges[nid, target]["is_active"] = False
    changed_structure = graph_builder.serialize_graph_structure(graph)

    resp = _post(client, _payload_from_structure(changed_structure))

    assert resp.status_code == 200
    body = resp.json()
    assert body["edges_created"] == 0
    assert body["edges_updated"] == 1

    # Still the same number of edge ROWS -- updated in place, not duplicated.
    assert session.query(GovernanceGraphEdge).filter_by(org_id=1).count() == edge_count_before

    _, target_key = schema.split_node_id(target)
    updated_row = (
        session.query(GovernanceGraphNode).filter_by(org_id=1, node_key="sys-alpha", node_type="ai_system").one()
    )
    edge_row = (
        session.query(GovernanceGraphEdge)
        .filter_by(org_id=1, source_node_id=updated_row.id, edge_type="system_classified_as")
        .one()
    )
    assert edge_row.is_active is False


def test_graph_structure_ingest_is_org_scoped(client, session):
    structure = _built_structure()
    payload = _payload_from_structure(structure)

    _post(client, payload, org_id=1)
    _post(client, payload, org_id=2)

    # Each org gets its OWN full set of rows -- pushing the same structure
    # for a second org must never touch or dedupe against the first org's
    # rows (governance_graph_nodes' unique constraint is scoped by org_id).
    assert session.query(GovernanceGraphNode).filter_by(org_id=1).count() == len(structure["nodes"])
    assert session.query(GovernanceGraphNode).filter_by(org_id=2).count() == len(structure["nodes"])
    assert session.query(GovernanceGraphNode).count() == 2 * len(structure["nodes"])


def test_graph_structure_ingest_requires_ingest_scope(client):
    structure = _built_structure()
    payload = _payload_from_structure(structure)

    resp = _post(client, payload, headers={"Authorization": "Bearer dev-export-key"})

    assert resp.status_code == 403
