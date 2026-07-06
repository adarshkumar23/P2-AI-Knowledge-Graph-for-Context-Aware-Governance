# Patch for CompliVibe core (app.complivibe.in), lands on top of migration head 0175.
# Test-harness only (not part of the patch set shipped to core).
#
# FastAPI TestClient tests for routers/patent_ingest_p2.py covering the full
# "Satellites Compute, Core Decides" validation contract: auth scoping,
# unknown-id rejection (422), the mismatch-flagging path (validation_status=
# "flagged_mismatch", no ai_system_obligation_links write), and the happy path
# (validation_status="validated", links written, audit log fired).
from __future__ import annotations

import logging

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
from mismatch_metrics import MismatchMetrics
from models import (
    AiSystemObligationLink,
    Base,
    GovernanceGraphEdge,
    GovernanceGraphNode,
    GovernanceGraphTraversalResult,
)


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
    MismatchMetrics._reset_for_tests()
    import rate_limiter as _rate_limiter_module

    _rate_limiter_module._reset_rate_limiter_for_tests()

    app = FastAPI()
    app.include_router(patent_ingest_p2.router)
    app.dependency_overrides[dependencies.get_db_session] = lambda: session

    test_client = TestClient(app)
    yield test_client
    app.dependency_overrides.clear()


def _valid_payload(ai_system_key: str = "sys-alpha") -> dict:
    expected = EXPECTED[ai_system_key]
    return {
        "ai_system_id": ai_system_key,
        "derived_obligations": list(expected["derived_obligations"]),
        "derived_controls": list(expected["derived_controls"]),
        "graph_path": [],
        "methodology_version": "p2-v1.0.0",
        "trigger_reason": "event",
        "derivation_hash": "test-hash-1",
    }


def _post(client, payload, headers=None):
    headers = headers or {"Authorization": "Bearer dev-ingest-key"}
    return client.post("/api/v1/patent-ingest/p2/obligation-derivation", json=payload, headers=headers)


def test_ingest_requires_authorization_header(client):
    resp = client.post("/api/v1/patent-ingest/p2/obligation-derivation", json=_valid_payload())
    assert resp.status_code == 401


def test_ingest_rejects_wrong_scope(client):
    resp = _post(client, _valid_payload(), headers={"Authorization": "Bearer dev-export-key"})
    assert resp.status_code == 403


def test_ingest_rejects_unknown_obligation_id(client):
    payload = _valid_payload()
    payload["derived_obligations"].append("totally_made_up_obligation")
    resp = _post(client, payload)
    assert resp.status_code == 422
    assert "totally_made_up_obligation" in resp.json()["detail"]["ids"]


def test_ingest_rejects_unknown_control_id(client):
    payload = _valid_payload()
    payload["derived_controls"].append("totally_made_up_control")
    resp = _post(client, payload)
    assert resp.status_code == 422
    assert "totally_made_up_control" in resp.json()["detail"]["ids"]


def test_ingest_flags_mismatch_and_does_not_write_links(client, session):
    payload = _valid_payload("sys-beta")
    # Drop one legitimately-known obligation so it no longer matches core's
    # independent re-derivation -- this must NOT 422 (the id is real/known),
    # it must be flagged for human review instead.
    payload["derived_obligations"] = [o for o in payload["derived_obligations"] if o != "dpdp_consent_notice"]

    resp = _post(client, payload)

    assert resp.status_code == 200
    body = resp.json()
    assert body["validation_status"] == "flagged_mismatch"
    assert "dpdp_consent_notice" in body["reference_derived_obligations"]

    links = session.query(AiSystemObligationLink).filter_by(ai_system_id="sys-beta").all()
    assert links == []

    traversal_rows = session.query(GovernanceGraphTraversalResult).filter_by(ai_system_id="sys-beta").all()
    assert len(traversal_rows) == 1
    assert traversal_rows[0].validation_status == "flagged_mismatch"

    assert len(AuditService._written) == 1
    audit_entry = AuditService._written[0]
    assert audit_entry.payload["validation_status"] == "flagged_mismatch"
    assert audit_entry.payload["methodology_version"] == "p2-v1.0.0"
    assert audit_entry.payload["trigger_reason"] == "event"


def test_ingest_validates_and_writes_links_on_exact_match(client, session):
    payload = _valid_payload("sys-alpha")

    resp = _post(client, payload)

    assert resp.status_code == 200
    body = resp.json()
    assert body["validation_status"] == "validated"

    obligation_links = {
        row.obligation_id
        for row in session.query(AiSystemObligationLink).filter_by(ai_system_id="sys-alpha")
        if row.obligation_id is not None
    }
    assert obligation_links == set(payload["derived_obligations"])

    control_links = {
        row.control_type_id
        for row in session.query(AiSystemObligationLink).filter_by(ai_system_id="sys-alpha")
        if row.control_type_id is not None
    }
    assert control_links == set(payload["derived_controls"])

    traversal_rows = session.query(GovernanceGraphTraversalResult).filter_by(ai_system_id="sys-alpha").all()
    assert len(traversal_rows) == 1
    assert traversal_rows[0].validation_status == "validated"

    assert len(AuditService._written) == 1
    assert AuditService._written[0].payload["validation_status"] == "validated"


def test_ingest_rejects_unknown_ai_system(client):
    payload = _valid_payload()
    payload["ai_system_id"] = "sys-does-not-exist"
    resp = _post(client, payload)
    assert resp.status_code == 422


# --------------------------------------------------------------------------
# Task 1: mismatch-visibility -- a WARNING-level log line on mismatch, and
# MismatchMetrics recording every ingest outcome (validated AND flagged) so
# the rate's denominator is correct.
# --------------------------------------------------------------------------


def test_flagged_mismatch_emits_a_warning_log_with_org_and_system_ids(client, caplog):
    payload = _valid_payload("sys-beta")
    payload["derived_obligations"] = [o for o in payload["derived_obligations"] if o != "dpdp_consent_notice"]

    with caplog.at_level(logging.WARNING, logger="core_side_patch.patent_ingest_p2"):
        resp = _post(client, payload)

    assert resp.status_code == 200
    assert resp.json()["validation_status"] == "flagged_mismatch"

    mismatch_records = [
        r for r in caplog.records if r.name == "core_side_patch.patent_ingest_p2" and r.levelno >= logging.WARNING
    ]
    assert len(mismatch_records) == 1
    record = mismatch_records[0]
    assert record.message == "governance_graph.obligation_derivation_mismatch" or "mismatch" in record.getMessage()
    assert record.org_id == 1
    assert record.ai_system_id == "sys-beta"
    assert record.methodology_version == "p2-v1.0.0"
    assert record.trigger_reason == "event"


def test_flagged_mismatch_is_recorded_in_mismatch_metrics(client):
    payload = _valid_payload("sys-beta")
    payload["derived_obligations"] = [o for o in payload["derived_obligations"] if o != "dpdp_consent_notice"]

    _post(client, payload)

    assert MismatchMetrics.total_recorded() == 1
    assert MismatchMetrics.mismatch_rate() == 1.0


def test_validated_ingest_does_not_emit_warning_log_but_is_still_recorded(client, caplog):
    payload = _valid_payload("sys-alpha")

    with caplog.at_level(logging.WARNING, logger="core_side_patch.patent_ingest_p2"):
        resp = _post(client, payload)

    assert resp.status_code == 200
    assert resp.json()["validation_status"] == "validated"

    mismatch_records = [
        r for r in caplog.records if r.name == "core_side_patch.patent_ingest_p2" and r.levelno >= logging.WARNING
    ]
    assert mismatch_records == []

    # Still counted toward the denominator, with zero mismatches -> rate 0.0.
    assert MismatchMetrics.total_recorded() == 1
    assert MismatchMetrics.mismatch_rate() == 0.0
