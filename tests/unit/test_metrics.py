"""
Unit tests for src/p2_satellite/metrics.py and the GET /metrics endpoint on
event_listener.py's FastAPI app.

Covers:
  - GET /metrics returns Prometheus text-exposition-format content.
  - Traversal count/duration, ingest push success/failure, and
    validation-mismatch counters actually increment when
    process_ai_system_changed runs (mocked fetch/derive/push, same
    convention as tests/unit/test_event_listener.py).
  - The replay-cache gauge reflects event_listener._seen_signatures' live
    size, not a stale snapshot.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from prometheus_client import CONTENT_TYPE_LATEST
from prometheus_client.parser import text_string_to_metric_families

from src.p2_satellite import event_listener, metrics

client = TestClient(event_listener.app)


def _metric_samples(body: str, name: str) -> list:
    families = list(text_string_to_metric_families(body))
    family = next((f for f in families if f.name == name), None)
    return list(family.samples) if family else []


def test_metrics_endpoint_returns_prometheus_text_format():
    resp = client.get("/metrics")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith(CONTENT_TYPE_LATEST.split(";")[0])
    # Every metric this module defines should have registered at least its
    # family name in the exposition output, even before any events fire.
    body = resp.text
    assert "p2_traversal_total" in body
    assert "p2_ingest_push_total" in body
    assert "p2_validation_mismatch_total" in body
    assert "p2_replay_cache_size" in body


def test_replay_cache_gauge_reflects_live_size(monkeypatch):
    monkeypatch.setattr(event_listener, "_seen_signatures", {("t", "a"): 1.0, ("t", "b"): 2.0})

    body = client.get("/metrics").text
    samples = _metric_samples(body, "p2_replay_cache_size")

    assert len(samples) == 1
    assert samples[0].value == 2.0


def test_process_ai_system_changed_increments_traversal_and_push_metrics(monkeypatch):
    def fake_fetch_and_build_graph(changed_since=None):
        import networkx as nx

        from src.p2_satellite import schema

        g = nx.DiGraph()
        g.add_node(
            schema.node_id(schema.NODE_AI_SYSTEM, "sys-metrics-test"),
            node_type=schema.NODE_AI_SYSTEM,
            node_key="sys-metrics-test",
        )
        return g

    def fake_derive_obligations(graph, node_id, max_traversal_depth=None):
        return {
            "ai_system_id": "sys-metrics-test",
            "derived_obligations": [],
            "derived_controls": [],
            "graph_path": [],
            "methodology_version": "test",
        }

    def fake_push_derivation(derivation, trigger_reason):
        return {"status": "accepted", "validation_status": "validated"}

    def fake_push_graph_structure(structure):
        return {"nodes_created": 0}

    monkeypatch.setattr(event_listener, "fetch_and_build_graph", fake_fetch_and_build_graph)
    monkeypatch.setattr(event_listener, "derive_obligations", fake_derive_obligations)
    monkeypatch.setattr(event_listener, "push_derivation", fake_push_derivation)
    monkeypatch.setattr(event_listener, "push_graph_structure", fake_push_graph_structure)

    before = metrics.TRAVERSAL_TOTAL.labels(trigger_reason="event")._value.get()
    before_validated = metrics.VALIDATION_MISMATCH_TOTAL.labels(validation_status="validated")._value.get()
    before_push_success = metrics.INGEST_PUSH_TOTAL.labels(push_kind="derivation", outcome="success")._value.get()

    event_listener.process_ai_system_changed("sys-metrics-test", "risk_tier")

    assert metrics.TRAVERSAL_TOTAL.labels(trigger_reason="event")._value.get() == before + 1
    assert metrics.VALIDATION_MISMATCH_TOTAL.labels(validation_status="validated")._value.get() == before_validated + 1
    assert (
        metrics.INGEST_PUSH_TOTAL.labels(push_kind="derivation", outcome="success")._value.get()
        == before_push_success + 1
    )


def test_process_ai_system_changed_records_push_failure(monkeypatch):
    def fake_fetch_and_build_graph(changed_since=None):
        import networkx as nx

        from src.p2_satellite import schema

        g = nx.DiGraph()
        g.add_node(
            schema.node_id(schema.NODE_AI_SYSTEM, "sys-metrics-fail"),
            node_type=schema.NODE_AI_SYSTEM,
            node_key="sys-metrics-fail",
        )
        return g

    def fake_derive_obligations(graph, node_id, max_traversal_depth=None):
        return {
            "ai_system_id": "sys-metrics-fail",
            "derived_obligations": [],
            "derived_controls": [],
            "graph_path": [],
            "methodology_version": "test",
        }

    def fake_push_derivation_raises(derivation, trigger_reason):
        raise RuntimeError("simulated push failure")

    def fake_push_graph_structure(structure):
        return {"nodes_created": 0}

    monkeypatch.setattr(event_listener, "fetch_and_build_graph", fake_fetch_and_build_graph)
    monkeypatch.setattr(event_listener, "derive_obligations", fake_derive_obligations)
    monkeypatch.setattr(event_listener, "push_derivation", fake_push_derivation_raises)
    monkeypatch.setattr(event_listener, "push_graph_structure", fake_push_graph_structure)

    before_failure = metrics.INGEST_PUSH_TOTAL.labels(push_kind="derivation", outcome="failure")._value.get()

    event_listener.process_ai_system_changed("sys-metrics-fail", "risk_tier")

    assert (
        metrics.INGEST_PUSH_TOTAL.labels(push_kind="derivation", outcome="failure")._value.get() == before_failure + 1
    )


def test_record_validation_status_ignores_missing_status():
    before = sum(s.value for s in metrics.VALIDATION_MISMATCH_TOTAL.collect()[0].samples)
    metrics.record_validation_status(None)
    after = sum(s.value for s in metrics.VALIDATION_MISMATCH_TOTAL.collect()[0].samples)
    assert before == after
