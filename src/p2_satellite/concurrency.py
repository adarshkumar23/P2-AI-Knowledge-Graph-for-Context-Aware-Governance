"""
Per-ai_system_id processing guard (Workstream D, production-hardening pass).

PROBLEM
-------
Both the event-triggered path (event_listener.process_ai_system_changed,
run via FastAPI BackgroundTasks) and the safety-net poll (scheduler's
_run_safety_net_poll, run on APScheduler's BackgroundScheduler thread) fetch
the graph, derive obligations, and push to core for a given ai_system_id.
Nothing previously prevented both paths from doing this for the SAME
ai_system_id at nearly the same wall-clock moment -- e.g. an event fires for
a system right as the 2-hour sweep reaches it.

DECISION: per-ai_system_id non-blocking lock (not a dedupe-time-window, not
last-write-wins)
------------------------------------------------------------------------
- A dedupe window needs an arbitrary "how many seconds counts as duplicate"
  constant and still leaves a gap: two calls landing further apart than the
  window but still overlapping in flight (a slow fetch+derive+push cycle can
  run for seconds) would not be caught. A lock covers exactly the window that
  actually matters -- "is a push for this id in flight right now" -- with no
  magic constant to tune.
- last-write-wins (i.e. do nothing and let both proceed) was considered and
  rejected: although push_derivation's derivation_hash is purely a function
  of derivation content (see ingest_client.py), so two concurrent pushes that
  happen to compute the same hash are individually idempotency-safe on
  core's side, the two HTTP calls are NOT ordered or awaited against each
  other. That means two in-flight requests can race with different
  Idempotency-Key/derivation_hash values if the underlying graph mutates
  between the two independent fetches (event path fetched a moment before a
  core-side write; poll path fetched a moment after). There is no way to
  argue "no non-idempotent side effect" with confidence in that case, so
  last-write-wins is not an acceptable choice here.
- A lock is simple, requires no cross-process coordination (event listener
  and scheduler run inside the SAME Python process -- the FastAPI lifespan
  in event_listener.py starts scheduler.py's BackgroundScheduler thread
  in-process), and fails toward the safe outcome: the loser just skips this
  cycle and logs it. If it was the event path that lost, the next safety-net
  poll will still catch the system. If it was the poll path that lost for a
  system whose event already fired, no reconciliation was needed anyway --
  the event path already has it in flight.

IMPLEMENTATION
--------------
A dict of `ai_system_id -> threading.Lock`, guarded by one small master lock
for dict access (locks are created lazily, never removed -- at this
codebase's scale, holding one small Lock object per ai_system_id ever seen
for the lifetime of the process is a trivial amount of memory, and removing
entries safely under concurrent access is unnecessary complexity for no
real benefit here).

`try_acquire_ai_system_processing` is a context manager (not a raw
lock-or-None return) that always yields a bool: True if the caller now holds
the per-id lock (and must do the work, releasing on exit), False if another
in-flight call already holds it (caller must skip and log, doing no further
work). This shape lets both call sites use one `with ... as acquired:` block
and branch on `acquired` rather than needing a None-check plus a separate
`with` for the lock object itself.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager

_registry_lock = threading.Lock()
_locks: dict[str, threading.Lock] = {}


def _get_lock(ai_system_id: str) -> threading.Lock:
    with _registry_lock:
        lock = _locks.get(ai_system_id)
        if lock is None:
            lock = threading.Lock()
            _locks[ai_system_id] = lock
        return lock


@contextmanager
def try_acquire_ai_system_processing(ai_system_id: str) -> Iterator[bool]:
    """Non-blocking per-ai_system_id guard.

    Usage:
        with try_acquire_ai_system_processing(ai_system_id) as acquired:
            if not acquired:
                # another in-flight call (event path or safety-net poll)
                # already holds this id -- skip, log, and move on.
                return  # or `continue` in a loop
            ... do the fetch/derive/push work ...

    Never blocks: if the lock for `ai_system_id` is already held, yields
    False immediately rather than waiting. Always releases the lock on exit
    if it was acquired here.
    """
    lock = _get_lock(ai_system_id)
    acquired = lock.acquire(blocking=False)
    try:
        yield acquired
    finally:
        if acquired:
            lock.release()
