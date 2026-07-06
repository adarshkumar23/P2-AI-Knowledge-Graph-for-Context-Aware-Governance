"""
Safety-net reconciliation poll (Workstream D) -- the SECONDARY half of the
hybrid trigger described in PATENT.md "Satellite Architecture" -> HYBRID
TRIGGER (b).

This module exists ONLY to catch ai_system changes whose event notification
was missed or whose event-triggered processing (event_listener.py) failed.
It is independent of, and complementary to, the event-triggered path -- it
is NOT the primary derivation mechanism and must never be described as
"real-time" or "polling-based real-time sync" (see PATENT.md CHANGE LOG,
which locks this language decision).

Scheduler choice: BackgroundScheduler (thread-based), not AsyncIOScheduler.
event_listener.py's FastAPI app is served by an ASGI worker whose single
event loop must stay free to handle HTTP requests; running the poll job on
its own OS thread via BackgroundScheduler keeps a slow/blocking traversal-
and-push cycle for many ai_systems from ever stalling that event loop, and
keeps this module trivially unit-testable in a plain synchronous test
(call `_run_safety_net_poll()` directly -- no event loop required).

The poll interval (settings.safety_net_poll_hours) is read exactly ONCE,
inside start_scheduler(), when the job is registered -- never hardcoded
elsewhere and never re-read per tick.
"""

from __future__ import annotations

import logging
import time
from contextlib import ExitStack

from apscheduler.schedulers.background import BackgroundScheduler

from src.p2_satellite import metrics, schema
from src.p2_satellite.concurrency import try_acquire_ai_system_processing
from src.p2_satellite.config import settings

# TODO(integration): confirm exact signature once graph_builder.py / traversal.py land
from src.p2_satellite.graph_builder import fetch_and_build_graph, serialize_graph_structure
from src.p2_satellite.ingest_client import push_derivations_batch, push_graph_structure
from src.p2_satellite.observability import get_logger, log_event, timed_stage
from src.p2_satellite.traversal import derive_obligations

logger = get_logger(__name__)

JOB_ID = "p2_safety_net_poll"

_scheduler: BackgroundScheduler | None = None


def _run_safety_net_poll() -> None:
    """Job body: re-derive obligations for every ai_system node currently in
    the graph and push them to core in CHUNKED batched HTTP calls (via
    ingest_client.push_derivations_batch) rather than one HTTP round-trip per
    ai_system -- at realistic scale (thousands of ai_systems), one-push-per-
    system every SAFETY_NET_POLL_HOURS is needlessly chatty. See PATENT.md's
    performance-at-scale hardening pass.

    Chunked, not one giant batch: core's ingest rate limiter charges a
    batch's FULL size in one atomic check (see
    core-side-patch/rate_limiter.py), so an unchunked burst of thousands of
    derivations in a single call would instantly exceed a reasonably-sized
    per-window limit. Chunks of at most settings.ingest_batch_chunk_size are
    sent settings.ingest_batch_pace_seconds apart -- this is a background
    reconciliation job with hours between runs (SAFETY_NET_POLL_HOURS), so
    trading a slower sweep for never tripping core's flood protection is the
    right default. Both values must be coordinated with core's real rate
    limit before large-fleet go-live -- see config.py's docstring on these
    two fields and MERGE_CHECKLIST.md.

    Safe to run repeatedly / redundantly with the event-triggered path: each
    derivation keeps its own content-derived idempotency hash, so re-pushing
    a derivation that hasn't actually changed since the last event-triggered
    push does not duplicate audit rows on the core side (see
    ingest_client.py docstring).

    Each ai_system_id is processed through the same
    concurrency.try_acquire_ai_system_processing guard used by
    event_listener.process_ai_system_changed -- crucially, the lock for each
    included ai_system_id is held (via the ExitStack below) for the ENTIRE
    batch, from derive() through the batch push actually completing, not just
    while deriving. Releasing it right after deriving (before the shared
    batch push lands) would reopen exactly the race window this guard exists
    to close: an event-triggered push for the same id could interleave
    between this poll's derive and its (delayed, batched) push. See
    concurrency.py's module docstring for the guard's design rationale.
    """
    graph = fetch_and_build_graph(changed_since=None)

    # Same "push the whole freshly-built structure, let core upsert
    # idempotently" approach as event_listener.process_ai_system_changed --
    # see that function's comment and core-side-patch/ASSUMPTIONS.md item 22.
    # The safety-net poll is the natural place to keep this fresh even if no
    # single watched-field event fired recently (e.g. a brand new
    # regulation/jurisdiction added upstream with no ai_system field change
    # to trigger an event push).
    try:
        push_graph_structure(serialize_graph_structure(graph))
        metrics.INGEST_PUSH_TOTAL.labels(push_kind="graph_structure", outcome="success").inc()
    except Exception:
        metrics.INGEST_PUSH_TOTAL.labels(push_kind="graph_structure", outcome="failure").inc()
        log_event(logger, logging.ERROR, "safety_net_poll.graph_structure_push_failed", exc_info=True)

    ai_system_nodes = [
        (node_id, node_data)
        for node_id, node_data in graph.nodes(data=True)
        if node_data.get("node_type") == schema.NODE_AI_SYSTEM
    ]

    with timed_stage(logger, "safety_net_poll", ai_system_count=len(ai_system_nodes)):
        skipped = 0
        derivations: list[dict] = []

        with ExitStack() as locks:
            for node_id, _node_data in ai_system_nodes:
                _, ai_system_key = schema.split_node_id(node_id)

                acquired = locks.enter_context(try_acquire_ai_system_processing(ai_system_key))
                if not acquired:
                    skipped += 1
                    log_event(
                        logger,
                        logging.WARNING,
                        "scheduled_derivation.skipped_in_flight",
                        ai_system_id=ai_system_key,
                        trigger_reason="scheduled",
                    )
                    continue

                metrics.TRAVERSAL_TOTAL.labels(trigger_reason="scheduled").inc()
                with metrics.TRAVERSAL_DURATION_SECONDS.labels(trigger_reason="scheduled").time():
                    derivations.append(derive_obligations(graph, node_id))

            processed = 0
            chunk_size = max(1, settings.ingest_batch_chunk_size)
            chunks = [derivations[i : i + chunk_size] for i in range(0, len(derivations), chunk_size)]

            for chunk_index, chunk in enumerate(chunks):
                try:
                    response = push_derivations_batch(chunk, trigger_reason="scheduled")
                    metrics.INGEST_PUSH_TOTAL.labels(push_kind="derivation_batch", outcome="success").inc()
                    results = response.get("results", [])
                    processed += sum(1 for r in results if r.get("ok"))
                    for r in results:
                        if r.get("ok"):
                            metrics.record_validation_status(r.get("result", {}).get("validation_status"))
                    failed = [r for r in results if not r.get("ok")]
                    for r in failed:
                        log_event(
                            logger,
                            logging.ERROR,
                            "scheduled_derivation.item_rejected",
                            ai_system_id=r.get("ai_system_id"),
                            error=r.get("error"),
                        )
                except Exception:
                    metrics.INGEST_PUSH_TOTAL.labels(push_kind="derivation_batch", outcome="failure").inc()
                    log_event(
                        logger,
                        logging.ERROR,
                        "safety_net_poll.push_derivations_batch_failed",
                        exc_info=True,
                        chunk_size=len(chunk),
                        trigger_reason="scheduled",
                    )

                # Pace between chunks (not after the last one) so consecutive
                # bursts don't stack up against core's rate-limit window.
                if chunk_index < len(chunks) - 1 and settings.ingest_batch_pace_seconds > 0:
                    time.sleep(settings.ingest_batch_pace_seconds)
            # `locks` (and therefore every ai_system's processing lock) stays
            # held until this `with` block exits here, AFTER every chunk's
            # push has been attempted -- not right after deriving.

        log_event(
            logger,
            logging.INFO,
            "safety_net_poll.summary",
            ai_system_count=len(ai_system_nodes),
            processed=processed,
            skipped=skipped,
        )


def start_scheduler() -> BackgroundScheduler:
    """Start the safety-net reconciliation poll. Call once at app startup
    (event_listener.py does this from its FastAPI startup event).

    Idempotent: calling this again while a scheduler is already running just
    returns the existing instance rather than starting a second one.
    """
    global _scheduler

    if _scheduler is not None and _scheduler.running:
        return _scheduler

    # The ONE place settings.safety_net_poll_hours is read to configure the
    # schedule -- never hardcoded, never re-read elsewhere.
    poll_hours = settings.safety_net_poll_hours

    scheduler = BackgroundScheduler()
    scheduler.add_job(
        _run_safety_net_poll,
        trigger="interval",
        hours=poll_hours,
        id=JOB_ID,
        replace_existing=True,
    )
    scheduler.start()

    _scheduler = scheduler
    return scheduler


def stop_scheduler() -> None:
    """Clean shutdown -- call from event_listener.py's FastAPI shutdown event."""
    global _scheduler

    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
