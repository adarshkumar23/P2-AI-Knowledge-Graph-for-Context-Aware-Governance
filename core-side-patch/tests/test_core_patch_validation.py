# Patch for CompliVibe core (app.complivibe.in), lands on top of migration head 0175.
# Test-harness only (not part of the patch set shipped to core).
#
# Pure unit tests for validation.py -- no DB, no FastAPI.
from __future__ import annotations

from validation import compare_derivation, validate_obligation_control_ids


def test_validate_obligation_control_ids_all_known():
    catalog = {"obligation": {"gdpr_data_subject_rights"}, "control_type": {"access_control"}}
    payload = {"derived_obligations": ["gdpr_data_subject_rights"], "derived_controls": ["access_control"]}
    assert validate_obligation_control_ids(payload, catalog) == []


def test_validate_obligation_control_ids_rejects_unknown_obligation():
    catalog = {"obligation": {"gdpr_data_subject_rights"}, "control_type": {"access_control"}}
    payload = {"derived_obligations": ["made_up_obligation"], "derived_controls": ["access_control"]}
    assert validate_obligation_control_ids(payload, catalog) == ["made_up_obligation"]


def test_validate_obligation_control_ids_rejects_inactive_control_not_in_catalog():
    # Caller is expected to have already filtered catalog to archived=false rows,
    # so an archived/inactive id simply won't appear in `catalog` -- this test
    # models that: "audit_logging" exists but isn't in the active catalog.
    catalog = {"obligation": set(), "control_type": {"access_control"}}
    payload = {"derived_obligations": [], "derived_controls": ["audit_logging"]}
    assert validate_obligation_control_ids(payload, catalog) == ["audit_logging"]


def test_validate_obligation_control_ids_reports_all_bad_ids():
    catalog = {"obligation": set(), "control_type": set()}
    payload = {"derived_obligations": ["a", "b"], "derived_controls": ["c"]}
    assert validate_obligation_control_ids(payload, catalog) == ["a", "b", "c"]


def test_compare_derivation_exact_match():
    submitted = {"derived_obligations": ["a", "b"], "derived_controls": ["c"]}
    reference = {"derived_obligations": ["b", "a"], "derived_controls": ["c"]}
    assert compare_derivation(submitted, reference) is True


def test_compare_derivation_mismatch_missing_obligation():
    submitted = {"derived_obligations": ["a"], "derived_controls": ["c"]}
    reference = {"derived_obligations": ["a", "b"], "derived_controls": ["c"]}
    assert compare_derivation(submitted, reference) is False


def test_compare_derivation_mismatch_extra_control():
    submitted = {"derived_obligations": ["a"], "derived_controls": ["c", "extra"]}
    reference = {"derived_obligations": ["a"], "derived_controls": ["c"]}
    assert compare_derivation(submitted, reference) is False
