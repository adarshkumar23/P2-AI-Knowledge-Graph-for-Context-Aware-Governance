"""
Patch for CompliVibe core (app.complivibe.in), lands on top of migration head 0175.
Field names / existing-pattern assumptions below are best-effort from PATENT.md and
must be verified against the real ai_system model / AuditService / permission system
before merging -- see core-side-patch/ASSUMPTIONS.md.

Pure, DB-free, FastAPI-free functions implementing the two checks required by
PATENT.md's "Satellites Compute, Core Decides" validation contract:

  1. validate_obligation_control_ids -- reject unknown/inactive obligation or
     control_type ids (contract step 1).
  2. compare_derivation -- exact-match comparison between the satellite's
     submitted derivation and core's independent re-derivation (contract step 2).

Kept dependency-free on purpose so they're trivially unit-testable and so the
route handler (routers/patent_ingest_p2.py) is the only place that has to deal
with DB sessions / HTTP concerns.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence


def validate_obligation_control_ids(
    payload: Mapping[str, Sequence[str]], catalog: Mapping[str, Sequence[str] | set[str]]
) -> list[str]:
    """Return the list of obligation/control ids in `payload` that are NOT present
    in the active `catalog` (i.e. unknown to core, or archived/inactive -- the
    caller is expected to have already filtered `catalog` down to
    archived=false rows per PATENT.md step 1).

    payload: {"derived_obligations": [...ids], "derived_controls": [...ids]}
    catalog: {"obligation": {...active ids}, "control_type": {...active ids}}
    """
    obligation_catalog = set(catalog.get("obligation", ()))
    control_catalog = set(catalog.get("control_type", ()))

    bad_ids: list[str] = []
    for obligation_id in payload.get("derived_obligations", []):
        if obligation_id not in obligation_catalog:
            bad_ids.append(obligation_id)
    for control_id in payload.get("derived_controls", []):
        if control_id not in control_catalog:
            bad_ids.append(control_id)
    return bad_ids


def compare_derivation(submitted: Mapping[str, Sequence[str]], reference_derived: Mapping[str, Sequence[str]]) -> bool:
    """Exact-match comparison (PATENT.md step 2): True iff the submitted
    derived_obligations/derived_controls sets are identical (as sets -- order
    and duplicates don't matter) to core's independently re-derived sets.
    """
    submitted_obligations = set(submitted.get("derived_obligations", []))
    submitted_controls = set(submitted.get("derived_controls", []))
    reference_obligations = set(reference_derived.get("derived_obligations", []))
    reference_controls = set(reference_derived.get("derived_controls", []))

    return submitted_obligations == reference_obligations and submitted_controls == reference_controls
