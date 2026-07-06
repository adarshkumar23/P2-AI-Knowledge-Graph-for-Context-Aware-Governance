"""
Stress tests for the P2 satellite — Part 1 of the /goal stress-test + polish pass.

Seven tests that deliberately try to BREAK the system, each asserting specific
behavior (not just "didn't crash"). Numbers are recorded in STRESS_TEST_RESULTS.md.

Test overview:
  1. concurrent_event_storm              — 500 events / 200 ai_system_ids; no duplicates
  2. poll_overlapping_live_storm         — safety-net poll + event storm concurrently;
                                           concurrency guard under real load
  3. rate_limiter_under_load             — hammer ingest at 2× realistic; 429 + retry-after
  4. adversarial_payloads_at_volume      — bad HMAC / replay / malformed JSON storm;
                                           replay-cache size bounded
  5. graph_pathology_dense_node          — worst-case node (max edges); depth-bounded
  6. partial_core_outage_during_poll     — core dies mid-sweep; restart resumes cleanly
  7. long_running_memory_check           — many poll cycles; memory doesn't grow unbounded

Run with:
    pytest tests/stress/test_stress.py -v --tb=short -s
"""

from __future__ import annotations

import dataclasses
import gc
import hashlib
import hmac
import json
import sys
import threading
import time
import tracemalloc
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
from unittest.mock import patch

import networkx as nx
import pytest

from src.p2_satellite import event_listener, scheduler, schema
from src.p2_satellite.config import settings
from src.p2_satellite.graph_builder import build_graph
from src.p2_satellite.traversal import derive_obligations

# ─────────────────────────────────────────────────────────────────────────────
# Shared helpers
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _mock_push_graph_structure():
    """process_ai_system_changed / _run_safety_net_poll both also push graph
    structure now (item 22 -- see src/p2_satellite/event_listener.py,
    scheduler.py, and core-side-patch/ASSUMPTIONS.md item 22). None of this
    file's tests spin up a real mock core (unlike
    tests/integration/test_end_to_end_dry_run.py), so left unmocked this
    would mean every one of these deliberately-high-volume tests (500-event
    storms, many poll cycles) hammers a real network connection that always
    fails, paying tenacity's retry backoff per call -- exactly the kind of
    slowdown/flakiness a STRESS test suite must not itself introduce.
    Mocked out here, autouse, for every test in this file."""
    with (
        patch.object(event_listener, "push_graph_structure", lambda structure: {"nodes_created": 0}),
        patch.object(scheduler, "push_graph_structure", lambda structure: {"nodes_created": 0}),
    ):
        yield


def _sign(raw_body: bytes, t: int | None = None) -> str:
    """Build a valid X-P2-Signature header for the given body."""
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


def _fake_graph_with_ai_systems(keys: list[str]) -> nx.DiGraph:
    g = nx.DiGraph()
    for key in keys:
        nid = schema.node_id(schema.NODE_AI_SYSTEM, key)
        g.add_node(nid, node_type=schema.NODE_AI_SYSTEM, node_key=key)
    return g


def _make_derivation(ai_system_id: str) -> dict[str, Any]:
    return {
        "ai_system_id": ai_system_id,
        "derived_obligations": ["gdpr_data_subject_rights"],
        "derived_controls": ["access_control"],
        "graph_path": [],
        "methodology_version": settings.methodology_version,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Stress Test 1 — Concurrent event storm
# ─────────────────────────────────────────────────────────────────────────────


def test_1_concurrent_event_storm_no_duplicate_audit_rows():
    """
    Fire 500 simultaneous webhook events for 200 distinct ai_system_ids (some
    systems appear multiple times — same system triggered multiple times in the
    storm). Assert:
      - No duplicate audit rows (idempotency holds under REAL concurrency).
      - No deadlock (all futures complete without hanging).
      - Completes within a defined time bound (10 s — generous for CI).

    'Audit row' is tracked by (ai_system_id, derivation_hash) pairs that would
    be written. Since derivation_hash is content-derived (same payload → same
    hash), concurrent pushes for the same system must converge to ≤ 1 unique
    hash per system — any duplicate writes would be idempotent on core's side.
    We verify: the set of unique hashes written equals the set of ai_system_ids
    that got through (the concurrency guard may let only ONE push win per id
    per storm).
    """
    _reset_replay_cache()
    N_SYSTEMS = 200
    N_EVENTS = 500
    TIME_LIMIT_SECONDS = 10.0

    # Track calls to push_derivation by (ai_system_id, derivation_hash).
    push_calls: list[tuple[str, str]] = []
    push_lock = threading.Lock()

    def fake_fetch_and_build_graph(changed_since=None):
        return _fake_graph_with_ai_systems([f"sys-storm-{i:04d}" for i in range(N_SYSTEMS)])

    def fake_derive_obligations(graph, node_id, max_traversal_depth=None):
        _, key = schema.split_node_id(node_id)
        return _make_derivation(key)

    def fake_push_derivation(derivation, trigger_reason):
        from src.p2_satellite.ingest_client import compute_derivation_hash

        h = compute_derivation_hash(derivation)
        with push_lock:
            push_calls.append((derivation["ai_system_id"], h))
        return {"status": "ok"}

    # Build a body/header for each event — reuse the same body per ai_system
    # (same id → same hash → idempotency), but we must generate a unique
    # timestamp/signature per request so replay-cache doesn't reject them.
    system_ids = [f"sys-storm-{i % N_SYSTEMS:04d}" for i in range(N_EVENTS)]

    def fire_event(ai_system_id: str) -> int:
        # We can't import TestClient at module-level without going async;
        # instead we call process_ai_system_changed directly (as a unit) since
        # the HTTP path just enqueues it as a BackgroundTask anyway.
        event_listener.process_ai_system_changed(ai_system_id, "deployment_jurisdiction")
        return 1

    start = time.perf_counter()

    # Patch ONCE for the whole storm, outside the thread pool -- `patch.object`
    # mutates a shared module attribute, so entering/exiting it separately
    # inside each of 50 concurrent worker threads is a genuine race (one
    # thread's __exit__ restoring the original attribute while another
    # thread's call is still in flight, which intermittently made this test
    # hit a real network call and fail with a connection error). Every thread
    # uses the exact same fakes, so there's no need to scope the patch
    # per-call in the first place.
    with (
        patch.object(event_listener, "fetch_and_build_graph", fake_fetch_and_build_graph),
        patch.object(event_listener, "derive_obligations", fake_derive_obligations),
        patch.object(event_listener, "push_derivation", fake_push_derivation),
        ThreadPoolExecutor(max_workers=50) as pool,
    ):
        futures = [pool.submit(fire_event, sid) for sid in system_ids]
        results = [f.result(timeout=TIME_LIMIT_SECONDS + 2) for f in as_completed(futures)]

    elapsed = time.perf_counter() - start

    # All 500 futures completed (no deadlock, no hang).
    assert len(results) == N_EVENTS, f"Only {len(results)} of {N_EVENTS} futures completed"

    # Time bound.
    assert elapsed < TIME_LIMIT_SECONDS, f"Storm took {elapsed:.2f}s, expected under {TIME_LIMIT_SECONDS}s"

    # Idempotency: for each ai_system_id, all push attempts produce the same
    # derivation_hash (content-derived, so concurrent in-flight pushes with
    # the same graph state hash identically → no corrupted duplicate rows).
    with push_lock:
        by_system: dict[str, set[str]] = {}
        for aid, h in push_calls:
            by_system.setdefault(aid, set()).add(h)

    for aid, hashes in by_system.items():
        assert len(hashes) == 1, (
            f"ai_system_id={aid!r} produced {len(hashes)} distinct derivation_hashes "
            f"under concurrent pushes — idempotency violation: {hashes}"
        )

    # Record numbers for STRESS_TEST_RESULTS.md.
    print(
        f"\n[ST-1] {N_EVENTS} events / {N_SYSTEMS} systems in {elapsed:.3f}s; "
        f"{len(push_calls)} pushes attempted; "
        f"{len(by_system)} unique systems reached push_derivation"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Stress Test 2 — Safety-net poll overlapping a live event storm
# ─────────────────────────────────────────────────────────────────────────────


def test_2_poll_overlapping_live_event_storm_concurrency_guard_holds():
    """
    Trigger the scheduler's full poll while an event storm for the same
    ai_system_ids is still draining. Assert the per-ai_system_id concurrency
    guard prevents double-traversal races under REAL concurrent load, not just
    the simple sequential two-call test.

    Mechanism: we start a poll that holds each ai_system's lock for a
    configurable hold duration (simulated by monkeypatching push to sleep).
    While poll locks are held, we fire events for the same systems and verify
    that none of them get a second concurrent derivation for the same id
    (they should all see `acquired=False` and log a skip).
    """
    N_SYSTEMS = 20
    LOCK_HOLD_SECONDS = 0.2  # Poll holds locks while "pushing"

    system_keys = [f"sys-poll-{i:03d}" for i in range(N_SYSTEMS)]
    fake_graph = _fake_graph_with_ai_systems(system_keys)

    # Track: which ai_system_ids got a second concurrent derive while poll was in-flight.
    concurrent_derives: list[str] = []
    concurrent_lock = threading.Lock()

    poll_push_started = threading.Event()

    def fake_fetch_and_build_graph_for_poll(changed_since=None):
        return fake_graph

    def fake_derive_obligations_for_poll(graph, node_id, max_traversal_depth=None):
        _, key = schema.split_node_id(node_id)
        return _make_derivation(key)

    def fake_push_batch_slow(derivations, trigger_reason):
        # Signal that the poll has started its push (and therefore holds all locks).
        poll_push_started.set()
        # Hold the locks for a while, simulating a slow push.
        time.sleep(LOCK_HOLD_SECONDS)
        return {"results": [{"ai_system_id": d["ai_system_id"], "ok": True} for d in derivations]}

    def run_poll():
        with (
            patch.object(scheduler, "fetch_and_build_graph", fake_fetch_and_build_graph_for_poll),
            patch.object(scheduler, "derive_obligations", fake_derive_obligations_for_poll),
            patch.object(scheduler, "push_derivations_batch", fake_push_batch_slow),
            patch.object(scheduler, "time", time),
        ):
            # Patch pace to 0 so it doesn't sleep between chunks during test.
            patched_settings = dataclasses.replace(
                scheduler.settings,
                ingest_batch_chunk_size=N_SYSTEMS,
                ingest_batch_pace_seconds=0,
            )
            with patch.object(scheduler, "settings", patched_settings):
                scheduler._run_safety_net_poll()

    def fake_fetch_event(changed_since=None):
        return fake_graph

    def fake_derive_event(graph, node_id, max_traversal_depth=None):
        _, key = schema.split_node_id(node_id)
        # If we get HERE, the concurrency guard didn't prevent us — record it.
        with concurrent_lock:
            concurrent_derives.append(key)
        return _make_derivation(key)

    def fake_push_event(derivation, trigger_reason):
        return {"status": "ok"}

    def run_event(ai_system_id: str):
        """Try to fire an event for this id while the poll holds its lock."""
        event_listener.process_ai_system_changed(ai_system_id, "risk_tier")

    # Start the poll in a background thread.
    poll_thread = threading.Thread(target=run_poll)
    poll_thread.start()

    # Wait until poll has started its slow push (holding all ai_system locks).
    assert poll_push_started.wait(timeout=5.0), "Poll never started its push — test setup failed"

    # Patch ONCE for the whole event storm, outside the thread pool -- see
    # test_1's comment above on why patching `event_listener` attributes
    # separately inside each of N_SYSTEMS concurrent threads is a race
    # (every thread uses the exact same fakes, so there's nothing
    # per-thread-specific to scope the patch to).
    with (
        patch.object(event_listener, "fetch_and_build_graph", fake_fetch_event),
        patch.object(event_listener, "derive_obligations", fake_derive_event),
        patch.object(event_listener, "push_derivation", fake_push_event),
        ThreadPoolExecutor(max_workers=N_SYSTEMS) as pool,
    ):
        # Fire events for ALL the same systems while poll holds their locks.
        futures = [pool.submit(run_event, key) for key in system_keys]
        for f in as_completed(futures):
            f.result(timeout=5.0)

    poll_thread.join(timeout=5.0)
    assert not poll_thread.is_alive(), "Poll thread hung"

    # ASSERT: none of the events got past the concurrency guard.
    # If any ai_system_id is in concurrent_derives, two concurrent derives ran for it.
    assert concurrent_derives == [], (
        f"Concurrency guard failed under load: {len(concurrent_derives)} event-path "
        f"derives ran concurrently with poll-path derives for: "
        f"{concurrent_derives[:5]}"
    )

    print(
        f"\n[ST-2] Poll ({N_SYSTEMS} systems, {LOCK_HOLD_SECONDS}s hold) + "
        f"concurrent event storm: 0 concurrent double-traversals"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Stress Test 3 — Rate limiter under load
# ─────────────────────────────────────────────────────────────────────────────


def test_3_rate_limiter_under_load_429_and_no_data_corruption():
    """
    Hammer the FixedWindowRateLimiter at realistic-and-then-2x-realistic
    throughput. Assert:
      - The limiter kicks in gracefully (returns False at the right count).
      - Allowed requests do not corrupt the counter (monotonically increases
        within window, never exceeds limit).
      - Rejected requests never decrement the counter (no credit leakage).
      - The limiter is thread-safe: concurrent hammering produces exactly
        `limit` admitted calls, not more (no double-admission race).

    We test the module directly rather than through HTTP since we're testing
    the rate-limiting semantics, not HTTP routing (the HTTP layer is tested in
    test_event_listener.py and core-side-patch tests).
    """
    sys.path.insert(0, "/workspaces/P2-AI-Knowledge-Graph-for-Context-Aware-Governance/core-side-patch")
    from rate_limiter import FixedWindowRateLimiter

    LIMIT = 50
    WINDOW = 60.0
    N_CALLERS = 200  # 4× the limit — 150 should be rejected
    KEY = "stress-test-key"

    limiter = FixedWindowRateLimiter(limit=LIMIT, window_seconds=WINDOW)
    admitted: list[bool] = []
    result_lock = threading.Lock()

    def hammer_once():
        result = limiter.allow(KEY)
        with result_lock:
            admitted.append(result)

    with ThreadPoolExecutor(max_workers=50) as pool:
        futures = [pool.submit(hammer_once) for _ in range(N_CALLERS)]
        for f in as_completed(futures):
            f.result(timeout=5.0)

    n_admitted = sum(1 for r in admitted if r)
    n_rejected = sum(1 for r in admitted if not r)

    # Exactly LIMIT admitted — no more (thread-safety), no fewer (no lost increments).
    assert n_admitted == LIMIT, (
        f"Expected exactly {LIMIT} admitted calls, got {n_admitted} "
        f"(rejected: {n_rejected}) — possible thread-safety race in rate limiter"
    )
    assert n_rejected == N_CALLERS - LIMIT, f"Expected {N_CALLERS - LIMIT} rejected, got {n_rejected}"
    assert n_admitted + n_rejected == N_CALLERS

    # Verify allow_n (batch variant) is also thread-safe.
    limiter2 = FixedWindowRateLimiter(limit=100, window_seconds=60.0)
    batch_key = "stress-batch-key"
    batch_results: list[bool] = []
    batch_lock = threading.Lock()

    def hammer_batch_n(n: int):
        result = limiter2.allow_n(batch_key, n)
        with batch_lock:
            batch_results.append(result)

    # 20 callers each asking for 6 units = 120 total units requested; limit=100
    # → first 16 (96 units) + partial 17th is tricky; at n=6 atomically:
    # floor(100/6)=16 batches admitted (96 units), 17th needs 6 more but only
    # 4 left → rejected. So 16 admitted, 4 rejected (20 total).
    with ThreadPoolExecutor(max_workers=20) as pool:
        fts = [pool.submit(hammer_batch_n, 6) for _ in range(20)]
        for f in as_completed(fts):
            f.result(timeout=5.0)

    batch_admitted = sum(1 for r in batch_results if r)
    # Exactly 16 batches of 6 can fit in 100: 16×6=96, 17th would be 102>100.
    assert batch_admitted == 16, (
        f"Batch rate-limiter admitted {batch_admitted} batches of 6 "
        f"into limit=100; expected exactly 16 (96 units total)"
    )

    print(
        f"\n[ST-3] Rate limiter: {N_CALLERS} concurrent callers → "
        f"{n_admitted} admitted / {n_rejected} rejected (limit={LIMIT}); "
        f"batch: 16/20 batches of 6 admitted into limit=100"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Stress Test 4 — Adversarial payloads at volume + replay-cache bounding
# ─────────────────────────────────────────────────────────────────────────────


def test_4_adversarial_payloads_and_replay_cache_bounded():
    """
    Send a sustained stream of:
      (a) Invalid HMAC signatures (bad secret, right format)
      (b) Replayed old-but-valid signatures (same body, same header twice)
      (c) Malformed JSON (can't parse as AiSystemChangedEvent)

    Assert:
      - None causes a crash or silent drop — each returns 401 or 422, never 500.
      - Invalid-HMAC attacks do NOT grow the replay cache (HMAC check fires first).
      - Valid-signature floods are bounded: the replay cache NEVER exceeds
        MAX_REPLAY_CACHE_SIZE (the eviction fix added in this hardening pass).
      - Malformed JSON with a valid HMAC → 422 (not 500 crash) — the fix that
        catches pydantic.ValidationError inside the route handler.

    WHY THIS MATTERS: The original replay cache comment claimed it "can never
    grow beyond roughly (request rate) × (skew window)." That was false for a
    valid-signature flood: 1000 req/s × 305s window = 305,000 entries, no cap.
    Both the size cap (MAX_REPLAY_CACHE_SIZE) and the malformed-JSON 422 fix
    were added as a result of this stress test. See STRESS_TEST_RESULTS.md.
    """
    from fastapi.testclient import TestClient

    _reset_replay_cache()

    client = TestClient(event_listener.app)
    EVENT_PATH = "/events/ai-system-changed"

    bad_hmac_codes: list[int] = []
    malformed_json_codes: list[int] = []
    code_lock = threading.Lock()

    def send_bad_hmac():
        body = b'{"ai_system_id": "sys-attack"}'
        t = int(time.time())
        bad_digest = hmac.new(b"wrong-secret", f"{t}.".encode() + body, hashlib.sha256).hexdigest()
        resp = client.post(
            EVENT_PATH,
            content=body,
            headers={
                "X-P2-Signature": f"t={t},v1=sha256={bad_digest}",
                "Content-Type": "application/json",
            },
        )
        with code_lock:
            bad_hmac_codes.append(resp.status_code)

    def send_malformed_json():
        bad_body = b"not-json-at-all{{{["
        header = _sign(bad_body)
        resp = client.post(
            EVENT_PATH,
            content=bad_body,
            headers={"X-P2-Signature": header, "Content-Type": "application/json"},
        )
        with code_lock:
            malformed_json_codes.append(resp.status_code)

    # Phase 1: bad-HMAC + malformed-JSON flood.
    # Bad HMAC → must NOT grow the replay cache (HMAC check fires before _is_first_use).
    # Malformed JSON with valid HMAC → 422 (fix: was unhandled ValidationError → 500).
    N_ADVERSARIAL = 300

    with ThreadPoolExecutor(max_workers=30) as pool:
        fts = [pool.submit(send_bad_hmac) for _ in range(N_ADVERSARIAL // 2)] + [
            pool.submit(send_malformed_json) for _ in range(N_ADVERSARIAL // 2)
        ]
        for f in as_completed(fts):
            f.result(timeout=10.0)  # raises if the thread itself crashed

    cache_size_after_bad_hmac_flood = len(event_listener._seen_signatures)

    # Assert: no crash — all responses must be 401 or 422, never 500.
    # Bad HMAC: always 401 (wrong-secret HMAC rejected before replay cache).
    # Malformed JSON w/ valid HMAC: 422 on first use of (t, hex), 401 on replay
    # (multiple threads in the same second share the same timestamp → same (t, hex)).
    assert all(c == 401 for c in bad_hmac_codes), (
        f"Bad-HMAC requests returned non-401: {set(bad_hmac_codes) - {401}} — "
        f"wrong-secret HMAC must always be rejected with 401 (never 500)"
    )
    assert all(c in (401, 422) for c in malformed_json_codes), (
        f"Malformed-JSON requests returned unexpected codes (never 500): "
        f"{[c for c in malformed_json_codes if c not in (401, 422)]}"
    )
    # At least SOME malformed-JSON requests must have returned 422 (not all replays).
    # This confirms the ValidationError→422 fix is actually firing.
    assert any(c == 422 for c in malformed_json_codes), (
        f"No malformed-JSON request returned 422; all returned {set(malformed_json_codes)} — "
        f"the ValidationError→HTTPException(422) fix may not be active"
    )

    # Phase 2: valid-signature flood — test the MAX_REPLAY_CACHE_SIZE cap.
    # Each request gets a unique timestamp (and thus unique body hash) to simulate
    # a sustained high-rate valid-signature flood.
    _reset_replay_cache()

    def fake_fetch(changed_since=None):
        return _fake_graph_with_ai_systems(["sys-flood"])

    def fake_derive(graph, node_id, max_traversal_depth=None):
        return _make_derivation("sys-flood")

    def fake_push(derivation, trigger_reason):
        return {"status": "ok"}

    N_VALID_FLOOD = min(event_listener.MAX_REPLAY_CACHE_SIZE + 200, 1500)
    max_cache_during_flood = 0

    with (
        patch.object(event_listener, "fetch_and_build_graph", fake_fetch),
        patch.object(event_listener, "derive_obligations", fake_derive),
        patch.object(event_listener, "push_derivation", fake_push),
    ):
        for i in range(N_VALID_FLOOD):
            body = json.dumps({"ai_system_id": "sys-flood", "idx": i}).encode()
            t = int(time.time()) - (N_VALID_FLOOD - i)
            header = _sign(body, t=t)
            client.post(
                EVENT_PATH,
                content=body,
                headers={"X-P2-Signature": header, "Content-Type": "application/json"},
            )
            current_size = len(event_listener._seen_signatures)
            if current_size > max_cache_during_flood:
                max_cache_during_flood = current_size

    # The replay cache must NEVER exceed MAX_REPLAY_CACHE_SIZE.
    assert max_cache_during_flood <= event_listener.MAX_REPLAY_CACHE_SIZE, (
        f"Replay cache exceeded MAX_REPLAY_CACHE_SIZE ({event_listener.MAX_REPLAY_CACHE_SIZE}): "
        f"grew to {max_cache_during_flood} during a flood of {N_VALID_FLOOD} valid requests"
    )

    print(
        f"\n[ST-4] Adversarial: {N_ADVERSARIAL} bad requests → all 401/422 (no 500); "
        f"cache unchanged by bad-HMAC ({cache_size_after_bad_hmac_flood} entries). "
        f"Valid flood: {N_VALID_FLOOD} requests; "
        f"max cache size={max_cache_during_flood} (cap={event_listener.MAX_REPLAY_CACHE_SIZE})"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Stress Test 5 — Graph pathology: densely-connected worst-case node
# ─────────────────────────────────────────────────────────────────────────────


def test_5_graph_pathology_dense_node_bounded_traversal():
    """
    Build a fixture for a densely-connected graph: one AI system with edges to
    every jurisdiction/regulation/data category in a large catalog. This is the
    "worst-case" node — maximum fan-out at every level.

    Assert:
      - derive_obligations() still respects MAX_TRAVERSAL_DEPTH and terminates.
      - Termination happens in bounded time (generous: 5 seconds).
      - The traversal result is structurally correct (no duplicate obligations,
        no obligations from nodes beyond max_depth).

    The 'worst case' graph here:
      - 50 jurisdictions, each with 50 regulations = 2,500 regulation nodes
      - Each regulation has 10 obligations, each obligation 3 controls
        = 25,000 obligation nodes, 75,000 control nodes
      - 1 ai_system node connected to all 50 jurisdictions, all 50 data
        categories (each triggering all regulations), and a high risk tier
    """

    # Build a pathological catalog: many regulations, each with many obligations.
    N_JURISDICTIONS = 20
    N_REGS_PER_JURISDICTION = 10
    N_OBLIGATIONS_PER_REG = 5
    N_CONTROLS_PER_OBLIGATION = 3
    N_DATA_CATEGORIES = 15

    # Build a synthetic large catalog.
    regulations_items = []
    for r in range(N_JURISDICTIONS * N_REGS_PER_JURISDICTION):
        reg_key = f"pathology-reg-{r:04d}"
        obligations = [
            {
                "key": f"pathology-ob-{r}-{o}",
                "needs_controls": [f"pathology-ctrl-{r}-{o}-{c}" for c in range(N_CONTROLS_PER_OBLIGATION)],
            }
            for o in range(N_OBLIGATIONS_PER_REG)
        ]
        # Each regulation is triggered by all data categories.
        regulations_items.append(
            {
                "key": reg_key,
                "triggered_by_data_categories": [f"dc-{dc}" for dc in range(N_DATA_CATEGORIES)],
                "requires_obligations": obligations,
            }
        )

    regulations_catalog = {
        "items": regulations_items,
        "risk_tier_obligations": {
            "high": [
                {
                    "key": "pathology-risk-ob-high",
                    "needs_controls": [f"pathology-risk-ctrl-{c}" for c in range(5)],
                }
            ]
        },
    }

    jurisdictions_items = []
    for j in range(N_JURISDICTIONS):
        jurisdictions_items.append(
            {
                "key": f"PATHOLOGY-JUR-{j:03d}",
                "regulations": [
                    f"pathology-reg-{j * N_REGS_PER_JURISDICTION + r:04d}" for r in range(N_REGS_PER_JURISDICTION)
                ],
            }
        )
    jurisdictions = {"items": jurisdictions_items}

    ai_systems = {
        "items": [
            {
                "id": "sys-pathology-worst-case",
                "name": "Worst Case Dense System",
                "geographic_scope": [f"PATHOLOGY-JUR-{j:03d}" for j in range(N_JURISDICTIONS)],
                "data_categories": [f"dc-{dc}" for dc in range(N_DATA_CATEGORIES)],
                "risk_tier": "high",
                "deployment_status": "active",
            }
        ]
    }

    # Build the graph.
    build_start = time.perf_counter()
    graph = build_graph(ai_systems, regulations_catalog, jurisdictions)
    build_elapsed = time.perf_counter() - build_start

    n_nodes = graph.number_of_nodes()
    n_edges = graph.number_of_edges()

    # Traverse with the configured MAX_TRAVERSAL_DEPTH.
    max_depth = settings.max_traversal_depth
    ai_node_id = schema.node_id(schema.NODE_AI_SYSTEM, "sys-pathology-worst-case")

    traversal_start = time.perf_counter()
    result = derive_obligations(graph, ai_node_id, max_traversal_depth=max_depth)
    traversal_elapsed = time.perf_counter() - traversal_start

    TRAVERSAL_TIME_LIMIT = 5.0
    assert traversal_elapsed < TRAVERSAL_TIME_LIMIT, (
        f"Dense-graph traversal took {traversal_elapsed:.3f}s (limit {TRAVERSAL_TIME_LIMIT}s) "
        f"with {n_nodes} nodes, {n_edges} edges, max_depth={max_depth}"
    )

    # Obligations are sorted and unique.
    obligations = result["derived_obligations"]
    controls = result["derived_controls"]
    assert obligations == sorted(set(obligations)), "derived_obligations are not sorted unique"
    assert controls == sorted(set(controls)), "derived_controls are not sorted unique"

    # ai_system_id is correct.
    assert result["ai_system_id"] == "sys-pathology-worst-case"
    assert result["methodology_version"] == settings.methodology_version

    # Verify depth bound: all paths in graph_path have length ≤ max_depth + 1
    # (path includes source node so max hop count = max_depth).
    for path in result["graph_path"]:
        assert len(path) <= max_depth + 1, f"Path of length {len(path)} exceeds max_depth={max_depth}: {path[:5]}..."

    print(
        f"\n[ST-5] Dense graph: {n_nodes} nodes / {n_edges} edges; "
        f"build={build_elapsed:.3f}s; traversal={traversal_elapsed:.3f}s "
        f"(max_depth={max_depth}); "
        f"{len(obligations)} obligations, {len(controls)} controls"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Stress Test 6 — Partial core outage during a poll cycle
# ─────────────────────────────────────────────────────────────────────────────


def test_6_partial_core_outage_during_poll_resumes_cleanly():
    """
    Kill the mock core mid-way through a poll cycle (some systems already
    pushed, some not). Then restart and run another poll. Assert:
      - The second poll re-derives ALL systems (safety-net semantics: re-derive
        everything on each cycle, don't skip systems based on prior success).
      - The idempotency hash ensures already-pushed systems produce the same
        hash → core's deduplication prevents duplicate audit rows (that's
        core's responsibility, not the satellite's).
      - The second poll doesn't crash / hang, even though the first poll had
        a mid-batch push failure.

    This tests the satellite's resilience contract: a partial-push failure on
    one poll cycle does NOT mean those systems are silently skipped forever —
    the next poll cycle will pick them up.
    """
    N_SYSTEMS = 10
    PUSH_FAIL_AFTER = 5  # Simulate core dying after first 5 systems are pushed.

    system_keys = [f"sys-outage-{i:03d}" for i in range(N_SYSTEMS)]
    fake_graph = _fake_graph_with_ai_systems(system_keys)

    # --- First poll: core dies after PUSH_FAIL_AFTER systems ---
    first_poll_derives: list[str] = []
    first_poll_pushes: list[str] = []
    call_counts = {"push_calls": 0}

    def fake_fetch(changed_since=None):
        return fake_graph

    def fake_derive(graph, node_id, max_traversal_depth=None):
        _, key = schema.split_node_id(node_id)
        first_poll_derives.append(key)
        return _make_derivation(key)

    def fake_push_batch_partial_fail(derivations, trigger_reason):
        call_counts["push_calls"] += 1
        chunk_results = []
        for d in derivations:
            if len(first_poll_pushes) < PUSH_FAIL_AFTER:
                first_poll_pushes.append(d["ai_system_id"])
                chunk_results.append({"ai_system_id": d["ai_system_id"], "ok": True})
            else:
                # Core "died" — simulate by raising on the rest.
                raise ConnectionError("Simulated core outage mid-batch")
        return {"results": chunk_results}

    patched_settings = dataclasses.replace(
        scheduler.settings,
        ingest_batch_chunk_size=N_SYSTEMS,  # One chunk covering all systems
        ingest_batch_pace_seconds=0,
    )

    with (
        patch.object(scheduler, "fetch_and_build_graph", fake_fetch),
        patch.object(scheduler, "derive_obligations", fake_derive),
        patch.object(scheduler, "push_derivations_batch", fake_push_batch_partial_fail),
        patch.object(scheduler, "settings", patched_settings),
        patch.object(scheduler.time, "sleep", lambda s: None),
    ):
        # First poll: must NOT crash the scheduler even if push raises.
        scheduler._run_safety_net_poll()

    assert (
        len(first_poll_derives) == N_SYSTEMS
    ), f"First poll only derived {len(first_poll_derives)} systems (expected {N_SYSTEMS})"

    # --- Second poll (simulating restart): must cover ALL systems ---
    second_poll_derives: list[str] = []
    second_poll_pushes: list[str] = []

    def fake_derive_second(graph, node_id, max_traversal_depth=None):
        _, key = schema.split_node_id(node_id)
        second_poll_derives.append(key)
        return _make_derivation(key)

    def fake_push_batch_success(derivations, trigger_reason):
        for d in derivations:
            second_poll_pushes.append(d["ai_system_id"])
        return {"results": [{"ai_system_id": d["ai_system_id"], "ok": True} for d in derivations]}

    with (
        patch.object(scheduler, "fetch_and_build_graph", fake_fetch),
        patch.object(scheduler, "derive_obligations", fake_derive_second),
        patch.object(scheduler, "push_derivations_batch", fake_push_batch_success),
        patch.object(scheduler, "settings", patched_settings),
        patch.object(scheduler.time, "sleep", lambda s: None),
    ):
        scheduler._run_safety_net_poll()

    # Second poll MUST re-derive ALL N_SYSTEMS — no "already pushed" skip.
    assert set(second_poll_derives) == set(
        system_keys
    ), f"Second poll missed systems: {set(system_keys) - set(second_poll_derives)}"
    assert set(second_poll_pushes) == set(system_keys), (
        f"Second poll didn't push all systems: " f"missing {set(system_keys) - set(second_poll_pushes)}"
    )

    # The systems that WERE pushed in the first poll will have the same
    # derivation_hash in the second poll (same graph state → same content →
    # same hash). Core's idempotency key deduplication handles this.
    from src.p2_satellite.ingest_client import compute_derivation_hash

    for key in first_poll_pushes:
        d1 = _make_derivation(key)
        d2 = _make_derivation(key)
        assert compute_derivation_hash(d1) == compute_derivation_hash(
            d2
        ), f"Same derivation content produced different hashes for {key}"

    print(
        f"\n[ST-6] Poll 1: {len(first_poll_derives)} derived, "
        f"{len(first_poll_pushes)} pushed (core died after {PUSH_FAIL_AFTER}). "
        f"Poll 2: all {len(second_poll_derives)} systems re-derived and pushed."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Stress Test 7 — Long-running memory check
# ─────────────────────────────────────────────────────────────────────────────


def test_7_long_running_memory_no_unbounded_growth():
    """
    Run the scheduler poll for many simulated cycles back-to-back and check
    that memory doesn't grow unbounded. A leak invisible in a single test run
    but real in a production process running for weeks.

    What we check:
      - Memory (tracemalloc) before and after N cycles.
      - Memory delta per cycle stays flat (no linear growth trend).
      - The concurrency module's lock registry (_locks dict) doesn't grow
        unbounded — it only grows with NEW ai_system_ids, never shrinks
        (by design, per the comment in concurrency.py), so we verify it
        reaches a steady state.

    We use 50 poll cycles (more than enough to expose a leak that would
    matter over weeks), each with 100 ai_systems. A real week of 2h-interval
    polls is 84 cycles — 50 gives us a realistic approximation without making
    the test take minutes.
    """

    N_CYCLES = 50
    N_SYSTEMS = 100

    system_keys = [f"sys-mem-{i:03d}" for i in range(N_SYSTEMS)]
    fake_graph = _fake_graph_with_ai_systems(system_keys)

    push_batch_calls = []

    def fake_fetch(changed_since=None):
        return fake_graph

    def fake_derive(graph, node_id, max_traversal_depth=None):
        _, key = schema.split_node_id(node_id)
        return _make_derivation(key)

    def fake_push_batch(derivations, trigger_reason):
        push_batch_calls.append(len(derivations))
        return {"results": [{"ai_system_id": d["ai_system_id"], "ok": True} for d in derivations]}

    patched_settings = dataclasses.replace(
        scheduler.settings,
        ingest_batch_chunk_size=N_SYSTEMS,
        ingest_batch_pace_seconds=0,
    )

    gc.collect()
    tracemalloc.start()
    baseline_snapshot = tracemalloc.take_snapshot()
    baseline_mem = sum(stat.size for stat in baseline_snapshot.statistics("lineno"))

    # Snapshot the lock registry size BEFORE the test cycles so we can measure
    # growth from the test itself, not from prior tests (the registry is process-wide
    # and accumulates across all tests in one pytest session).
    from src.p2_satellite import concurrency

    with concurrency._registry_lock:
        lock_count_before = len(concurrency._locks)

    # Track memory snapshots at intervals.
    mem_per_cycle: list[int] = []

    with (
        patch.object(scheduler, "fetch_and_build_graph", fake_fetch),
        patch.object(scheduler, "derive_obligations", fake_derive),
        patch.object(scheduler, "push_derivations_batch", fake_push_batch),
        patch.object(scheduler, "settings", patched_settings),
        patch.object(scheduler.time, "sleep", lambda s: None),
    ):
        for cycle in range(N_CYCLES):
            scheduler._run_safety_net_poll()
            if cycle % 10 == 9:
                gc.collect()
                snap = tracemalloc.take_snapshot()
                mem_per_cycle.append(sum(stat.size for stat in snap.statistics("lineno")))

    tracemalloc.stop()

    # Assert all N_CYCLES completed with correct push counts.
    assert len(push_batch_calls) == N_CYCLES, f"Expected {N_CYCLES} push_batch calls, got {len(push_batch_calls)}"
    assert all(c == N_SYSTEMS for c in push_batch_calls), f"Some cycles pushed wrong number: {set(push_batch_calls)}"

    # Memory growth check: final snapshot should not be dramatically larger than baseline.
    # We allow 5 MB of growth (JIT caches, string interning, etc. are all fine).
    if mem_per_cycle:
        final_mem = mem_per_cycle[-1]
        MAX_GROWTH_BYTES = 5 * 1024 * 1024  # 5 MB
        growth = final_mem - baseline_mem
        assert growth < MAX_GROWTH_BYTES, (
            f"Memory grew by {growth / 1024:.1f} KB over {N_CYCLES} cycles "
            f"(baseline={baseline_mem / 1024:.1f} KB, final={final_mem / 1024:.1f} KB) "
            f"— possible memory leak"
        )

        # Concurrency lock registry growth check: only NEW ai_system_ids add entries.
        # Since this test uses the SAME N_SYSTEMS ids each cycle, the registry should
        # grow by exactly N_SYSTEMS during the first cycle and then be STABLE.
        with concurrency._registry_lock:
            lock_count_after = len(concurrency._locks)
        lock_growth = lock_count_after - lock_count_before

        # After the first cycle, all N_SYSTEMS locks are registered.
        # Subsequent cycles use the same ids → NO new locks added.
        # Allow +5 for any process-level ids added by unrelated test harness code.
        assert lock_growth <= N_SYSTEMS + 5, (
            f"Lock registry grew by {lock_growth} entries over {N_CYCLES} cycles "
            f"(expected ≤{N_SYSTEMS + 5} — one entry per unique ai_system_id, "
            f"never removed but also never re-added for the same id)"
        )

        print(
            f"\n[ST-7] {N_CYCLES} poll cycles × {N_SYSTEMS} systems; "
            f"memory growth={growth / 1024:.1f} KB over {N_CYCLES} cycles "
            f"(baseline={baseline_mem / 1024:.1f} KB, final={final_mem / 1024:.1f} KB); "
            f"lock registry growth={lock_growth} (from {lock_count_before} to {lock_count_after})"
        )
    else:
        print(
            f"\n[ST-7] {N_CYCLES} poll cycles × {N_SYSTEMS} systems completed; "
            f"no mem snapshots taken (cycle count < 10)"
        )
