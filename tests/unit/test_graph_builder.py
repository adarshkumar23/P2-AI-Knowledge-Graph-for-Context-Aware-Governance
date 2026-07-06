"""
Unit tests for src/p2_satellite/graph_builder.py (Workstream B).

Covers:
  - build_graph() against tests/fixtures/sample_export.py's three exports
    directly: specific expected nodes/edges, and sane total counts.
  - fetch_and_build_graph() / the individual fetch_* functions, with httpx
    calls monkeypatched (no real network calls).
  - tenacity retry behavior: retries on transient errors (ConnectError,
    TimeoutException, 5xx) and does NOT retry on 4xx.
"""

from __future__ import annotations

import httpx
import pytest

from src.p2_satellite import graph_builder, schema
from tests.fixtures.sample_export import (
    AI_SYSTEMS_EXPORT,
    JURISDICTIONS_EXPORT,
    REGULATIONS_CATALOG_EXPORT,
)


@pytest.fixture()
def graph():
    return graph_builder.build_graph(AI_SYSTEMS_EXPORT, REGULATIONS_CATALOG_EXPORT, JURISDICTIONS_EXPORT)


# --------------------------------------------------------------------------
# build_graph() — node/edge assertions
# --------------------------------------------------------------------------


def test_expected_ai_system_nodes_exist(graph):
    assert schema.node_id(schema.NODE_AI_SYSTEM, "sys-alpha") in graph.nodes
    assert schema.node_id(schema.NODE_AI_SYSTEM, "sys-beta") in graph.nodes


def test_ai_system_node_attrs(graph):
    nid = schema.node_id(schema.NODE_AI_SYSTEM, "sys-beta")
    attrs = graph.nodes[nid]
    assert attrs["node_type"] == schema.NODE_AI_SYSTEM
    assert attrs["node_key"] == "sys-beta"


def test_expected_regulation_and_obligation_nodes_exist(graph):
    for key in ("GDPR", "EU_AI_ACT", "DPDP"):
        assert schema.node_id(schema.NODE_REGULATION, key) in graph.nodes
    for key in (
        "gdpr_data_subject_rights",
        "gdpr_breach_notification",
        "euaiact_transparency_notice",
        "euaiact_conformity_assessment",
        "euaiact_human_oversight",
        "dpdp_consent_notice",
    ):
        assert schema.node_id(schema.NODE_OBLIGATION, key) in graph.nodes


def test_expected_control_and_jurisdiction_and_risk_tier_nodes_exist(graph):
    for key in (
        "access_control",
        "audit_logging",
        "transparency_documentation",
        "consent_management",
    ):
        assert schema.node_id(schema.NODE_CONTROL_TYPE, key) in graph.nodes
    assert schema.node_id(schema.NODE_JURISDICTION, "EU") in graph.nodes
    assert schema.node_id(schema.NODE_JURISDICTION, "IN") in graph.nodes
    assert schema.node_id(schema.NODE_RISK_TIER, "high") in graph.nodes
    assert schema.node_id(schema.NODE_RISK_TIER, "limited") in graph.nodes


def test_system_uses_edge(graph):
    sid = schema.node_id(schema.NODE_AI_SYSTEM, "sys-beta")
    dcid = schema.node_id(schema.NODE_DATA_CATEGORY, "biometric")
    assert graph.has_edge(sid, dcid)
    assert graph.edges[sid, dcid]["edge_type"] == schema.EDGE_SYSTEM_USES
    assert graph.edges[sid, dcid]["is_active"] is True


def test_system_deploys_in_edge(graph):
    sid = schema.node_id(schema.NODE_AI_SYSTEM, "sys-beta")
    jid = schema.node_id(schema.NODE_JURISDICTION, "IN")
    assert graph.has_edge(sid, jid)
    assert graph.edges[sid, jid]["edge_type"] == schema.EDGE_SYSTEM_DEPLOYS_IN


def test_system_classified_as_edge(graph):
    sid = schema.node_id(schema.NODE_AI_SYSTEM, "sys-alpha")
    tid = schema.node_id(schema.NODE_RISK_TIER, "limited")
    assert graph.has_edge(sid, tid)
    assert graph.edges[sid, tid]["edge_type"] == schema.EDGE_SYSTEM_CLASSIFIED_AS


def test_data_triggers_edge(graph):
    dcid = schema.node_id(schema.NODE_DATA_CATEGORY, "biometric")
    rid = schema.node_id(schema.NODE_REGULATION, "EU_AI_ACT")
    assert graph.has_edge(dcid, rid)
    assert graph.edges[dcid, rid]["edge_type"] == schema.EDGE_DATA_TRIGGERS


def test_jurisdiction_has_edge(graph):
    jid = schema.node_id(schema.NODE_JURISDICTION, "EU")
    rid = schema.node_id(schema.NODE_REGULATION, "GDPR")
    assert graph.has_edge(jid, rid)
    assert graph.edges[jid, rid]["edge_type"] == schema.EDGE_JURISDICTION_HAS


def test_regulation_requires_edge(graph):
    rid = schema.node_id(schema.NODE_REGULATION, "GDPR")
    oid = schema.node_id(schema.NODE_OBLIGATION, "gdpr_data_subject_rights")
    assert graph.has_edge(rid, oid)
    assert graph.edges[rid, oid]["edge_type"] == schema.EDGE_REGULATION_REQUIRES


def test_obligation_needs_edge(graph):
    oid = schema.node_id(schema.NODE_OBLIGATION, "gdpr_data_subject_rights")
    cid = schema.node_id(schema.NODE_CONTROL_TYPE, "access_control")
    assert graph.has_edge(oid, cid)
    assert graph.edges[oid, cid]["edge_type"] == schema.EDGE_OBLIGATION_NEEDS


def test_risk_tier_adds_edge(graph):
    tid = schema.node_id(schema.NODE_RISK_TIER, "high")
    oid = schema.node_id(schema.NODE_OBLIGATION, "euaiact_conformity_assessment")
    assert graph.has_edge(tid, oid)
    assert graph.edges[tid, oid]["edge_type"] == schema.EDGE_RISK_TIER_ADDS


def test_node_and_edge_counts_are_sane(graph):
    # Sanity bounds -- not brittle exact counts, but catch gross regressions
    # (e.g. duplicated nodes, missing edge categories).
    assert graph.number_of_nodes() >= 15
    assert graph.number_of_edges() >= 20
    assert graph.is_directed()


def test_all_edges_have_valid_edge_type(graph):
    for _, _, data in graph.edges(data=True):
        schema.validate_edge_type(data["edge_type"])
        assert data["is_active"] is True


# --------------------------------------------------------------------------
# fetch_* / fetch_and_build_graph — network mocked via monkeypatch
# --------------------------------------------------------------------------


def test_fetch_and_build_graph_uses_fetchers(monkeypatch):
    calls = {}

    def fake_fetch_ai_systems(changed_since=None):
        calls["ai_systems"] = changed_since
        return AI_SYSTEMS_EXPORT

    def fake_fetch_regulations_catalog(changed_since=None):
        calls["regulations_catalog"] = changed_since
        return REGULATIONS_CATALOG_EXPORT

    def fake_fetch_jurisdictions(changed_since=None):
        calls["jurisdictions"] = changed_since
        return JURISDICTIONS_EXPORT

    monkeypatch.setattr(graph_builder, "fetch_ai_systems", fake_fetch_ai_systems)
    monkeypatch.setattr(graph_builder, "fetch_regulations_catalog", fake_fetch_regulations_catalog)
    monkeypatch.setattr(graph_builder, "fetch_jurisdictions", fake_fetch_jurisdictions)

    g = graph_builder.fetch_and_build_graph(changed_since="2026-01-01T00:00:00Z")

    assert calls == {
        "ai_systems": "2026-01-01T00:00:00Z",
        "regulations_catalog": "2026-01-01T00:00:00Z",
        "jurisdictions": "2026-01-01T00:00:00Z",
    }
    assert schema.node_id(schema.NODE_AI_SYSTEM, "sys-beta") in g.nodes


def test_get_json_sends_auth_header_and_changed_since(monkeypatch):
    captured = {}

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return AI_SYSTEMS_EXPORT

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, headers=None, params=None):
            captured["url"] = url
            captured["headers"] = headers
            captured["params"] = params
            return FakeResponse()

    monkeypatch.setattr(httpx, "Client", FakeClient)

    result = graph_builder.fetch_ai_systems(changed_since="2026-05-01T00:00:00Z")

    assert result == AI_SYSTEMS_EXPORT
    assert captured["url"].endswith(graph_builder.AI_SYSTEMS_PATH)
    assert captured["headers"]["Authorization"].startswith("Bearer ")
    assert captured["params"] == {"changed_since": "2026-05-01T00:00:00Z"}


def test_retries_on_transient_connect_error_then_succeeds(monkeypatch):
    attempts = {"n": 0}

    class FlakyThenOkClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, headers=None, params=None):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise httpx.ConnectError("boom", request=httpx.Request("GET", url))
            return _make_ok_response(JURISDICTIONS_EXPORT)

    monkeypatch.setattr(httpx, "Client", FlakyThenOkClient)

    result = graph_builder.fetch_jurisdictions()

    assert result == JURISDICTIONS_EXPORT
    assert attempts["n"] == 3


def test_does_not_retry_on_4xx(monkeypatch):
    attempts = {"n": 0}

    class AlwaysBadRequestClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, headers=None, params=None):
            attempts["n"] += 1
            request = httpx.Request("GET", url)
            response = httpx.Response(400, request=request, json={"error": "bad request"})
            return response

    monkeypatch.setattr(httpx, "Client", AlwaysBadRequestClient)

    with pytest.raises(httpx.HTTPStatusError):
        graph_builder.fetch_ai_systems()

    assert attempts["n"] == 1


def test_retries_exhausted_on_persistent_5xx(monkeypatch):
    attempts = {"n": 0}

    class AlwaysServerErrorClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, headers=None, params=None):
            attempts["n"] += 1
            request = httpx.Request("GET", url)
            response = httpx.Response(503, request=request, json={"error": "unavailable"})
            return response

    monkeypatch.setattr(httpx, "Client", AlwaysServerErrorClient)

    with pytest.raises(httpx.HTTPStatusError):
        graph_builder.fetch_regulations_catalog()

    assert attempts["n"] == 3


def _make_ok_response(payload):
    request = httpx.Request("GET", "http://example.test")
    return httpx.Response(200, request=request, json=payload)
