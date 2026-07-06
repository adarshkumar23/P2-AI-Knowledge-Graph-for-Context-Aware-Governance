"""
Synthetic mock of the three core export payloads
(GET /api/v1/patent-exports/p2/{ai-systems,regulations-catalog,jurisdictions}).

This is the SHARED contract used by:
  - src/p2_satellite/graph_builder.py (Workstream B) to build the NetworkX graph
  - tests/fixtures/reference_cte.py (ground truth) to build the SQLite rows
  - tests/fixtures/expected_traversal.py (hand-computed expected output)
  - tests/benchmark/eu_india_biometric_case.py (extends this pattern at larger scale)

Two AI systems are modeled:
  sys-alpha: EU-only, personal data, risk_tier=limited  -> baseline GDPR case
  sys-beta:  EU+IN, biometric+health, risk_tier=high    -> the novel joint-
             jurisdiction case the patent claim is about (scaled-down preview
             of tests/benchmark/eu_india_biometric_case.py)

Do not edit the *_KEY values without updating expected_traversal.py and
reference_cte.py to match — all three files must stay in lockstep.
"""

from __future__ import annotations

AI_SYSTEMS_EXPORT = {
    "items": [
        {
            "id": "sys-alpha",
            "name": "Alpha Resume Screener",
            "geographic_scope": ["EU"],
            "data_categories": ["personal"],
            "risk_tier": "limited",
            "deployment_status": "active",
        },
        {
            "id": "sys-beta",
            "name": "Beta Biometric Access Control",
            "geographic_scope": ["EU", "IN"],
            "data_categories": ["biometric", "health"],
            "risk_tier": "high",
            "deployment_status": "active",
        },
    ]
}

REGULATIONS_CATALOG_EXPORT = {
    "items": [
        {
            "key": "GDPR",
            "name": "General Data Protection Regulation",
            "triggered_by_data_categories": ["personal", "health"],
            "requires_obligations": [
                {
                    "key": "gdpr_data_subject_rights",
                    "name": "Data subject rights (access/erasure/portability)",
                    "needs_controls": ["access_control"],
                },
                {
                    "key": "gdpr_breach_notification",
                    "name": "72-hour breach notification",
                    "needs_controls": ["audit_logging"],
                },
            ],
        },
        {
            "key": "EU_AI_ACT",
            "name": "EU AI Act",
            "triggered_by_data_categories": ["biometric"],
            "requires_obligations": [
                {
                    "key": "euaiact_transparency_notice",
                    "name": "Transparency notice to affected persons",
                    "needs_controls": ["transparency_documentation"],
                },
            ],
        },
        {
            "key": "DPDP",
            "name": "Digital Personal Data Protection Act (India)",
            "triggered_by_data_categories": [],
            "requires_obligations": [
                {
                    "key": "dpdp_consent_notice",
                    "name": "Explicit consent notice",
                    "needs_controls": ["consent_management"],
                },
            ],
        },
    ],
    # risk_tier -> additional obligations (edge type risk_tier_adds), separate
    # from a regulation's baseline requires_obligations. Modeling EU AI Act's
    # risk-tiered obligations (conformity assessment / human oversight only
    # apply to high-risk systems) as risk_tier_adds rather than baseline
    # regulation_requires is what lets the graph resolve risk correctly
    # instead of a blanket "EU implies AI Act conformity assessment" rule.
    "risk_tier_obligations": {
        "high": [
            {
                "key": "euaiact_conformity_assessment",
                "name": "Conformity assessment (Annex VI/VII)",
                "needs_controls": ["audit_logging"],
            },
            {
                "key": "euaiact_human_oversight",
                "name": "Human oversight measures",
                "needs_controls": ["access_control"],
            },
        ],
        "limited": [],
        "minimal": [],
        "prohibited": [],
    },
}

JURISDICTIONS_EXPORT = {
    "items": [
        {"key": "EU", "name": "European Union", "regulations": ["GDPR", "EU_AI_ACT"]},
        {"key": "IN", "name": "India", "regulations": ["DPDP"]},
    ]
}
