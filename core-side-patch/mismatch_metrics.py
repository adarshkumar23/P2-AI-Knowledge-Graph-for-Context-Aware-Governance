"""
Patch for CompliVibe core (app.complivibe.in), lands on top of migration head 0175.
Field names / existing-pattern assumptions below are best-effort from PATENT.md and
must be verified against the real ai_system model / AuditService / permission system
before merging -- see core-side-patch/ASSUMPTIONS.md.

In-process counter for the validation-mismatch rate -- arguably the single
most important operability number for trusting PATENT.md's "Satellites
Compute, Core Decides" contract in production: if the satellite's independent
NetworkX traversal and core's reference CTE re-derivation disagree often,
that's either a systematic bug (config drift, e.g. MAX_TRAVERSAL_DEPTH out of
sync between the two sides -- see MERGE_NOTES.md section 5) or a sign the
validation step is doing real work catching genuine edge cases. Either way, a
human needs to be able to see the rate, not just know that individual flagged
rows exist somewhere in governance_graph_traversal_results.

*** THIS IS A STAND-IN, NOT A PRODUCTION METRICS PIPELINE. ***
This module is an in-memory, single-process counter -- it exists so the
*pattern* ("record every ingest outcome, expose a queryable mismatch rate") is
testable in this repo, which has no access to whatever real metrics backend
core actually runs (Prometheus, Datadog, CloudWatch, etc. -- unknown, see
ASSUMPTIONS.md). Before this is production-ready, a human MUST:
  - replace/augment `MismatchMetrics.record()` with an emission to core's real
    metrics backend (e.g. a Prometheus Counter with org_id/validation_status
    labels, or whatever core already uses elsewhere in its 297 tables), AND
  - wire an alert rule on the resulting mismatch rate (e.g. "page if
    flagged_mismatch rate over the last N ingests exceeds X%"), per the
    rollback-plan guidance in MERGE_NOTES.md section 5.
This module resets on process restart and does not aggregate across multiple
core replicas -- same single-process caveat as rate_limiter.py, see
ASSUMPTIONS.md for the corresponding new entry.
"""

from __future__ import annotations

import threading
from typing import Any, ClassVar


class MismatchMetrics:
    """Thread-safe, in-process record of every ingest's (org_id, validation_status)
    outcome, plus a simple count-based mismatch rate query.

    Deliberately mirrors audit_service_stub.AuditService's class-level,
    test-visible list pattern (`_written` there, `_records` here) so tests can
    assert on it directly without a real metrics client.
    """

    _lock: ClassVar[threading.Lock] = threading.Lock()
    _records: ClassVar[list[tuple[Any, str]]] = []

    @classmethod
    def record(cls, org_id: Any, validation_status: str) -> None:
        """Record one ingest outcome. Called on EVERY ingest -- validated or
        flagged_mismatch -- so the rate's denominator (total ingests) is
        correct, not just the mismatch numerator."""
        with cls._lock:
            cls._records.append((org_id, validation_status))

    @classmethod
    def mismatch_rate(cls, org_id: Any | None = None, window: int | None = None) -> float:
        """Fraction of recorded ingests whose validation_status was
        "flagged_mismatch".

        org_id: if given, scope the rate to just that org's recorded ingests.
        window: if given, only consider the most recent `window` matching
            records (a simple recency window, not a time-based one -- this
            counter doesn't store timestamps, see module docstring on why
            that's a stand-in, not a real metrics pipeline).

        Returns 0.0 if there are no matching records (nothing recorded yet is
        not the same claim as "0% mismatch rate" in a real system -- callers
        should treat an empty denominator as "no data" rather than "healthy").
        """
        with cls._lock:
            records = list(cls._records)

        if org_id is not None:
            records = [r for r in records if r[0] == org_id]
        if window is not None:
            records = records[-window:]

        if not records:
            return 0.0

        mismatches = sum(1 for _, status in records if status == "flagged_mismatch")
        return mismatches / len(records)

    @classmethod
    def total_recorded(cls, org_id: Any | None = None) -> int:
        """Convenience helper: total number of recorded ingests (denominator),
        optionally scoped to one org."""
        with cls._lock:
            records = list(cls._records)
        if org_id is not None:
            records = [r for r in records if r[0] == org_id]
        return len(records)

    @classmethod
    def _reset_for_tests(cls) -> None:
        with cls._lock:
            cls._records.clear()
