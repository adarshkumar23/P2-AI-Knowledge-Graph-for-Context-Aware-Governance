"""
Unit tests for src/p2_satellite/scheduler.py (Workstream D) -- the
safety-net reconciliation poll (NOT the primary event-triggered path, and
NOT "real-time" -- see PATENT.md CHANGE LOG).

Covers:
  - start_scheduler() registers a job whose interval matches
    settings.safety_net_poll_hours (read once, not hardcoded).
  - _run_safety_net_poll() (the job body) is called directly here rather
    than waiting hours for APScheduler to fire it -- it calls
    fetch_and_build_graph / derive_obligations (mocked) for every ai_system
    node in a fake graph, then pushes them all in ONE
    push_derivations_batch call (mocked) tagged trigger_reason="scheduled".
"""

from __future__ import annotations

from datetime import timedelta

import networkx as nx
import pytest

from src.p2_satellite import scheduler, schema
from src.p2_satellite.config import settings


@pytest.fixture(autouse=True)
def _cleanup_scheduler():
    """Ensure no real BackgroundScheduler instance leaks between tests."""
    yield
    scheduler.stop_scheduler()


@pytest.fixture(autouse=True)
def _mock_push_graph_structure(monkeypatch):
    """_run_safety_net_poll also pushes graph structure now (item 22 --
    see src/p2_satellite/scheduler.py and
    core-side-patch/ASSUMPTIONS.md item 22). Mocked out here, autouse, so
    every test in this push_derivations_batch-focused file stays free of
    real (and here, always-failing) network calls and the tenacity retry
    delay that comes with them. Dedicated coverage for push_graph_structure
    itself lives in tests/unit/test_graph_structure_push.py."""
    monkeypatch.setattr(scheduler, "push_graph_structure", lambda structure: {"nodes_created": 0, "edges_created": 0})


def _fake_graph_with_ai_systems(keys):
    g = nx.DiGraph()
    for key in keys:
        nid = schema.node_id(schema.NODE_AI_SYSTEM, key)
        g.add_node(nid, node_type=schema.NODE_AI_SYSTEM, node_key=key)
    # A non-ai_system node, to prove the job filters correctly.
    reg_id = schema.node_id(schema.NODE_REGULATION, "GDPR")
    g.add_node(reg_id, node_type=schema.NODE_REGULATION, node_key="GDPR")
    return g


# --------------------------------------------------------------------------
# start_scheduler() -- interval configuration
# --------------------------------------------------------------------------


def test_start_scheduler_registers_job_with_configured_interval():
    sched = scheduler.start_scheduler()
    try:
        job = sched.get_job(scheduler.JOB_ID)
        assert job is not None
        assert job.trigger.interval == timedelta(hours=settings.safety_net_poll_hours)
    finally:
        scheduler.stop_scheduler()


def test_start_scheduler_is_idempotent():
    sched1 = scheduler.start_scheduler()
    sched2 = scheduler.start_scheduler()
    assert sched1 is sched2
    scheduler.stop_scheduler()


def test_stop_scheduler_allows_restart():
    scheduler.start_scheduler()
    scheduler.stop_scheduler()
    sched = scheduler.start_scheduler()
    assert sched.running
    scheduler.stop_scheduler()


# --------------------------------------------------------------------------
# _run_safety_net_poll() -- job body, called directly (no real sleeping)
# --------------------------------------------------------------------------


def test_run_safety_net_poll_pushes_every_ai_system_with_scheduled_reason(monkeypatch):
    fake_graph = _fake_graph_with_ai_systems(["sys-alpha", "sys-beta", "sys-gamma"])
    calls = {"fetch": 0, "derive": [], "push_batch": []}

    def fake_fetch_and_build_graph(changed_since=None):
        calls["fetch"] += 1
        assert changed_since is None
        return fake_graph

    def fake_derive_obligations(graph, node_id, max_traversal_depth=None):
        calls["derive"].append(node_id)
        _, key = schema.split_node_id(node_id)
        return {
            "ai_system_id": key,
            "derived_obligations": ["some_obligation"],
            "derived_controls": ["some_control"],
            "graph_path": [],
            "methodology_version": settings.methodology_version,
        }

    def fake_push_derivations_batch(derivations, trigger_reason):
        calls["push_batch"].append((tuple(d["ai_system_id"] for d in derivations), trigger_reason))
        return {"results": [{"ai_system_id": d["ai_system_id"], "ok": True} for d in derivations]}

    monkeypatch.setattr(scheduler, "fetch_and_build_graph", fake_fetch_and_build_graph)
    monkeypatch.setattr(scheduler, "derive_obligations", fake_derive_obligations)
    monkeypatch.setattr(scheduler, "push_derivations_batch", fake_push_derivations_batch)

    scheduler._run_safety_net_poll()

    assert calls["fetch"] == 1
    assert set(calls["derive"]) == {
        schema.node_id(schema.NODE_AI_SYSTEM, "sys-alpha"),
        schema.node_id(schema.NODE_AI_SYSTEM, "sys-beta"),
        schema.node_id(schema.NODE_AI_SYSTEM, "sys-gamma"),
    }
    # Exactly one batch push call, covering all three systems, tagged scheduled.
    assert len(calls["push_batch"]) == 1
    pushed_ids, trigger_reason = calls["push_batch"][0]
    assert set(pushed_ids) == {"sys-alpha", "sys-beta", "sys-gamma"}
    assert trigger_reason == "scheduled"
    # Non-ai_system nodes (e.g. the GDPR regulation node) must never be
    # passed to derive_obligations as a traversal root.
    assert schema.node_id(schema.NODE_REGULATION, "GDPR") not in calls["derive"]


def test_run_safety_net_poll_continues_after_one_item_rejected(monkeypatch):
    fake_graph = _fake_graph_with_ai_systems(["sys-alpha", "sys-beta"])

    def fake_fetch_and_build_graph(changed_since=None):
        return fake_graph

    def fake_derive_obligations(graph, node_id, max_traversal_depth=None):
        _, key = schema.split_node_id(node_id)
        return {
            "ai_system_id": key,
            "derived_obligations": [],
            "derived_controls": [],
            "graph_path": [],
            "methodology_version": settings.methodology_version,
        }

    def fake_push_derivations_batch(derivations, trigger_reason):
        # Core rejects sys-alpha's item but still processes sys-beta's --
        # this is the "one bad item doesn't fail the batch" contract.
        return {
            "results": [
                {"ai_system_id": "sys-alpha", "ok": False, "error": {"status_code": 422}},
                {"ai_system_id": "sys-beta", "ok": True, "result": {"validation_status": "validated"}},
            ]
        }

    monkeypatch.setattr(scheduler, "fetch_and_build_graph", fake_fetch_and_build_graph)
    monkeypatch.setattr(scheduler, "derive_obligations", fake_derive_obligations)
    monkeypatch.setattr(scheduler, "push_derivations_batch", fake_push_derivations_batch)

    # Should not raise -- one item's rejection must not abort processing of
    # the rest of the poll's results.
    scheduler._run_safety_net_poll()


def test_run_safety_net_poll_continues_after_batch_push_exception(monkeypatch):
    """If the WHOLE batch push raises (e.g. connection exhausted all
    retries), the poll must log and return rather than propagate/crash the
    scheduler thread."""
    fake_graph = _fake_graph_with_ai_systems(["sys-alpha"])

    def fake_fetch_and_build_graph(changed_since=None):
        return fake_graph

    def fake_derive_obligations(graph, node_id, max_traversal_depth=None):
        _, key = schema.split_node_id(node_id)
        return {
            "ai_system_id": key,
            "derived_obligations": [],
            "derived_controls": [],
            "graph_path": [],
            "methodology_version": settings.methodology_version,
        }

    def fake_push_derivations_batch(derivations, trigger_reason):
        raise RuntimeError("simulated total ingest failure")

    monkeypatch.setattr(scheduler, "fetch_and_build_graph", fake_fetch_and_build_graph)
    monkeypatch.setattr(scheduler, "derive_obligations", fake_derive_obligations)
    monkeypatch.setattr(scheduler, "push_derivations_batch", fake_push_derivations_batch)

    # Must not raise.
    scheduler._run_safety_net_poll()


def test_run_safety_net_poll_chunks_large_sweeps_and_paces_between_chunks(monkeypatch):
    """A sweep larger than settings.ingest_batch_chunk_size must be split
    into multiple push_derivations_batch calls (never one giant burst that
    would instantly exceed core's per-window ingest rate limit -- see
    scheduler.py's docstring and config.py's ingest_batch_chunk_size /
    ingest_batch_pace_seconds), with a pacing sleep between chunks (but not
    after the last one)."""
    import dataclasses

    fake_graph = _fake_graph_with_ai_systems([f"sys-{i}" for i in range(5)])
    calls = {"push_batch": [], "sleep": []}

    def fake_fetch_and_build_graph(changed_since=None):
        return fake_graph

    def fake_derive_obligations(graph, node_id, max_traversal_depth=None):
        _, key = schema.split_node_id(node_id)
        return {
            "ai_system_id": key,
            "derived_obligations": [],
            "derived_controls": [],
            "graph_path": [],
            "methodology_version": settings.methodology_version,
        }

    def fake_push_derivations_batch(derivations, trigger_reason):
        calls["push_batch"].append([d["ai_system_id"] for d in derivations])
        return {"results": [{"ai_system_id": d["ai_system_id"], "ok": True} for d in derivations]}

    def fake_sleep(seconds):
        calls["sleep"].append(seconds)

    # Chunk size of 2 over 5 systems -> chunks of [2, 2, 1].
    small_chunk_settings = dataclasses.replace(
        scheduler.settings, ingest_batch_chunk_size=2, ingest_batch_pace_seconds=7.5
    )
    monkeypatch.setattr(scheduler, "settings", small_chunk_settings)
    monkeypatch.setattr(scheduler, "fetch_and_build_graph", fake_fetch_and_build_graph)
    monkeypatch.setattr(scheduler, "derive_obligations", fake_derive_obligations)
    monkeypatch.setattr(scheduler, "push_derivations_batch", fake_push_derivations_batch)
    monkeypatch.setattr(scheduler.time, "sleep", fake_sleep)

    scheduler._run_safety_net_poll()

    assert [len(c) for c in calls["push_batch"]] == [2, 2, 1]
    assert set().union(*calls["push_batch"]) == {f"sys-{i}" for i in range(5)}
    # Paced between chunks (2 gaps for 3 chunks), never after the last chunk.
    assert calls["sleep"] == [7.5, 7.5]
