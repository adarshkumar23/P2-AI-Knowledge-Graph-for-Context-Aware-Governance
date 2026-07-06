"""
Hand-computed expected traversal results for tests/fixtures/sample_export.py.

Derived obligations/controls are the union across ALL paths from the
ai_system node (the reference CTE and the NetworkX traversal both return
every reachable ('obligation'|'control_type') node, deduplicated by
node_key — see PATENT.md's reference CTE final SELECT). Order is not
significant; both implementations sort before comparing.
"""

from __future__ import annotations

EXPECTED = {
    "sys-alpha": {
        # EU only, personal data, risk_tier=limited -> baseline GDPR only,
        # no EU AI Act obligations trigger (no biometric data, risk not high).
        "derived_obligations": sorted(
            [
                "gdpr_data_subject_rights",
                "gdpr_breach_notification",
                "euaiact_transparency_notice",  # baseline, applies to any EU AI system
            ]
        ),
        "derived_controls": sorted(
            [
                "access_control",
                "audit_logging",
                "transparency_documentation",
            ]
        ),
    },
    "sys-beta": {
        # EU+IN, biometric+health, risk_tier=high -> GDPR + EU AI Act
        # (baseline + high-risk additions) + DPDP. This is the novel
        # combination a static single-jurisdiction lookup table cannot
        # anticipate; see tests/benchmark/eu_india_biometric_case.py.
        "derived_obligations": sorted(
            [
                "gdpr_data_subject_rights",
                "gdpr_breach_notification",
                "euaiact_transparency_notice",
                "euaiact_conformity_assessment",
                "euaiact_human_oversight",
                "dpdp_consent_notice",
            ]
        ),
        "derived_controls": sorted(
            [
                "access_control",
                "audit_logging",
                "transparency_documentation",
                "consent_management",
            ]
        ),
    },
}

# All obligations above are reachable within this many hops from the
# ai_system node in this fixture graph — used to test that
# MAX_TRAVERSAL_DEPTH is genuinely load-bearing (a shallower cap truncates
# results) rather than a magic number that happens to never bind.
MIN_DEPTH_FOR_FULL_RESULT = 4
