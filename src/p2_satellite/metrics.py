"""
Prometheus metrics for the P2 satellite (prometheus-client, Apache-2.0).

Exposed via `GET /metrics` on event_listener.py's FastAPI app. This is what
makes the validation-mismatch rate (core-side-patch/mismatch_metrics.py's
in-process counter, flagged in an earlier hardening pass as needing real
monitoring, not just log lines) actually scrapeable/alertable from a real
Prometheus/Grafana stack, from the satellite side of the same signal --
every ingest push's response already tells the satellite whether core
validated or flagged it, so there's no reason this observability should
exist only on core's side.

*** SINGLE-PROCESS ONLY, same caveat as core-side-patch's in-process
counters (mismatch_metrics.py item 15 / rate_limiter.py item 16 in
ASSUMPTIONS.md): this uses prometheus_client's default in-process registry,
not the multiprocess mode. If the satellite is ever run as more than one
worker process behind a shared /metrics scrape target, switch to
prometheus_client's multiprocess mode (`PROMETHEUS_MULTIPROC_DIR` +
`multiprocess.MultiProcessCollector`) -- out of scope here since this
satellite currently runs as a single uvicorn worker (see README.md "How the
satellite deploys"). ***
"""

from __future__ import annotations

from collections.abc import Callable

from prometheus_client import Counter, Gauge, Histogram

TRAVERSAL_TOTAL = Counter(
    "p2_traversal_total",
    "Total number of obligation-derivation traversals run by the satellite, by trigger_reason.",
    labelnames=("trigger_reason",),
)

TRAVERSAL_DURATION_SECONDS = Histogram(
    "p2_traversal_duration_seconds",
    "Wall-clock duration of one traversal.derive_obligations() call, by trigger_reason.",
    labelnames=("trigger_reason",),
)

# Labels intentionally mirror core-side-patch's validation_status values
# ("validated", "flagged_mismatch") exactly -- see
# core-side-patch/mismatch_metrics.py and routers/patent_ingest_p2.py. This
# is the satellite-observable half of the SAME signal (read back from core's
# ingest response), not a second, independently-defined metric.
VALIDATION_MISMATCH_TOTAL = Counter(
    "p2_validation_mismatch_total",
    "Ingest outcomes by validation_status, as reported back in core's ingest response.",
    labelnames=("validation_status",),
)

INGEST_PUSH_TOTAL = Counter(
    "p2_ingest_push_total",
    "Ingest HTTP pushes to core, by push kind (derivation/derivation_batch/graph_structure) and outcome.",
    labelnames=("push_kind", "outcome"),
)

REPLAY_CACHE_SIZE = Gauge(
    "p2_replay_cache_size",
    "Current number of entries in the event webhook's replay-protection cache (event_listener._seen_signatures).",
)


def set_replay_cache_size_source(source: Callable[[], int]) -> None:
    """Wire REPLAY_CACHE_SIZE to a live callable (e.g. `lambda:
    len(_seen_signatures)`) via prometheus_client's `set_function`, so the
    gauge is always current AT SCRAPE TIME without needing an explicit
    `.set(...)` call at every insert/evict site in event_listener.py's
    `_is_first_use` -- one wiring call here instead of instrumenting every
    mutation point.
    """
    REPLAY_CACHE_SIZE.set_function(source)


def record_validation_status(validation_status: str | None) -> None:
    """Record one ingest outcome's validation_status, if present. A push
    that raised before getting a response (TransientIngestError /
    PermanentIngestError) has no validation_status to record here --
    INGEST_PUSH_TOTAL's outcome="failure" already covers that case."""
    if validation_status:
        VALIDATION_MISMATCH_TOTAL.labels(validation_status=validation_status).inc()
