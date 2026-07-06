"""
Patch for CompliVibe core (app.complivibe.in), lands on top of migration head 0175.
Field names / existing-pattern assumptions below are best-effort from PATENT.md and
must be verified against the real ai_system model / AuditService / permission system
before merging -- see core-side-patch/ASSUMPTIONS.md.

In-process, per-scoped-API-key rate limiter for
POST /api/v1/patent-ingest/p2/obligation-derivation.

Why per-scoped-key, not per-IP: PATENT.md's "Satellite Architecture" section
is explicit that the P2 satellite is agent-push / inbound-only and is the
ONLY caller of this endpoint, always presenting its dedicated
patent_ingest:p2:write scoped key (see dependencies.require_patent_ingest_scope).
There is no meaningful population of distinct IPs to rate-limit against --
what matters is bounding how fast a single (potentially compromised or
buggy-looping) satellite instance can flood core with derivation writes.

Algorithm: fixed-window counter (not token bucket). Chosen for simplicity and
because this endpoint's traffic pattern (event-triggered pushes + a periodic
safety-net poll, per PATENT.md's HYBRID TRIGGER) doesn't need smooth
burst-shaping -- a hard per-window cap is enough to stop a runaway loop. A
token bucket would allow smoother bursts but adds complexity this stopgap
doesn't need; revisit if real satellite traffic patterns turn out to be
bursty enough that a fixed window causes false-positive 429s at window
boundaries.

Default limit: 100 requests / 60-second window per scoped key. This is a
ROUGH STOPGAP DEFAULT, not a tuned production value -- PATENT.md's hybrid
trigger means normal traffic should be far below this (one push per changed
watched field, plus one sweep every SAFETY_NET_POLL_HOURS, default 2 hours,
across the org's ai_system inventory). Tune _DEFAULT_LIMIT /
_DEFAULT_WINDOW_SECONDS once real satellite traffic volume is known -- see
new ASSUMPTIONS.md entry.

*** SINGLE-PROCESS ONLY -- SEE ASSUMPTIONS.md. ***
This limiter's state (`_windows`) lives in this process's memory. It:
  - resets on process restart (a restarted core process forgets in-flight
    windows -- briefly generous, never briefly stingy, which is the safe
    failure direction for a rate limiter)
  - does NOT share state across multiple core replicas/workers -- if core
    runs N replicas behind a load balancer, the *effective* limit is
    approximately N x the configured per-process limit, not the configured
    limit itself, because each replica counts independently.
Real production deployments running more than one core replica MUST replace
this with a shared store (Redis INCR + EXPIRE, or whatever core's real
rate-limiting infra already is) before this bound is trustworthy at scale.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from fastapi import HTTPException, status

# --- Tunable defaults (module-level constants -- no shared settings object
# exists in core-side-patch, see MERGE_NOTES.md) ---------------------------
DEFAULT_LIMIT = 100
DEFAULT_WINDOW_SECONDS = 60.0


class FixedWindowRateLimiter:
    """Per-key fixed-window request counter.

    `limit` requests are allowed per key per rolling `window_seconds` window;
    the (count, window_start) pair for each key resets once `window_seconds`
    has elapsed since that key's window started (i.e. classic fixed-window,
    not sliding-window -- a burst can occur across a window boundary, which is
    an accepted stopgap-level imprecision, see module docstring).
    """

    def __init__(self, limit: int = DEFAULT_LIMIT, window_seconds: float = DEFAULT_WINDOW_SECONDS):
        self.limit = limit
        self.window_seconds = window_seconds
        self._lock = threading.Lock()
        # key -> (window_start_monotonic, count_in_window)
        self._windows: dict[str, tuple[float, int]] = {}

    def allow(self, key: str) -> bool:
        """Return True and record the hit if `key` is still under its limit
        for the current window; return False (without recording) if the
        caller should be rejected."""
        now = time.monotonic()
        with self._lock:
            window_start, count = self._windows.get(key, (now, 0))
            if now - window_start >= self.window_seconds:
                # Window elapsed -- start a fresh one.
                window_start, count = now, 0
            if count >= self.limit:
                # Still record the window_start so we don't keep resetting it
                # on every rejected call within the same window.
                self._windows[key] = (window_start, count)
                return False
            self._windows[key] = (window_start, count + 1)
            return True

    def allow_n(self, key: str, n: int) -> bool:
        """Atomically check-and-record `n` units against `key`'s current
        window in one call (used by the batch ingest route, where a single
        HTTP call represents `n` derivation writes, not one) -- either all
        `n` are admitted or none are (never partially charged)."""
        if n <= 0:
            return True
        now = time.monotonic()
        with self._lock:
            window_start, count = self._windows.get(key, (now, 0))
            if now - window_start >= self.window_seconds:
                window_start, count = now, 0
            if count + n > self.limit:
                self._windows[key] = (window_start, count)
                return False
            self._windows[key] = (window_start, count + n)
            return True

    def _reset_for_tests(self) -> None:
        with self._lock:
            self._windows.clear()


# Module-level singleton shared by the FastAPI dependency below -- mirrors
# mismatch_metrics.MismatchMetrics / audit_service_stub.AuditService's
# class-level shared-state pattern used elsewhere in this patch set.
_ingest_rate_limiter = FixedWindowRateLimiter()


def require_ingest_rate_limit(scoped_key: str) -> None:
    """Raise HTTPException(429) if `scoped_key` has exceeded the configured
    ingest rate limit for the current window; otherwise record the hit and
    return None.

    Intended to be composed with `dependencies.require_patent_ingest_scope()`
    in the route (see routers/patent_ingest_p2.py) -- called with the already
    -validated scoped API key token as `scoped_key`, so the limit is per
    issued key, not per caller IP (see module docstring for why)."""
    if not _ingest_rate_limiter.allow(scoped_key):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="patent_ingest:p2:write rate limit exceeded for this scoped key; retry later",
        )


def require_ingest_rate_limit_n(scoped_key: str, n: int) -> None:
    """Batch variant of require_ingest_rate_limit: charges `n` units (one per
    item in the batch) against `scoped_key`'s window in a single atomic
    check -- all-or-nothing, so a batch is never partially admitted against
    the rate limit. Raises HTTPException(429) if `n` units would exceed the
    remaining budget for the current window."""
    if not _ingest_rate_limiter.allow_n(scoped_key, n):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"patent_ingest:p2:write rate limit would be exceeded by this "
                f"batch of {n} derivations for this scoped key; retry later"
            ),
        )


def _reset_rate_limiter_for_tests() -> None:
    """Test-only helper -- clears the module-level singleton's state between
    tests so one test's hammering doesn't bleed into the next."""
    _ingest_rate_limiter._reset_for_tests()


# ---------------------------------------------------------------------------
# Per-org limiter for POST .../systems/{id}/derive-obligations (Feature 1 of
# the customer-facing knowledge-graph endpoints,
# routers/patent_knowledge_graph_p2.py).
#
# Deliberately a SEPARATE FixedWindowRateLimiter instance/key-space from
# `_ingest_rate_limiter` above, reusing the same class (per the task's "reuse
# the rate-limiting approach from the ingest endpoint" instruction) rather
# than sharing its counters:
#   - keyed by org_id, not a scoped API key -- this endpoint is reached by a
#     normal authenticated human user (or their automation) via
#     dependencies.require_permission, not a satellite scoped key, so there
#     is no per-key token to key off of the way the ingest limiter does.
#   - a MUCH lower default limit: this gates a synchronous, on-demand
#     recursive-CTE traversal triggered by a human clicking a button, not a
#     high-throughput machine-to-machine ingest path. 20/60s is a rough
#     stopgap default (unverified against real traversal latency/graph size
#     in production -- see ASSUMPTIONS.md), not a tuned production value.
# Same single-process/single-replica caveat as _ingest_rate_limiter -- see
# this module's docstring and ASSUMPTIONS.md item 16.
DEFAULT_ON_DEMAND_DERIVE_LIMIT = 20
DEFAULT_ON_DEMAND_DERIVE_WINDOW_SECONDS = 60.0

_on_demand_derive_rate_limiter = FixedWindowRateLimiter(
    limit=DEFAULT_ON_DEMAND_DERIVE_LIMIT, window_seconds=DEFAULT_ON_DEMAND_DERIVE_WINDOW_SECONDS
)


def require_on_demand_derive_rate_limit(org_id: Any) -> None:
    """Raise HTTPException(429) if `org_id` has exceeded the configured
    on-demand-derivation rate limit for the current window; otherwise record
    the hit and return None. Keyed by org_id (stringified) since an on-demand
    traversal is org-scoped work regardless of which user within the org
    triggered it."""
    key = f"org:{org_id}"
    if not _on_demand_derive_rate_limiter.allow(key):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="on-demand obligation derivation rate limit exceeded for this org; retry later",
        )


def _reset_on_demand_derive_rate_limiter_for_tests() -> None:
    """Test-only helper -- see _reset_rate_limiter_for_tests above."""
    _on_demand_derive_rate_limiter._reset_for_tests()
