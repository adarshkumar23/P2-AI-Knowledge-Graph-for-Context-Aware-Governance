"""
Naive static lookup table -- representative of what PATENT.md's "The
Problem It Solves" describes every existing GRC platform doing:

    "if the AI system is deployed in the EU and processes health data,
     apply GDPR + EU AI Act. This works for combinations the platform
     developers anticipated. It fails for novel combinations..."

This module is NOT a strawman. It is a faithful, small, realistic
implementation of exactly that pattern: a Python dict keyed on
(first-listed jurisdiction) -> (first-listed data category), built by a
developer who enumerated the SINGLE-jurisdiction, SINGLE-purpose
combinations they anticipated at the time (EU+health -> GDPR baseline,
EU+employment_data -> GDPR baseline, EU+biometric -> EU AI Act baseline,
IN+anything -> DPDP baseline). It has no concept of:
  - multiple jurisdictions active on the same system at once,
  - multiple data categories/purposes active on the same system at once,
  - risk-tier-driven additional obligations (conformity assessment, human
    oversight, biometric accuracy testing -- these only ever get attached
    by a real risk classification step, which this table doesn't have),
  - a controller/processor role split (the table returns bare obligation
    keys with no role dimension at all).

HOW IT FAILS ON THE BENCHMARK CASE (see fixtures.py):
sys-globalid-biometric has geographic_scope=["EU","IN"] and
data_categories=["biometric","employment_data","health"]. Real GRC
lookup-table code of this shape almost universally keys off "the primary
jurisdiction" and "the primary data category/purpose" fields on the system
record (exactly the fields a single-purpose intake form would have asked
for), so it picks jurisdiction=geographic_scope[0]="EU" and
data_category=data_categories[0]="biometric". Looking that up returns only
the EU AI Act's baseline transparency obligation -- ONE obligation out of
the eleven that are actually owed. It silently drops:
  - all of GDPR (the second jurisdiction-relevant regulation for the EU
    leg -- because the table's "EU" bucket is keyed by data category, and
    "biometric" was the only category consulted),
  - all of the EU AI Act's high-risk additions (conformity assessment,
    human oversight, biometric accuracy/bias testing -- because this
    table has no risk-tier reasoning at all),
  - all of DPDP (the India leg is dropped entirely because "EU" was
    picked as the -- singular -- jurisdiction),
  - the entire controller/processor role split (the table doesn't
    represent roles at all).

This is the "incomplete subset" failure mode PATENT.md anticipates ("They
either give you nothing or give you everything"), not the "returns
nothing" failure mode -- which is arguably worse, because an incomplete-
but-nonempty result is more likely to be mistaken for a complete one by a
downstream consumer that doesn't already know the right answer.
"""

from __future__ import annotations

from typing import Any

# Keyed exactly the way PATENT.md's own example is phrased: jurisdiction
# first, then data category. Built to cover only the single-jurisdiction /
# single-data-category combinations its author anticipated.
STATIC_LOOKUP_TABLE: dict[str, dict[str, list[str]]] = {
    "EU": {
        # "if EU and health data, apply GDPR" (+ this table's author folded
        # the two baseline GDPR obligations in directly, since that's the
        # anticipated case this bucket was written for).
        "health": ["gdpr_data_subject_rights", "gdpr_breach_notification"],
        "employment_data": ["gdpr_data_subject_rights", "gdpr_breach_notification"],
        # "if EU and biometric data, apply EU AI Act" -- baseline
        # transparency only; this table has no risk-tier concept, so the
        # high-risk additions (conformity assessment / human oversight /
        # biometric accuracy testing) never appear here under any key.
        "biometric": ["euaiact_transparency_notice"],
    },
    "IN": {
        # The author special-cased India as "any data category -> DPDP
        # baseline", since at the time this table was written every Indian
        # deployment the platform had seen was single-purpose.
        "any": ["dpdp_consent_notice", "dpdp_data_principal_rights"],
    },
}


def naive_static_lookup(ai_system: dict[str, Any]) -> list[str]:
    """Return the obligation keys this naive table produces for one
    ai_system export item (same shape as fixtures.AI_SYSTEMS_EXPORT
    items / tests/fixtures/sample_export.py items).

    Mirrors real lookup-table code: it reads exactly one jurisdiction and
    exactly one data category off the system record -- the fields a
    single-purpose intake form would have populated -- and does not
    attempt to combine multiple entries in either list, and does not
    reason about risk_tier or controller/processor role at all.
    """
    geographic_scope = ai_system.get("geographic_scope") or []
    data_categories = ai_system.get("data_categories") or []

    if not geographic_scope:
        return []
    # BUG (by design, representative of real platforms): only the
    # first-listed jurisdiction is consulted. A jointly-deployed system
    # spanning ["EU", "IN"] is treated as if it were EU-only.
    jurisdiction = geographic_scope[0]

    bucket = STATIC_LOOKUP_TABLE.get(jurisdiction)
    if bucket is None:
        return []

    if "any" in bucket:
        return sorted(bucket["any"])

    if not data_categories:
        return []
    # BUG (by design): only the first-listed data category/purpose is
    # consulted. A dual-purpose system (biometric + employment_data +
    # health all active) is treated as if only its first category mattered.
    data_category = data_categories[0]

    return sorted(bucket.get(data_category, []))
