"""
Unit tests for src/p2_satellite/concurrency.py -- the per-ai_system_id
processing guard shared by event_listener.process_ai_system_changed and
scheduler._run_safety_net_poll (see concurrency.py's module docstring for the
lock-vs-dedupe-window-vs-last-write-wins design decision).
"""

from __future__ import annotations

import threading

from src.p2_satellite.concurrency import try_acquire_ai_system_processing


def test_second_call_for_same_id_is_denied_while_first_still_in_flight():
    with try_acquire_ai_system_processing("sys-race") as first_acquired:
        assert first_acquired is True

        # Second attempt for the SAME ai_system_id, while the first guard is
        # still held open (not yet exited) -- simulates the event path and
        # the safety-net poll racing on the same id.
        with try_acquire_ai_system_processing("sys-race") as second_acquired:
            assert second_acquired is False


def test_lock_is_released_on_exit_and_can_be_reacquired():
    with try_acquire_ai_system_processing("sys-release") as acquired:
        assert acquired is True

    # Guard released on __exit__ -- a subsequent call must succeed again.
    with try_acquire_ai_system_processing("sys-release") as acquired_again:
        assert acquired_again is True


def test_different_ai_system_ids_do_not_contend():
    with try_acquire_ai_system_processing("sys-a") as a:
        assert a is True
        with try_acquire_ai_system_processing("sys-b") as b:
            assert b is True


def test_lock_released_even_if_body_raises():
    try:
        with try_acquire_ai_system_processing("sys-error") as acquired:
            assert acquired is True
            raise RuntimeError("boom")
    except RuntimeError:
        pass

    with try_acquire_ai_system_processing("sys-error") as acquired_again:
        assert acquired_again is True


def test_concurrent_threads_only_one_wins():
    """Two real OS threads racing try_acquire_ai_system_processing for the
    same id -- exactly one must win. The second thread only attempts its
    acquire after confirming the first thread already holds the lock (via an
    Event), so the ordering is deterministic rather than relying on sleep-
    based timing races."""
    results: dict[str, bool] = {}
    first_holds_lock = threading.Event()
    release_first = threading.Event()

    def first_worker():
        with try_acquire_ai_system_processing("sys-thread-race") as acquired:
            results["first"] = acquired
            first_holds_lock.set()
            release_first.wait(timeout=2.0)

    def second_worker():
        assert first_holds_lock.wait(timeout=2.0)
        with try_acquire_ai_system_processing("sys-thread-race") as acquired:
            results["second"] = acquired

    t1 = threading.Thread(target=first_worker)
    t2 = threading.Thread(target=second_worker)
    t1.start()
    t2.start()
    t2.join(timeout=3.0)
    release_first.set()
    t1.join(timeout=3.0)

    assert results == {"first": True, "second": False}
