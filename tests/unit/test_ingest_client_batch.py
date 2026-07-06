"""
Tests for src/p2_satellite/ingest_client.py :: push_derivations_batch (added
in the production-hardening pass so scheduler.py's safety-net sweep sends one
HTTP request for N ai_systems instead of N requests).

Mirrors tests/unit/test_ingest_client.py's fake-httpx-client convention.
"""

from __future__ import annotations

import httpx

from src.p2_satellite import ingest_client
from src.p2_satellite.config import settings

DERIVATION_A = {
    "ai_system_id": "sys-alpha",
    "derived_obligations": ["gdpr_data_subject_rights"],
    "derived_controls": ["access_control"],
    "graph_path": [],
    "methodology_version": "p2-v1.0.0",
}
DERIVATION_B = {
    "ai_system_id": "sys-beta",
    "derived_obligations": ["dpdp_consent_notice"],
    "derived_controls": ["consent_management"],
    "graph_path": [],
    "methodology_version": "p2-v1.0.0",
}


def _make_response(status_code: int, payload: dict) -> httpx.Response:
    request = httpx.Request("POST", "http://example.test")
    return httpx.Response(status_code, request=request, json=payload)


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


def test_push_derivations_batch_empty_list_is_a_noop(monkeypatch):
    def handler(url, body, headers):
        raise AssertionError("should never make an HTTP call for an empty batch")

    monkeypatch.setattr(httpx, "Client", _FakeClient(handler))
    result = ingest_client.push_derivations_batch([], trigger_reason="scheduled")
    assert result == {"results": []}


def test_push_derivations_batch_sends_correct_url_and_per_item_hashes(monkeypatch):
    captured = {}

    def handler(url, body, headers):
        captured["url"] = url
        captured["body"] = body
        captured["headers"] = headers
        return _make_response(
            200,
            {
                "results": [
                    {"ai_system_id": "sys-alpha", "ok": True},
                    {"ai_system_id": "sys-beta", "ok": True},
                ]
            },
        )

    monkeypatch.setattr(httpx, "Client", _FakeClient(handler))

    result = ingest_client.push_derivations_batch([DERIVATION_A, DERIVATION_B], trigger_reason="scheduled")

    assert captured["url"] == f"{settings.core_base_url}{ingest_client.BATCH_INGEST_PATH}"
    assert captured["headers"]["Authorization"] == f"Bearer {settings.core_ingest_api_key}"
    # No single batch-level Idempotency-Key -- each item carries its own hash.
    assert "Idempotency-Key" not in captured["headers"]

    items = captured["body"]["derivations"]
    assert len(items) == 2
    for item, source in zip(items, [DERIVATION_A, DERIVATION_B], strict=True):
        assert item["trigger_reason"] == "scheduled"
        assert item["derivation_hash"] == ingest_client.compute_derivation_hash(source)

    assert len(result["results"]) == 2


def test_push_derivations_batch_rejects_invalid_trigger_reason():
    try:
        ingest_client.push_derivations_batch([DERIVATION_A], trigger_reason="bogus")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
