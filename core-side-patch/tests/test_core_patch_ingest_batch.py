# Patch for CompliVibe core (app.complivibe.in), lands on top of migration head 0175.
# Test-harness only (not part of the patch set shipped to core).
#
# Tests for the batch ingest route (routers/patent_ingest_p2.py ::
# post_obligation_derivations_batch), added in the production-hardening pass
# to avoid one-HTTP-round-trip-per-ai_system when the satellite's safety-net
# poll sweeps a large inventory. Confirms: same validation contract as the
# single-item route (mirrors test_core_patch_ingest_router.py's fixtures),
# one bad item doesn't fail the rest of the batch, and rate limiting charges
# the whole batch size in one shot rather than a flat 1 unit per HTTP call.
from __future__ import annotations

import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from routers import patent_ingest_p2
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from tests.fixtures.expected_traversal import EXPECTED
from tests.fixtures.reference_cte import _build_node_edge_rows

import dependencies
from audit_service_stub import AuditService
from models import (
    AiSystemObligationLink,
    Base,
    GovernanceGraphEdge,
    GovernanceGraphNode,
    GovernanceGraphTraversalResult,
)
from rate_limiter import _reset_rate_limiter_for_tests


def _build_populated_session() -> Session:
    engine = sa.create_engine("sqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(
        engine,
        tables=[
            GovernanceGraphNode.__table__,
            GovernanceGraphEdge.__table__,
            GovernanceGraphTraversalResult.__table__,
            AiSystemObligationLink.__table__,
        ],
    )
    session = Session(engine)

    nodes, edges = _build_node_edge_rows()
    string_id_to_pk: dict[str, int] = {}
    for string_id, node_type, node_key in nodes:
        row = GovernanceGraphNode(org_id=1, node_type=node_type, node_key=node_key, properties={})
        session.add(row)
        session.flush()
        string_id_to_pk[string_id] = row.id

    for source_string_id, target_string_id, edge_type, is_active in edges:
        session.add(
            GovernanceGraphEdge(
                org_id=1,
                source_node_id=string_id_to_pk[source_string_id],
                target_node_id=string_id_to_pk[target_string_id],
                edge_type=edge_type,
                is_active=bool(is_active),
            )
        )
    session.commit()
    return session


@pytest.fixture()
def session():
    s = _build_populated_session()
    yield s
    s.close()


@pytest.fixture()
def client(session):
    AuditService._reset_for_tests()
    _reset_rate_limiter_for_tests()

    app = FastAPI()
    app.include_router(patent_ingest_p2.router)
    app.dependency_overrides[dependencies.get_db_session] = lambda: session

    test_client = TestClient(app)
    yield test_client
    app.dependency_overrides.clear()


def _item(ai_system_key: str) -> dict:
    expected = EXPECTED[ai_system_key]
    return {
        "ai_system_id": ai_system_key,
        "derived_obligations": list(expected["derived_obligations"]),
        "derived_controls": list(expected["derived_controls"]),
        "graph_path": [],
        "methodology_version": "p2-v1.0.0",
        "trigger_reason": "scheduled",
        "derivation_hash": f"test-hash-{ai_system_key}",
    }


def _post_batch(client, derivations, headers=None):
    headers = headers or {"Authorization": "Bearer dev-ingest-key"}
    return client.post(
        "/api/v1/patent-ingest/p2/obligation-derivations/batch",
        json={"derivations": derivations},
        headers=headers,
    )


def test_batch_requires_authorization_and_scope(client):
    resp = client.post(
        "/api/v1/patent-ingest/p2/obligation-derivations/batch",
        json={"derivations": [_item("sys-alpha")]},
    )
    assert resp.status_code == 401

    resp = _post_batch(client, [_item("sys-alpha")], headers={"Authorization": "Bearer dev-export-key"})
    assert resp.status_code == 403


def test_batch_processes_every_item_through_same_contract(client, session):
    resp = _post_batch(client, [_item("sys-alpha"), _item("sys-beta")])
    assert resp.status_code == 200

    body = resp.json()
    assert len(body["results"]) == 2
    by_id = {r["ai_system_id"]: r for r in body["results"]}
    assert by_id["sys-alpha"]["ok"] is True
    assert by_id["sys-alpha"]["result"]["validation_status"] == "validated"
    assert by_id["sys-beta"]["ok"] is True
    assert by_id["sys-beta"]["result"]["validation_status"] == "validated"

    # Both items actually persisted -- same effect as two single-item calls.
    assert session.query(GovernanceGraphTraversalResult).count() == 2
    assert len(AuditService._written) == 2


def test_batch_one_bad_item_does_not_fail_the_rest(client, session):
    bad_item = _item("sys-alpha")
    bad_item["derived_obligations"].append("totally_made_up_obligation")

    resp = _post_batch(client, [bad_item, _item("sys-beta")])
    assert resp.status_code == 200

    body = resp.json()
    by_id = {r["ai_system_id"]: r for r in body["results"]}
    assert by_id["sys-alpha"]["ok"] is False
    assert by_id["sys-alpha"]["error"]["status_code"] == 422
    assert by_id["sys-beta"]["ok"] is True
    assert by_id["sys-beta"]["result"]["validation_status"] == "validated"

    # Only the good item persisted -- the bad one's rollback didn't touch it.
    assert session.query(GovernanceGraphTraversalResult).count() == 1
    assert session.query(GovernanceGraphTraversalResult).one().ai_system_id == "sys-beta"


def test_batch_rate_limit_charges_full_batch_size_at_once(client):
    # Limit is 100/60s by default; a single batch of 101 items must exceed it
    # in one shot, proving the batch route charges len(derivations) atomically
    # rather than the flat 1-unit-per-HTTP-call the single-item route uses.
    huge_batch = [_item("sys-alpha") for _ in range(101)]
    resp = _post_batch(client, huge_batch)
    assert resp.status_code == 429
