"""
Unit tests for src/p2_satellite/ingest_client.py (Workstream D).

Covers:
  - push_derivation() sends the correct URL, Authorization header, and
    Idempotency-Key header.
  - derivation_hash is deterministic for identical input and changes when
    derived_obligations changes.
  - tenacity retry behavior: retries (and eventually raises) on a simulated
    persistent 500, does NOT retry on a simulated 422.

httpx is mocked via monkeypatch on httpx.Client, following the same fake
context-manager-client convention used in tests/unit/test_graph_builder.py
(no respx dependency available in this repo's requirements.txt).
"""

from __future__ import annotations

import httpx
import pytest

from src.p2_satellite import ingest_client
from src.p2_satellite.config import settings

SAMPLE_DERIVATION = {
    "ai_system_id": "sys-beta",
    "derived_obligations": ["gdpr_data_subject_rights", "euaiact_human_oversight"],
    "derived_controls": ["access_control", "audit_logging"],
    "graph_path": [["ai_system:sys-beta", "regulation:GDPR", "obligation:gdpr_data_subject_rights"]],
    "methodology_version": "p2-v1.0.0",
}


def _make_response(status_code: int, payload: dict | None = None) -> httpx.Response:
    request = httpx.Request("POST", "http://example.test")
    return httpx.Response(status_code, request=request, json=payload or {"ok": True})


class _FakeClient:
    """Fake httpx.Client context manager whose .post() is driven by a
    caller-supplied handler function."""

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


# --------------------------------------------------------------------------
# derivation_hash determinism
# --------------------------------------------------------------------------


def test_derivation_hash_is_deterministic_for_same_input():
    h1 = ingest_client.compute_derivation_hash(SAMPLE_DERIVATION)
    h2 = ingest_client.compute_derivation_hash(dict(SAMPLE_DERIVATION))
    assert h1 == h2
    assert len(h1) == 64  # sha256 hex digest length


def test_derivation_hash_ignores_graph_path_and_extra_fields():
    variant = dict(SAMPLE_DERIVATION)
    variant["graph_path"] = [["totally", "different", "path"]]
    variant["some_timestamp_field"] = "2026-07-06T00:00:00Z"
    assert ingest_client.compute_derivation_hash(SAMPLE_DERIVATION) == ingest_client.compute_derivation_hash(variant)


def test_derivation_hash_changes_when_obligations_change():
    variant = dict(SAMPLE_DERIVATION)
    variant["derived_obligations"] = ["dpdp_consent_notice"]
    h1 = ingest_client.compute_derivation_hash(SAMPLE_DERIVATION)
    h2 = ingest_client.compute_derivation_hash(variant)
    assert h1 != h2


def test_derivation_hash_changes_when_methodology_version_changes():
    variant = dict(SAMPLE_DERIVATION)
    variant["methodology_version"] = "p2-v2.0.0"
    h1 = ingest_client.compute_derivation_hash(SAMPLE_DERIVATION)
    h2 = ingest_client.compute_derivation_hash(variant)
    assert h1 != h2


# --------------------------------------------------------------------------
# push_derivation -- URL / headers / body
# --------------------------------------------------------------------------


def test_push_derivation_sends_correct_url_and_headers(monkeypatch):
    captured = {}

    def handler(url, json_body, headers):
        captured["url"] = url
        captured["json"] = json_body
        captured["headers"] = headers
        return _make_response(200, {"status": "accepted"})

    monkeypatch.setattr(httpx, "Client", _FakeClient(handler))

    result = ingest_client.push_derivation(SAMPLE_DERIVATION, trigger_reason="event")

    assert captured["url"] == f"{settings.core_base_url}{ingest_client.INGEST_PATH}"
    assert captured["headers"]["Authorization"] == f"Bearer {settings.core_ingest_api_key}"

    expected_hash = ingest_client.compute_derivation_hash(SAMPLE_DERIVATION)
    assert captured["headers"]["Idempotency-Key"] == expected_hash
    assert captured["json"]["derivation_hash"] == expected_hash
    assert captured["json"]["trigger_reason"] == "event"
    assert captured["json"]["ai_system_id"] == "sys-beta"

    assert result == {"status": "accepted"}


def test_push_derivation_rejects_invalid_trigger_reason():
    with pytest.raises(ValueError):
        ingest_client.push_derivation(SAMPLE_DERIVATION, trigger_reason="bogus")


# --------------------------------------------------------------------------
# retry behavior
# --------------------------------------------------------------------------


def test_retries_on_persistent_500_then_raises(monkeypatch):
    attempts = {"n": 0}

    def handler(url, json_body, headers):
        attempts["n"] += 1
        return _make_response(500, {"error": "internal"})

    monkeypatch.setattr(httpx, "Client", _FakeClient(handler))

    with pytest.raises(ingest_client.TransientIngestError):
        ingest_client.push_derivation(SAMPLE_DERIVATION, trigger_reason="scheduled")

    assert attempts["n"] == 3  # stop_after_attempt(3)


def test_retries_then_succeeds_on_transient_500(monkeypatch):
    attempts = {"n": 0}

    def handler(url, json_body, headers):
        attempts["n"] += 1
        if attempts["n"] < 2:
            return _make_response(503, {"error": "unavailable"})
        return _make_response(200, {"status": "accepted"})

    monkeypatch.setattr(httpx, "Client", _FakeClient(handler))

    result = ingest_client.push_derivation(SAMPLE_DERIVATION, trigger_reason="event")

    assert attempts["n"] == 2
    assert result == {"status": "accepted"}


def test_does_not_retry_on_422(monkeypatch):
    attempts = {"n": 0}

    def handler(url, json_body, headers):
        attempts["n"] += 1
        return _make_response(422, {"error": "unknown obligation id"})

    monkeypatch.setattr(httpx, "Client", _FakeClient(handler))

    with pytest.raises(ingest_client.PermanentIngestError):
        ingest_client.push_derivation(SAMPLE_DERIVATION, trigger_reason="event")

    assert attempts["n"] == 1


def test_retries_on_connect_error_then_succeeds(monkeypatch):
    attempts = {"n": 0}

    def handler(url, json_body, headers):
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise httpx.ConnectError("boom", request=httpx.Request("POST", url))
        return _make_response(200, {"status": "accepted"})

    monkeypatch.setattr(httpx, "Client", _FakeClient(handler))

    result = ingest_client.push_derivation(SAMPLE_DERIVATION, trigger_reason="scheduled")

    assert attempts["n"] == 2
    assert result == {"status": "accepted"}


# --------------------------------------------------------------------------
# Connection-dies-mid-push: proof that derivation_hash / Idempotency-Key is
# byte-identical across every retry attempt (and across repeat calls), and
# that a fully-exhausted retry propagates rather than being swallowed.
# --------------------------------------------------------------------------


def test_hash_and_idempotency_key_identical_across_retry_attempts(monkeypatch):
    """Simulates a connection dying mid-push: the FIRST attempt raises
    httpx.ConnectError, the SECOND (tenacity retry) succeeds. Captures the
    request body/headers on both attempts and asserts the derivation_hash
    (both as a body field and as the Idempotency-Key header) is byte-
    identical on both -- proving it's computed once, not per-attempt."""
    captured_attempts = []

    def handler(url, json_body, headers):
        captured_attempts.append({"json": dict(json_body), "headers": dict(headers)})
        if len(captured_attempts) == 1:
            raise httpx.ConnectError("connection dropped mid-push", request=httpx.Request("POST", url))
        return _make_response(200, {"status": "accepted"})

    monkeypatch.setattr(httpx, "Client", _FakeClient(handler))

    result = ingest_client.push_derivation(SAMPLE_DERIVATION, trigger_reason="event")

    assert len(captured_attempts) == 2
    assert result == {"status": "accepted"}

    first, second = captured_attempts
    assert first["json"]["derivation_hash"] == second["json"]["derivation_hash"]
    assert first["headers"]["Idempotency-Key"] == second["headers"]["Idempotency-Key"]
    # Body/headers are byte-identical across both attempts, not merely the
    # hash field -- proves tenacity is re-sending the exact same request, not
    # regenerating a derivation-hash-dependent payload per attempt.
    assert first["json"] == second["json"]
    assert first["headers"] == second["headers"]

    # A second, wholly independent call with the exact same derivation dict
    # produces the exact same derivation_hash again -- purely a function of
    # derivation content, not of attempt number or wall-clock time.
    repeat_hash = ingest_client.compute_derivation_hash(SAMPLE_DERIVATION)
    assert repeat_hash == first["json"]["derivation_hash"]


def test_push_derivation_propagates_after_all_retries_exhausted_on_connect_error(monkeypatch, caplog):
    """All 3 attempts fail with a connection error -- push_derivation must
    raise (propagate), not silently swallow, and must log a structured
    ingest_push.failed ERROR event carrying ai_system_id/trigger_reason/
    derivation_hash as context (via observability.timed_stage)."""
    import logging

    attempts = {"n": 0}

    def handler(url, json_body, headers):
        attempts["n"] += 1
        raise httpx.ConnectError("connection dropped", request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "Client", _FakeClient(handler))

    expected_hash = ingest_client.compute_derivation_hash(SAMPLE_DERIVATION)

    with (
        caplog.at_level(logging.ERROR, logger="src.p2_satellite.ingest_client"),
        pytest.raises(ingest_client.TransientIngestError),
    ):
        ingest_client.push_derivation(SAMPLE_DERIVATION, trigger_reason="event")

    assert attempts["n"] == 3

    failed_records = [r for r in caplog.records if getattr(r, "p2_event", None) == "ingest_push.failed"]
    assert len(failed_records) == 1
    record = failed_records[0]
    assert record.p2_ai_system_id == SAMPLE_DERIVATION["ai_system_id"]
    assert record.p2_trigger_reason == "event"
    assert record.p2_derivation_hash == expected_hash


# --------------------------------------------------------------------------
# Secret redaction / no-deliberate-secret-logging
# --------------------------------------------------------------------------


def test_push_never_logs_the_literal_ingest_api_key(monkeypatch):
    """Even at DEBUG level, no log record emitted during push_derivation
    (success or failure path) may contain the literal core_ingest_api_key
    value. install_secret_redaction() is a defense-in-depth safety net --
    this test would catch either a redaction failure or a deliberate/
    accidental log of the raw key."""
    import logging

    captured_records: list[logging.LogRecord] = []

    class _ListHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured_records.append(record)

    handler = _ListHandler()
    handler.setLevel(logging.DEBUG)
    ingest_client.logger.addHandler(handler)
    ingest_client.logger.setLevel(logging.DEBUG)

    attempts = {"n": 0}

    def handler_fn(url, json_body, headers):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise httpx.ConnectError("boom", request=httpx.Request("POST", url))
        return _make_response(200, {"status": "accepted"})

    monkeypatch.setattr(httpx, "Client", _FakeClient(handler_fn))

    try:
        ingest_client.push_derivation(SAMPLE_DERIVATION, trigger_reason="event")

        # Also exercise the failure/log path.
        def always_fail(url, json_body, headers):
            raise httpx.ConnectError("boom", request=httpx.Request("POST", url))

        monkeypatch.setattr(httpx, "Client", _FakeClient(always_fail))
        with pytest.raises(ingest_client.TransientIngestError):
            ingest_client.push_derivation(SAMPLE_DERIVATION, trigger_reason="scheduled")

        secret = settings.core_ingest_api_key
        for record in captured_records:
            rendered = record.getMessage()
            assert secret not in rendered
            for key, value in vars(record).items():
                if key.startswith("p2_"):
                    assert secret not in str(value)
    finally:
        ingest_client.logger.removeHandler(handler)
