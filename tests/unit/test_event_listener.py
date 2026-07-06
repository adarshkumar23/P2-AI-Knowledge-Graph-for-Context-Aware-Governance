"""
Unit tests for src/p2_satellite/event_listener.py (Workstream D).

Covers:
  - verify_signature(): the timestamped HMAC check (t=<epoch>,v1=sha256=<hex>),
    including freshness (clock skew) and replay rejection.
  - POST /events/ai-system-changed with a valid, fresh X-P2-Signature -> 202,
    and the (mocked) graph_builder / traversal / ingest_client calls fire with
    trigger_reason="event".
  - POST /events/ai-system-changed with a missing/invalid/stale/replayed
    signature -> 401, and none of the downstream derivation/push calls happen.
  - Optional IP allowlist (settings.event_listener_ip_allowlist): disabled by
    default (unaffected), 403 when configured and the caller's IP isn't on it.

WIRE FORMAT (production-hardening pass): the signature header changed from
`sha256=<hex over body alone>` (no freshness/replay component -- a captured
valid payload could be replayed forever) to a timestamped scheme:
    X-P2-Signature: t=<unix_epoch_seconds>,v1=sha256=<hex>
where <hex> = HMAC-SHA256(secret, f"{t}.".encode() + raw_body). Nothing in
production calls this webhook yet, so this is a deliberate breaking change;
these tests build the new header format instead of the old one.

graph_builder.fetch_and_build_graph, traversal.derive_obligations, and
ingest_client.push_derivation are all mocked (monkeypatch) -- these tests
never touch the network or a real graph.

NOTE: TestClient is intentionally used WITHOUT the `with` context-manager
form here, so FastAPI's startup/shutdown lifespan events (which would start
the real APScheduler safety-net poll via scheduler.start_scheduler()) never
fire during these request-focused tests. The scheduler itself is covered
separately in test_scheduler.py.
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient

from src.p2_satellite import event_listener
from src.p2_satellite.config import settings

client = TestClient(event_listener.app)

EVENT_PATH = "/events/ai-system-changed"


@pytest.fixture(autouse=True)
def _mock_push_graph_structure(monkeypatch):
    """process_ai_system_changed also pushes graph structure now (item 22 --
    see src/p2_satellite/event_listener.py and
    core-side-patch/ASSUMPTIONS.md item 22). Mocked out here, autouse, so
    every test in this file stays free of real (and here, always-failing)
    network calls and the tenacity retry delay that comes with them.
    Dedicated coverage for push_graph_structure itself lives in
    tests/unit/test_graph_structure_push.py."""
    monkeypatch.setattr(
        event_listener, "push_graph_structure", lambda structure: {"nodes_created": 0, "edges_created": 0}
    )


def _sign(raw_body: bytes, t: int | None = None) -> str:
    if t is None:
        t = int(time.time())
    digest = hmac.new(
        settings.event_listener_shared_secret.encode("utf-8"),
        f"{t}.".encode() + raw_body,
        hashlib.sha256,
    ).hexdigest()
    return f"t={t},v1=sha256={digest}"


def _reset_replay_cache():
    with event_listener._seen_signatures_lock:
        event_listener._seen_signatures.clear()


# --------------------------------------------------------------------------
# verify_signature() in isolation
# --------------------------------------------------------------------------


def test_verify_signature_accepts_correct_hmac():
    _reset_replay_cache()
    body = b'{"ai_system_id": "sys-beta"}'
    header = _sign(body)
    assert event_listener.verify_signature(body, header) is True


def test_verify_signature_rejects_missing_header():
    assert event_listener.verify_signature(b'{"ai_system_id": "sys-beta"}', None) is False


def test_verify_signature_rejects_wrong_secret_signature():
    _reset_replay_cache()
    body = b'{"ai_system_id": "sys-beta"}'
    t = int(time.time())
    bad_digest = hmac.new(b"wrong-secret", f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    bad_header = f"t={t},v1=sha256={bad_digest}"
    assert event_listener.verify_signature(body, bad_header) is False


def test_verify_signature_rejects_missing_prefix():
    _reset_replay_cache()
    body = b'{"ai_system_id": "sys-beta"}'
    t = int(time.time())
    digest = hmac.new(
        settings.event_listener_shared_secret.encode("utf-8"), f"{t}.".encode() + body, hashlib.sha256
    ).hexdigest()
    # Missing the "v1=sha256=" structure entirely.
    assert event_listener.verify_signature(body, f"t={t},{digest}") is False


def test_verify_signature_rejects_tampered_body():
    _reset_replay_cache()
    body = b'{"ai_system_id": "sys-beta"}'
    header = _sign(body)
    tampered_body = b'{"ai_system_id": "sys-evil"}'
    assert event_listener.verify_signature(tampered_body, header) is False


def test_verify_signature_rejects_malformed_header_missing_t():
    _reset_replay_cache()
    body = b'{"ai_system_id": "sys-beta"}'
    digest = hmac.new(settings.event_listener_shared_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    assert event_listener.verify_signature(body, f"v1=sha256={digest}") is False


def test_verify_signature_rejects_stale_timestamp():
    _reset_replay_cache()
    body = b'{"ai_system_id": "sys-beta"}'
    stale_t = int(time.time()) - int(settings.event_webhook_max_clock_skew_seconds) - 60
    header = _sign(body, t=stale_t)
    assert event_listener.verify_signature(body, header) is False


def test_verify_signature_rejects_replay_of_exact_same_pair():
    _reset_replay_cache()
    body = b'{"ai_system_id": "sys-beta"}'
    header = _sign(body)
    # First use is accepted...
    assert event_listener.verify_signature(body, header) is True
    # ...exact same (t, signature) replayed again is rejected, even though
    # it's cryptographically valid and still within the freshness window.
    assert event_listener.verify_signature(body, header) is False


# --------------------------------------------------------------------------
# POST /events/ai-system-changed -- valid signature
# --------------------------------------------------------------------------


def test_valid_signature_returns_202_and_triggers_event_derivation(monkeypatch):
    _reset_replay_cache()
    calls = {}

    def fake_fetch_and_build_graph(changed_since=None):
        calls["fetch_changed_since"] = changed_since
        return "FAKE_GRAPH"

    def fake_derive_obligations(graph, node_id, max_traversal_depth=None):
        calls["derive_graph"] = graph
        calls["derive_node_id"] = node_id
        return {
            "ai_system_id": "sys-beta",
            "derived_obligations": ["gdpr_data_subject_rights"],
            "derived_controls": ["access_control"],
            "graph_path": [],
            "methodology_version": "p2-v1.0.0",
        }

    def fake_push_derivation(derivation, trigger_reason):
        calls["push_derivation"] = derivation
        calls["trigger_reason"] = trigger_reason
        return {"status": "ok"}

    monkeypatch.setattr(event_listener, "fetch_and_build_graph", fake_fetch_and_build_graph)
    monkeypatch.setattr(event_listener, "derive_obligations", fake_derive_obligations)
    monkeypatch.setattr(event_listener, "push_derivation", fake_push_derivation)

    body_dict = {"ai_system_id": "sys-beta", "changed_field": "deployment_jurisdiction"}
    raw_body = json.dumps(body_dict).encode("utf-8")
    header = _sign(raw_body)

    response = client.post(
        EVENT_PATH,
        content=raw_body,
        headers={"X-P2-Signature": header, "Content-Type": "application/json"},
    )

    assert response.status_code == 202
    assert response.json()["ai_system_id"] == "sys-beta"

    assert calls["derive_node_id"] == "ai_system:sys-beta"
    assert calls["trigger_reason"] == "event"
    assert calls["push_derivation"]["ai_system_id"] == "sys-beta"


def test_replayed_valid_request_is_rejected_on_second_delivery(monkeypatch):
    """The exact same request (same body, same signature header) delivered
    twice must be accepted the first time and rejected the second time, even
    though the signature is still cryptographically valid and fresh."""
    _reset_replay_cache()

    def fake_fetch_and_build_graph(changed_since=None):
        return "FAKE_GRAPH"

    def fake_derive_obligations(graph, node_id, max_traversal_depth=None):
        return {
            "ai_system_id": "sys-beta",
            "derived_obligations": [],
            "derived_controls": [],
            "graph_path": [],
            "methodology_version": "p2-v1.0.0",
        }

    def fake_push_derivation(derivation, trigger_reason):
        return {"status": "ok"}

    monkeypatch.setattr(event_listener, "fetch_and_build_graph", fake_fetch_and_build_graph)
    monkeypatch.setattr(event_listener, "derive_obligations", fake_derive_obligations)
    monkeypatch.setattr(event_listener, "push_derivation", fake_push_derivation)

    body_dict = {"ai_system_id": "sys-beta"}
    raw_body = json.dumps(body_dict).encode("utf-8")
    header = _sign(raw_body)
    headers = {"X-P2-Signature": header, "Content-Type": "application/json"}

    first = client.post(EVENT_PATH, content=raw_body, headers=headers)
    assert first.status_code == 202

    second = client.post(EVENT_PATH, content=raw_body, headers=headers)
    assert second.status_code == 401


def test_stale_signature_returns_401(monkeypatch):
    _reset_replay_cache()
    body_dict = {"ai_system_id": "sys-beta"}
    raw_body = json.dumps(body_dict).encode("utf-8")
    stale_t = int(time.time()) - int(settings.event_webhook_max_clock_skew_seconds) - 60
    header = _sign(raw_body, t=stale_t)

    response = client.post(
        EVENT_PATH,
        content=raw_body,
        headers={"X-P2-Signature": header, "Content-Type": "application/json"},
    )
    assert response.status_code == 401


# --------------------------------------------------------------------------
# POST /events/ai-system-changed -- invalid / missing signature
# --------------------------------------------------------------------------


def test_missing_signature_returns_401_and_no_downstream_calls(monkeypatch):
    _reset_replay_cache()
    calls = {"fetch": False, "derive": False, "push": False}

    def fake_fetch_and_build_graph(changed_since=None):
        calls["fetch"] = True
        return "FAKE_GRAPH"

    def fake_derive_obligations(graph, node_id, max_traversal_depth=None):
        calls["derive"] = True
        return {}

    def fake_push_derivation(derivation, trigger_reason):
        calls["push"] = True
        return {}

    monkeypatch.setattr(event_listener, "fetch_and_build_graph", fake_fetch_and_build_graph)
    monkeypatch.setattr(event_listener, "derive_obligations", fake_derive_obligations)
    monkeypatch.setattr(event_listener, "push_derivation", fake_push_derivation)

    body_dict = {"ai_system_id": "sys-beta"}
    raw_body = json.dumps(body_dict).encode("utf-8")

    response = client.post(
        EVENT_PATH,
        content=raw_body,
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 401
    assert calls == {"fetch": False, "derive": False, "push": False}


def test_invalid_signature_returns_401_and_no_downstream_calls(monkeypatch):
    _reset_replay_cache()
    calls = {"push": False}

    def fake_push_derivation(derivation, trigger_reason):
        calls["push"] = True
        return {}

    monkeypatch.setattr(event_listener, "push_derivation", fake_push_derivation)

    body_dict = {"ai_system_id": "sys-beta"}
    raw_body = json.dumps(body_dict).encode("utf-8")
    t = int(time.time())

    response = client.post(
        EVENT_PATH,
        content=raw_body,
        headers={
            "X-P2-Signature": f"t={t},v1=sha256=" + ("0" * 64),
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 401
    assert calls["push"] is False


# --------------------------------------------------------------------------
# IP allowlist (opt-in defense in depth)
# --------------------------------------------------------------------------


def test_ip_allowlist_disabled_by_default_does_not_affect_valid_requests(monkeypatch):
    _reset_replay_cache()
    assert settings.event_listener_ip_allowlist == ""

    def fake_fetch_and_build_graph(changed_since=None):
        return "FAKE_GRAPH"

    def fake_derive_obligations(graph, node_id, max_traversal_depth=None):
        return {
            "ai_system_id": "sys-beta",
            "derived_obligations": [],
            "derived_controls": [],
            "graph_path": [],
            "methodology_version": "p2-v1.0.0",
        }

    def fake_push_derivation(derivation, trigger_reason):
        return {"status": "ok"}

    monkeypatch.setattr(event_listener, "fetch_and_build_graph", fake_fetch_and_build_graph)
    monkeypatch.setattr(event_listener, "derive_obligations", fake_derive_obligations)
    monkeypatch.setattr(event_listener, "push_derivation", fake_push_derivation)

    body_dict = {"ai_system_id": "sys-beta"}
    raw_body = json.dumps(body_dict).encode("utf-8")
    header = _sign(raw_body)

    response = client.post(
        EVENT_PATH,
        content=raw_body,
        headers={"X-P2-Signature": header, "Content-Type": "application/json"},
    )
    assert response.status_code == 202


def test_ip_allowlist_rejects_non_allowlisted_client_even_with_valid_signature(monkeypatch):
    _reset_replay_cache()
    patched_settings = dataclasses.replace(settings, event_listener_ip_allowlist="10.0.0.1,10.0.0.2")
    monkeypatch.setattr(event_listener, "settings", patched_settings)

    body_dict = {"ai_system_id": "sys-beta"}
    raw_body = json.dumps(body_dict).encode("utf-8")
    header = _sign(raw_body)

    response = client.post(
        EVENT_PATH,
        content=raw_body,
        headers={"X-P2-Signature": header, "Content-Type": "application/json"},
    )
    # TestClient's request.client.host will be some loopback/test address --
    # never a member of the configured allowlist -- so this must be 403.
    assert response.status_code == 403


# --------------------------------------------------------------------------
# Secret redaction / no-deliberate-secret-logging
# --------------------------------------------------------------------------


def test_no_log_record_ever_contains_the_literal_shared_secret(monkeypatch):
    """Even at DEBUG level, no log record emitted while verifying signatures
    and handling webhook requests may contain the literal
    event_listener_shared_secret value. install_secret_redaction() is a
    defense-in-depth safety net; this test would catch either a redaction
    failure or a deliberate/accidental log of the raw secret."""
    import logging

    _reset_replay_cache()
    captured_records: list = []

    class _ListHandler(logging.Handler):
        def emit(self, record):
            captured_records.append(record)

    handler = _ListHandler()
    handler.setLevel(logging.DEBUG)
    event_listener.logger.addHandler(handler)
    event_listener.logger.setLevel(logging.DEBUG)

    monkeypatch.setattr(event_listener, "fetch_and_build_graph", lambda changed_since=None: "FAKE_GRAPH")
    monkeypatch.setattr(
        event_listener,
        "derive_obligations",
        lambda graph, node_id, max_traversal_depth=None: {
            "ai_system_id": "sys-beta",
            "derived_obligations": [],
            "derived_controls": [],
            "graph_path": [],
            "methodology_version": "p2-v1.0.0",
        },
    )
    monkeypatch.setattr(event_listener, "push_derivation", lambda derivation, trigger_reason: {"status": "ok"})

    try:
        body = b'{"ai_system_id": "sys-beta"}'
        good_header = _sign(body)

        # Drive a full request through the app (valid, then a rejected
        # replay) to exercise every log call site, including the background
        # task body (process_ai_system_changed).
        response = client.post(
            "/events/ai-system-changed",
            content=body,
            headers={"X-P2-Signature": good_header, "Content-Type": "application/json"},
        )
        assert response.status_code == 202

        replayed = client.post(
            "/events/ai-system-changed",
            content=body,
            headers={"X-P2-Signature": good_header, "Content-Type": "application/json"},
        )
        assert replayed.status_code == 401

        secret = settings.event_listener_shared_secret
        for record in captured_records:
            rendered = record.getMessage()
            assert secret not in rendered
            for key, value in vars(record).items():
                if key.startswith("p2_"):
                    assert secret not in str(value)
    finally:
        event_listener.logger.removeHandler(handler)
