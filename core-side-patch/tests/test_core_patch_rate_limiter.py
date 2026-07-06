# Patch for CompliVibe core (app.complivibe.in), lands on top of migration head 0175.
# Test-harness only (not part of the patch set shipped to core).
#
# Unit tests for rate_limiter.FixedWindowRateLimiter/require_ingest_rate_limit
# in isolation, plus an end-to-end test hammering the real ingest FastAPI
# route past the configured limit to confirm a 429 actually surfaces there
# (test_core_patch_ingest_router.py covers the validation-contract behavior;
# this file covers the rate-limit behavior specifically).
from __future__ import annotations

import pytest
import sqlalchemy as sa
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from routers import patent_ingest_p2
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from tests.fixtures.expected_traversal import EXPECTED
from tests.fixtures.reference_cte import _build_node_edge_rows

import dependencies
import rate_limiter
from audit_service_stub import AuditService
from mismatch_metrics import MismatchMetrics
from models import (
    AiSystemObligationLink,
    Base,
    GovernanceGraphEdge,
    GovernanceGraphNode,
    GovernanceGraphTraversalResult,
)
from rate_limiter import DEFAULT_LIMIT, FixedWindowRateLimiter, require_ingest_rate_limit

# --------------------------------------------------------------------------
# Unit tests: FixedWindowRateLimiter in isolation
# --------------------------------------------------------------------------


def test_allows_up_to_the_configured_limit():
    limiter = FixedWindowRateLimiter(limit=3, window_seconds=60.0)
    assert limiter.allow("key-a") is True
    assert limiter.allow("key-a") is True
    assert limiter.allow("key-a") is True
    assert limiter.allow("key-a") is False


def test_limit_is_tracked_independently_per_key():
    limiter = FixedWindowRateLimiter(limit=1, window_seconds=60.0)
    assert limiter.allow("key-a") is True
    assert limiter.allow("key-a") is False
    # A different key has its own independent budget.
    assert limiter.allow("key-b") is True


def test_window_resets_after_elapsed_time(monkeypatch):
    limiter = FixedWindowRateLimiter(limit=1, window_seconds=10.0)
    fake_now = [1000.0]
    monkeypatch.setattr(rate_limiter.time, "monotonic", lambda: fake_now[0])

    assert limiter.allow("key-a") is True
    assert limiter.allow("key-a") is False

    fake_now[0] += 10.1  # past the window
    assert limiter.allow("key-a") is True


def test_require_ingest_rate_limit_raises_429_when_exceeded():
    limiter = FixedWindowRateLimiter(limit=1, window_seconds=60.0)
    rate_limiter._ingest_rate_limiter = limiter
    try:
        require_ingest_rate_limit("some-key")  # 1st call: fine
        with pytest.raises(HTTPException) as exc_info:
            require_ingest_rate_limit("some-key")  # 2nd call: rejected
        assert exc_info.value.status_code == 429
    finally:
        rate_limiter._ingest_rate_limiter = FixedWindowRateLimiter()


# --------------------------------------------------------------------------
# End-to-end: hammer the real ingest route past the limit
# --------------------------------------------------------------------------


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
def small_limit_client(session):
    """Route wired up with a tiny (limit=3) rate limiter so the test doesn't
    need to fire 100+ requests to observe a 429."""
    AuditService._reset_for_tests()
    MismatchMetrics._reset_for_tests()
    original_limiter = rate_limiter._ingest_rate_limiter
    rate_limiter._ingest_rate_limiter = FixedWindowRateLimiter(limit=3, window_seconds=60.0)

    app = FastAPI()
    app.include_router(patent_ingest_p2.router)
    app.dependency_overrides[dependencies.get_db_session] = lambda: session

    test_client = TestClient(app)
    yield test_client
    app.dependency_overrides.clear()
    rate_limiter._ingest_rate_limiter = original_limiter


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


def _post(client, payload=None):
    return client.post(
        "/api/v1/patent-ingest/p2/obligation-derivation",
        json=payload or _valid_payload(),
        headers={"Authorization": "Bearer dev-ingest-key"},
    )


def test_requests_under_the_limit_succeed_normally(small_limit_client):
    for _ in range(3):
        resp = _post(small_limit_client)
        assert resp.status_code == 200


def test_requests_past_the_limit_eventually_get_429(small_limit_client):
    statuses = [_post(small_limit_client).status_code for _ in range(5)]
    assert statuses[:3] == [200, 200, 200]
    assert 429 in statuses[3:]


def test_default_limit_constant_is_a_positive_stopgap_value():
    # Sanity check on the documented stopgap default -- not a claim that 100
    # is the "correct" tuned value (see rate_limiter.py docstring).
    assert DEFAULT_LIMIT > 0
