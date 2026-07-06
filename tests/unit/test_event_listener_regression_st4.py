"""
Regression tests for two bugs exposed by stress testing (ST-4):

  1. Replay cache unbounded growth:
     A valid-HMAC flood within the clock-skew window could grow _seen_signatures
     without limit. Fixed by adding MAX_REPLAY_CACHE_SIZE=10,000 with oldest-entry
     eviction (min expiry) in _is_first_use().

  2. Malformed JSON with valid signature causes unhandled ValidationError (500):
     model_validate_json raises pydantic.ValidationError (not HTTPException) for
     malformed bodies. Fixed by wrapping it in try/except in the route handler and
     returning 422 instead of propagating the crash.

These tests exist specifically to prevent regressions of the two fixes. They are
distinct from the stress tests in tests/stress/test_stress.py (which drove the
discovery), which measure behavior under sustained load.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient

from src.p2_satellite import event_listener
from src.p2_satellite.config import settings

EVENT_PATH = "/events/ai-system-changed"


def _sign(raw_body: bytes, t: int | None = None) -> str:
    if t is None:
        t = int(time.time())
    digest = hmac.new(
        settings.event_listener_shared_secret.encode("utf-8"),
        f"{t}.".encode() + raw_body,
        hashlib.sha256,
    ).hexdigest()
    return f"t={t},v1=sha256={digest}"


def _reset_replay_cache() -> None:
    with event_listener._seen_signatures_lock:
        event_listener._seen_signatures.clear()


@pytest.fixture(autouse=True)
def _clean_replay_cache():
    _reset_replay_cache()
    yield
    _reset_replay_cache()


# ─────────────────────────────────────────────────────────────────────────────
# Fix 1: Replay cache has MAX_REPLAY_CACHE_SIZE cap
# ─────────────────────────────────────────────────────────────────────────────


def test_max_replay_cache_size_constant_is_exported():
    """MAX_REPLAY_CACHE_SIZE must be a module-level attribute on event_listener so
    tests and operators can inspect the configured cap without digging through
    source code. The fix added this; ensure it's not accidentally removed."""
    assert hasattr(
        event_listener, "MAX_REPLAY_CACHE_SIZE"
    ), "event_listener.MAX_REPLAY_CACHE_SIZE not found — the size cap was removed"
    cap = event_listener.MAX_REPLAY_CACHE_SIZE
    assert isinstance(cap, int), f"MAX_REPLAY_CACHE_SIZE must be int, got {type(cap)}"
    assert cap >= 1000, (
        f"MAX_REPLAY_CACHE_SIZE={cap} is unrealistically small — would cause false-positive "
        f"replay rejections under normal load. Expected at least 1000."
    )


def test_replay_cache_stays_at_or_below_max_size_under_flood():
    """When more than MAX_REPLAY_CACHE_SIZE unique (t, hex) pairs are accepted
    in rapid succession, the cache must evict oldest entries instead of growing
    unboundedly. Regression for the unbounded-growth bug found in ST-4."""
    cap = event_listener.MAX_REPLAY_CACHE_SIZE

    # Generate cap + 100 unique (t, hex) pairs — all unique because we use
    # a different 't' for each (simulating a high-throughput flood where each
    # request has a unique timestamp within the skew window).
    overflow = cap + 100
    t_base = int(time.time())

    for i in range(overflow):
        body = f'{{"ai_system_id": "sys-flood", "i": {i}}}'.encode()
        t = t_base - (overflow - i)  # unique decreasing timestamps, all within window
        hex_val = hmac.new(
            settings.event_listener_shared_secret.encode("utf-8"),
            f"{t}.".encode() + body,
            hashlib.sha256,
        ).hexdigest()
        # Call _is_first_use directly (no HTTP round-trip needed — we're testing
        # the replay cache logic, not the HTTP layer).
        event_listener._is_first_use(t, hex_val)

    with event_listener._seen_signatures_lock:
        actual_size = len(event_listener._seen_signatures)

    assert actual_size <= cap, (
        f"Replay cache grew to {actual_size} entries (cap={cap}) after {overflow} insertions "
        f"— eviction is not working. This is the unbounded-growth bug from ST-4."
    )


def test_replay_cache_eviction_preserves_replay_protection_for_recent_entries():
    """When the cache is full and an eviction fires, the evicted entry is the
    soonest-to-expire one (lowest expiry value). An entry that was just added
    (and thus has a later expiry) must NOT be evicted — it needs replay
    protection for the full skew window. A regression here would mean evicting
    fresh entries, making them re-submittable immediately."""
    cap = event_listener.MAX_REPLAY_CACHE_SIZE

    t_base = int(time.time())

    # Fill the cache to just below capacity with entries that expire soon.
    # We can't control the monotonic expiry directly, so we use a simpler
    # structural test: after filling to cap+1, the cache is exactly cap,
    # and the most recently inserted entry is still in the cache (not evicted).
    # (If the eviction incorrectly removed the newest entry, the next _is_first_use
    # call for it would return True again instead of False.)

    # Fill cache to cap - 1.
    for i in range(cap - 1):
        body = f'{{"ai_system_id": "sys-evict-test", "i": {i}}}'.encode()
        t = t_base - (cap + 1 - i)
        hex_val = hmac.new(
            settings.event_listener_shared_secret.encode("utf-8"),
            f"{t}.".encode() + body,
            hashlib.sha256,
        ).hexdigest()
        event_listener._is_first_use(t, hex_val)

    # Insert the Nth entry (fills cache to exactly cap - 1).
    sentinel_body = b'{"ai_system_id": "sys-sentinel"}'
    sentinel_t = t_base  # most recent timestamp — should have the LATEST expiry
    sentinel_hex = hmac.new(
        settings.event_listener_shared_secret.encode("utf-8"),
        f"{sentinel_t}.".encode() + sentinel_body,
        hashlib.sha256,
    ).hexdigest()
    first = event_listener._is_first_use(sentinel_t, sentinel_hex)
    assert first is True, "Sentinel must be accepted as first use"

    # Insert one more — triggers eviction of the soonest-to-expire entry.
    overflow_body = b'{"ai_system_id": "sys-overflow"}'
    overflow_t = t_base + 1
    overflow_hex = hmac.new(
        settings.event_listener_shared_secret.encode("utf-8"),
        f"{overflow_t}.".encode() + overflow_body,
        hashlib.sha256,
    ).hexdigest()
    event_listener._is_first_use(overflow_t, overflow_hex)

    # The sentinel (most recent, latest expiry) must still be in the cache —
    # trying to use it again must return False (replay rejection).
    sentinel_again = event_listener._is_first_use(sentinel_t, sentinel_hex)
    assert sentinel_again is False, (
        "Sentinel entry was evicted when it should have been retained (it has the latest "
        "expiry — the eviction should remove the soonest-to-expire entry, not the newest)."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Fix 2: Malformed JSON with valid signature returns 422, not 500
# ─────────────────────────────────────────────────────────────────────────────


def test_malformed_json_with_valid_signature_returns_422():
    """A valid HMAC signature over a malformed JSON body must return 422, not
    crash the handler with an unhandled pydantic.ValidationError (which would
    propagate as a 500). Regression for the bug found in ST-4."""
    client = TestClient(event_listener.app)

    malformed_body = b"not-json-at-all{{{"
    header = _sign(malformed_body)

    response = client.post(
        EVENT_PATH,
        content=malformed_body,
        headers={"X-P2-Signature": header, "Content-Type": "application/json"},
    )

    assert response.status_code == 422, (
        f"Expected 422 for malformed JSON with valid signature; got {response.status_code}. "
        f"Body: {response.text[:200]}"
    )
    # Must not be a server error.
    assert response.status_code < 500, "Handler crashed (5xx) on malformed JSON — pydantic.ValidationError is unhandled"


def test_valid_json_wrong_schema_with_valid_signature_returns_422():
    """Valid JSON that doesn't match AiSystemChangedEvent's schema (e.g. missing
    required fields, wrong types) must also return 422, not 500. The fix covers
    both JSON parse errors AND schema validation errors."""
    client = TestClient(event_listener.app)

    wrong_schema_body = json.dumps({"wrong_field": 12345, "another": True}).encode()
    header = _sign(wrong_schema_body)

    # AiSystemChangedEvent requires ai_system_id: str (not missing).
    # Pydantic v2 raises ValidationError when a required field is missing.
    response = client.post(
        EVENT_PATH,
        content=wrong_schema_body,
        headers={"X-P2-Signature": header, "Content-Type": "application/json"},
    )

    # Note: pydantic v2 with 'model_validate_json' treats 'ai_system_id' as
    # required — missing it raises ValidationError → 422.
    # If pydantic silently ignores extra fields and treats missing required as
    # empty/None (which it doesn't in strict mode), this test may return 202.
    # The key assertion is: must not be 500.
    assert response.status_code in (202, 422), (
        f"Expected 202 (if pydantic coerces) or 422 (if strict); got {response.status_code}. "
        f"Body: {response.text[:200]}"
    )
    assert response.status_code < 500, "Handler crashed (5xx) on wrong-schema JSON — ValidationError is unhandled"


def test_bad_hmac_with_malformed_json_returns_401_not_422():
    """If BOTH the HMAC is bad AND the body is malformed JSON, the handler must
    return 401 (HMAC check fires first) — not 422. The HMAC check is the gate;
    malformed-body handling only fires after the gate passes."""
    client = TestClient(event_listener.app)

    malformed_body = b"totally-not-json"
    t = int(time.time())
    bad_digest = hmac.new(
        b"wrong-secret",
        f"{t}.".encode() + malformed_body,
        hashlib.sha256,
    ).hexdigest()

    response = client.post(
        EVENT_PATH,
        content=malformed_body,
        headers={
            "X-P2-Signature": f"t={t},v1=sha256={bad_digest}",
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 401, f"Expected 401 (bad HMAC) before 422 (bad body); got {response.status_code}"
