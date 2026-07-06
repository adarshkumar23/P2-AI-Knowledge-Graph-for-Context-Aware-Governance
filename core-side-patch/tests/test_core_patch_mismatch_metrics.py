# Patch for CompliVibe core (app.complivibe.in), lands on top of migration head 0175.
# Test-harness only (not part of the patch set shipped to core).
#
# Unit tests for mismatch_metrics.MismatchMetrics in isolation (no FastAPI/DB
# involved -- the ingest-router-level integration of this counter is covered
# separately in test_core_patch_ingest_router.py).
from __future__ import annotations

import pytest

from mismatch_metrics import MismatchMetrics


@pytest.fixture(autouse=True)
def _reset_metrics():
    MismatchMetrics._reset_for_tests()
    yield
    MismatchMetrics._reset_for_tests()


def test_mismatch_rate_with_no_records_is_zero():
    assert MismatchMetrics.mismatch_rate() == 0.0
    assert MismatchMetrics.total_recorded() == 0


def test_mismatch_rate_counts_only_flagged_mismatch_status():
    MismatchMetrics.record(org_id=1, validation_status="validated")
    MismatchMetrics.record(org_id=1, validation_status="validated")
    MismatchMetrics.record(org_id=1, validation_status="flagged_mismatch")

    assert MismatchMetrics.total_recorded() == 3
    assert MismatchMetrics.mismatch_rate() == pytest.approx(1 / 3)


def test_mismatch_rate_scoped_to_one_org():
    MismatchMetrics.record(org_id=1, validation_status="flagged_mismatch")
    MismatchMetrics.record(org_id=1, validation_status="flagged_mismatch")
    MismatchMetrics.record(org_id=2, validation_status="validated")
    MismatchMetrics.record(org_id=2, validation_status="validated")

    assert MismatchMetrics.mismatch_rate(org_id=1) == 1.0
    assert MismatchMetrics.mismatch_rate(org_id=2) == 0.0
    assert MismatchMetrics.total_recorded(org_id=1) == 2
    assert MismatchMetrics.total_recorded(org_id=2) == 2
    # Unscoped rate mixes both orgs.
    assert MismatchMetrics.mismatch_rate() == pytest.approx(0.5)


def test_mismatch_rate_respects_recency_window():
    MismatchMetrics.record(org_id=1, validation_status="flagged_mismatch")
    MismatchMetrics.record(org_id=1, validation_status="flagged_mismatch")
    MismatchMetrics.record(org_id=1, validation_status="validated")
    MismatchMetrics.record(org_id=1, validation_status="validated")

    # Only the most recent 2 records (both "validated") should count.
    assert MismatchMetrics.mismatch_rate(window=2) == 0.0
    # All 4 records: 2/4 = 0.5.
    assert MismatchMetrics.mismatch_rate() == pytest.approx(0.5)


def test_validated_ingest_still_counts_toward_denominator():
    MismatchMetrics.record(org_id=1, validation_status="validated")
    assert MismatchMetrics.total_recorded() == 1
    assert MismatchMetrics.mismatch_rate() == 0.0
