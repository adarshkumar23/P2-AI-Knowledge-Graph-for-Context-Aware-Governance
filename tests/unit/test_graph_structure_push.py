"""
Unit tests for graph_builder.serialize_graph_structure() and
ingest_client.push_graph_structure()/compute_structure_hash() -- the
satellite-side half of closing core-side-patch/ASSUMPTIONS.md item 22 (the
satellite is now the sole source of truth for governance_graph_nodes/edges,
pushing its whole built graph after every fetch).

Mirrors tests/unit/test_ingest_client.py's fake-httpx-client convention.
"""

from __future__ import annotations

import httpx
import pytest

from src.p2_satellite import graph_builder, ingest_client, schema
from src.p2_satellite.config import settings
from tests.fixtures.sample_export import (
    AI_SYSTEMS_EXPORT,
    JURISDICTIONS_EXPORT,
    REGULATIONS_CATALOG_EXPORT,
)


@pytest.fixture()
def graph():
    return graph_builder.build_graph(AI_SYSTEMS_EXPORT, REGULATIONS_CATALOG_EXPORT, JURISDICTIONS_EXPORT)


class _FakeClient:
    def __init__(self, handler):
        self._handler = handler

    def __call__(self, *args, **kwargs):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def post(self, url, json=None, headers=None):
        return self._handler(url, json, headers)


def _make_response(status_code: int, payload: dict) -> httpx.Response:
    request = httpx.Request("POST", "http://example.test")
    return httpx.Response(status_code, request=request, json=payload)


# --------------------------------------------------------------------------
# serialize_graph_structure
# --------------------------------------------------------------------------


def test_serialize_uses_natural_keys_not_internal_node_ids(graph):
    structure = graph_builder.serialize_graph_structure(graph)

    node_keys = {(n["node_type"], n["node_key"]) for n in structure["nodes"]}
    assert (schema.NODE_AI_SYSTEM, "sys-alpha") in node_keys
    assert (schema.NODE_REGULATION, "GDPR") in node_keys
    # No internal "node_type:node_key" strings anywhere in the payload.
    for node in structure["nodes"]:
        assert ":" not in node["node_key"] or node["node_type"] != "ai_system"


def test_serialize_edges_reference_endpoints_by_natural_key(graph):
    structure = graph_builder.serialize_graph_structure(graph)

    edge = next(
        e
        for e in structure["edges"]
        if e["edge_type"] == schema.EDGE_SYSTEM_CLASSIFIED_AS and e["source_node_key"] == "sys-alpha"
    )
    assert edge["source_node_type"] == schema.NODE_AI_SYSTEM
    assert edge["target_node_type"] == schema.NODE_RISK_TIER
    assert edge["target_node_key"] == "limited"
    assert edge["is_active"] is True


def test_serialize_is_deterministic_across_repeated_builds():
    graph1 = graph_builder.build_graph(AI_SYSTEMS_EXPORT, REGULATIONS_CATALOG_EXPORT, JURISDICTIONS_EXPORT)
    graph2 = graph_builder.build_graph(AI_SYSTEMS_EXPORT, REGULATIONS_CATALOG_EXPORT, JURISDICTIONS_EXPORT)

    structure1 = graph_builder.serialize_graph_structure(graph1)
    structure2 = graph_builder.serialize_graph_structure(graph2)

    assert structure1 == structure2
    assert ingest_client.compute_structure_hash(structure1) == ingest_client.compute_structure_hash(structure2)


def test_structure_hash_changes_when_an_edge_is_deactivated(graph):
    structure_before = graph_builder.serialize_graph_structure(graph)

    nid = schema.node_id(schema.NODE_AI_SYSTEM, "sys-alpha")
    target = next(t for _, t, _ in graph.out_edges(nid, data="edge_type"))
    graph.edges[nid, target]["is_active"] = False

    structure_after = graph_builder.serialize_graph_structure(graph)

    assert ingest_client.compute_structure_hash(structure_before) != ingest_client.compute_structure_hash(
        structure_after
    )


# --------------------------------------------------------------------------
# push_graph_structure -- URL / headers / idempotency
# --------------------------------------------------------------------------


def test_push_graph_structure_sends_correct_url_and_headers(monkeypatch, graph):
    captured = {}

    def handler(url, json_body, headers):
        captured["url"] = url
        captured["json"] = json_body
        captured["headers"] = headers
        return _make_response(200, {"nodes_created": 10, "edges_created": 10})

    monkeypatch.setattr(httpx, "Client", _FakeClient(handler))

    structure = graph_builder.serialize_graph_structure(graph)
    result = ingest_client.push_graph_structure(structure)

    assert captured["url"] == f"{settings.core_base_url}{ingest_client.GRAPH_STRUCTURE_PATH}"
    assert captured["headers"]["Authorization"] == f"Bearer {settings.core_ingest_api_key}"

    expected_hash = ingest_client.compute_structure_hash(structure)
    assert captured["headers"]["Idempotency-Key"] == expected_hash
    assert captured["json"]["structure_hash"] == expected_hash
    assert captured["json"]["nodes"] == structure["nodes"]
    assert captured["json"]["edges"] == structure["edges"]
    assert result == {"nodes_created": 10, "edges_created": 10}


def test_push_graph_structure_retries_on_transient_500(monkeypatch, graph):
    attempts = {"n": 0}

    def handler(url, json_body, headers):
        attempts["n"] += 1
        if attempts["n"] < 2:
            return _make_response(503, {"error": "unavailable"})
        return _make_response(200, {"nodes_created": 0, "edges_created": 0})

    monkeypatch.setattr(httpx, "Client", _FakeClient(handler))

    structure = graph_builder.serialize_graph_structure(graph)
    result = ingest_client.push_graph_structure(structure)

    assert attempts["n"] == 2
    assert result == {"nodes_created": 0, "edges_created": 0}


def test_push_graph_structure_does_not_retry_on_422(monkeypatch, graph):
    attempts = {"n": 0}

    def handler(url, json_body, headers):
        attempts["n"] += 1
        return _make_response(422, {"error": "bad structure"})

    monkeypatch.setattr(httpx, "Client", _FakeClient(handler))

    structure = graph_builder.serialize_graph_structure(graph)
    with pytest.raises(ingest_client.PermanentIngestError):
        ingest_client.push_graph_structure(structure)

    assert attempts["n"] == 1
