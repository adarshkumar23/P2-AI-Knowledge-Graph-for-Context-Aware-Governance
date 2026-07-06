"""
Benchmark-scale export dataset for Workstream E (patent evidence).

This is a LARGER, richer sibling of tests/fixtures/sample_export.py, built in
the exact same three-dict shape (ai-systems / regulations-catalog /
jurisdictions) documented there, so that src/p2_satellite/graph_builder.py's
build_graph() consumes it UNMODIFIED. We deliberately do not reuse
sample_export.py's tiny sys-beta system: that fixture is scoped for a fast
unit test, not for carrying a defensible, independently-verifiable
controller/processor + dual-purpose + dual-regulation-role narrative at the
rigor level PATENT.md's "Required Evidence Before Filing" section demands.

--------------------------------------------------------------------------
THE BENCHMARK CASE (see PATENT.md "The Problem It Solves" and
"Required Evidence Before Filing")
--------------------------------------------------------------------------
One AI system: "sys-globalid-biometric" — a biometric identity-verification
platform (facial-recognition-based matching) operated by a single company
across a JOINT EU-India data center. It is used for two distinct purposes
simultaneously:
  1. Employment screening — verifying candidate/employee identity
     (data_category: employment_data, plus biometric).
  2. Healthcare patient identity verification at a partnered clinic
     (data_category: health, plus biometric).

Because the deployment is joint (a single shared processing pipeline
spanning both jurisdictions, per PATENT.md's literal example: "a biometric
AI system deployed in a joint EU-India data center processing data for both
jurisdictions"), the company holds BOTH a controller role and a processor
role concurrently:
  - EU leg: the company is the PII CONTROLLER. It is the employer running
    its own candidate-screening program and the clinic operator determining
    purposes and means of the health-data processing for its own EU
    patients/employees.
  - India leg: the company is a PII PROCESSOR. The joint data center also
    performs biometric verification as a contracted service on behalf of an
    Indian client (a partner hospital / employer group) who is the data
    fiduciary/controller for the Indian data subjects; the company only
    processes on that client's documented instructions. Because the
    pipeline is joint/shared, EU data subjects' biometric templates also
    transit the India-based processing leg, which is precisely why GDPR's
    extraterritorial processor obligations (Art. 28) attach to the India
    leg in addition to India's own DPDP processor obligations.

This is exactly the class of input PATENT.md says a static, single-
jurisdiction/single-purpose lookup table cannot resolve: it is not "EU +
health" or "India + biometric" in isolation, it is a SIMULTANEOUS union of
jurisdictions, purposes, and controller/processor roles.

--------------------------------------------------------------------------
CONTROLLER/PROCESSOR ROLE ENCODING — DESIGN DECISION (read this)
--------------------------------------------------------------------------
PATENT.md declares exactly seven node types and eight edge types (see
src/p2_satellite/schema.py) and we do not invent new ones. We also do not
modify graph_builder.py or traversal.py. Given those constraints, the role
split is carried two ways, deliberately kept separate so each does honest
work:

1. DOCUMENTATION / AUDIT FIDELITY: the ai_system item below carries a
   `properties` dict (`controller_processor_role_by_jurisdiction`,
   `purposes`, `deployment_model`) mirroring the `properties JSONB` column
   PATENT.md specifies on `governance_graph_nodes`. NOTE: the frozen
   build_graph() in src/p2_satellite/graph_builder.py does not currently
   read `properties` off an ai-system export item into the graph (it only
   reads id/data_categories/geographic_scope/risk_tier) — this is a real,
   observed gap we flag rather than silently work around (see
   PATENT_TECHNICAL_EFFECT.md "Known limitation"). The properties dict is
   still included here because it is part of the export contract's shape
   and is what a human/auditor reviewing this system's record would use to
   understand *why* both roles' obligations legitimately apply — it is not
   itself what drives obligation selection in this benchmark.

2. OBLIGATION-LEVEL ROLE TAGGING (what actually drives the demonstrable
   result): each obligation dict in the regulations catalog below carries a
   `properties: {"applies_to_role": "controller" | "processor" | "joint"}`
   tag. GDPR is modeled with FOUR obligations: two joint/baseline
   (data-subject rights, breach notification), one controller-specific
   (accountability/records-of-processing/DPIA ownership — GDPR Art. 5(2),
   24, 30, 35), and one processor-specific (data-processing-agreement /
   process-only-on-instructions — GDPR Art. 28). DPDP similarly carries a
   processor-specific obligation (India's DPDP Act separately distinguishes
   "Data Fiduciary" from "Data Processor", Section 8(2)) alongside two
   joint ones. All obligations under a regulation are reachable from that
   regulation's node via `regulation_requires` edges (this is the existing,
   unmodified graph_builder.py behavior — a regulation's obligations are
   NOT individually gated by which specific data_category/edge triggered
   reachability to the regulation node). That is precisely the behavior we
   want to demonstrate: because sys-globalid-biometric legitimately holds
   BOTH roles, the correct, complete answer for this specific system
   includes both the controller-tagged AND processor-tagged obligations —
   graph traversal surfaces the full, correctly-labeled set, and
   PATENT_TECHNICAL_EFFECT.md verifies each tag by hand against this file.

--------------------------------------------------------------------------
WHY EU_AI_ACT IS HIGH-RISK HERE
--------------------------------------------------------------------------
risk_tier="high" is set on the ai_system because biometric identification of
natural persons for identity verification is an EU AI Act Annex III /
Article 6 high-risk use case (remote biometric identification systems are
explicitly called out). That risk_tier drives three additional obligations
via the risk_tier_adds edge (conformity assessment, human oversight,
biometric accuracy/bias testing), on top of EU_AI_ACT's own baseline
transparency obligation.

--------------------------------------------------------------------------
WHY EU_AI_ACT IS REACHABLE ONLY VIA data_triggers, NOT jurisdiction_has
--------------------------------------------------------------------------
Unlike tests/fixtures/sample_export.py (which, for unit-test simplicity,
lists EU_AI_ACT directly under EU's jurisdiction_has regulations), this
benchmark deliberately does NOT put EU_AI_ACT under EU's jurisdiction_has
list. EU AI Act applicability is driven by the AI system's use
case/risk profile (biometric identification), not blanket EU deployment —
so it is modeled as reachable ONLY through the `biometric` data_category's
data_triggers edge. This makes the "biometric" dimension load-bearing in
the graph (removing it would remove EU_AI_ACT reachability entirely), which
is exactly the dimension a lookup table keyed on a single (jurisdiction,
data_category) pair struggles to combine correctly with the other two
dimensions (dual jurisdiction, dual purpose) at once.
"""

from __future__ import annotations

AI_SYSTEM_KEY = "sys-globalid-biometric"

AI_SYSTEMS_EXPORT = {
    "items": [
        {
            "id": AI_SYSTEM_KEY,
            "name": "GlobalID Biometric Identity Verification Platform",
            # Dual jurisdiction: a single joint EU-India deployment, not two
            # independent single-country systems.
            "geographic_scope": ["EU", "IN"],
            # Dual purpose, both active at once: biometric matching serves
            # both employment screening (employment_data) and healthcare
            # patient identification (health).
            "data_categories": ["biometric", "employment_data", "health"],
            # Biometric identification of natural persons -> EU AI Act
            # Annex III high-risk use case.
            "risk_tier": "high",
            "deployment_status": "active",
            # See "CONTROLLER/PROCESSOR ROLE ENCODING" above: carried for
            # audit/documentation fidelity; not consumed by the frozen
            # build_graph() implementation.
            "properties": {
                "deployment_model": "joint_eu_india_data_center",
                "purposes": [
                    "employment_screening",
                    "healthcare_patient_identification",
                ],
                "controller_processor_role_by_jurisdiction": {
                    "EU": "controller",
                    "IN": "processor",
                },
            },
        }
    ]
}

REGULATIONS_CATALOG_EXPORT = {
    "items": [
        {
            "key": "GDPR",
            "name": "General Data Protection Regulation",
            # Reachable both via EU's jurisdiction_has (below) and via these
            # data categories -- multiple independent trigger routes, all
            # converging on the same regulation node, which is itself part
            # of what a single-key lookup table cannot represent.
            "triggered_by_data_categories": ["employment_data", "health"],
            "requires_obligations": [
                {
                    "key": "gdpr_data_subject_rights",
                    "name": "Data subject rights (access/erasure/portability)",
                    "needs_controls": ["access_control"],
                    "properties": {"applies_to_role": "joint"},
                },
                {
                    "key": "gdpr_breach_notification",
                    "name": "72-hour breach notification",
                    "needs_controls": ["audit_logging"],
                    "properties": {"applies_to_role": "joint"},
                },
                {
                    "key": "gdpr_controller_accountability",
                    "name": (
                        "Controller accountability: records of processing "
                        "(Art. 30), DPIA ownership (Art. 35), and "
                        "demonstrable compliance (Art. 5(2), 24)"
                    ),
                    "needs_controls": ["records_of_processing_control"],
                    "properties": {"applies_to_role": "controller"},
                },
                {
                    "key": "gdpr_processor_data_processing_agreement",
                    "name": (
                        "Processor obligations: written Art. 28 data "
                        "processing agreement, process only on documented "
                        "controller instructions, assist controller with "
                        "data subject requests and breach notification"
                    ),
                    "needs_controls": ["data_processing_agreement_control"],
                    "properties": {"applies_to_role": "processor"},
                },
            ],
        },
        {
            "key": "EU_AI_ACT",
            "name": "EU AI Act",
            # See module docstring: reachable ONLY via biometric, not via
            # jurisdiction_has, so this dimension is load-bearing.
            "triggered_by_data_categories": ["biometric"],
            "requires_obligations": [
                {
                    "key": "euaiact_transparency_notice",
                    "name": "Transparency notice to affected persons",
                    "needs_controls": ["transparency_documentation"],
                    "properties": {"applies_to_role": "joint"},
                },
            ],
        },
        {
            "key": "DPDP",
            "name": "Digital Personal Data Protection Act, 2023 (India)",
            # Jurisdiction-only trigger, matching the established
            # sample_export.py pattern: DPDP's applicability is not gated by
            # a specific data_category, only by processing digital personal
            # data of an Indian data principal.
            "triggered_by_data_categories": [],
            "requires_obligations": [
                {
                    "key": "dpdp_consent_notice",
                    "name": "Explicit, itemized consent notice",
                    "needs_controls": ["consent_management"],
                    "properties": {"applies_to_role": "joint"},
                },
                {
                    "key": "dpdp_data_principal_rights",
                    "name": ("Data Principal rights: access, correction, " "erasure, and grievance redressal"),
                    "needs_controls": ["access_control"],
                    "properties": {"applies_to_role": "joint"},
                },
                {
                    "key": "dpdp_processor_contractual_terms",
                    "name": (
                        "Data Processor obligations (DPDP Act Sec. 8(2)): "
                        "process personal data only pursuant to a valid "
                        "contract with the Data Fiduciary"
                    ),
                    "needs_controls": ["data_processing_agreement_control"],
                    "properties": {"applies_to_role": "processor"},
                },
            ],
        },
    ],
    # risk_tier -> additional obligations (edge type risk_tier_adds),
    # separate from a regulation's baseline requires_obligations. Triggered
    # by ai_system.risk_tier == "high", which this system is classified as
    # because it performs biometric identification of natural persons (see
    # module docstring).
    "risk_tier_obligations": {
        "high": [
            {
                "key": "euaiact_conformity_assessment",
                "name": "Conformity assessment (Annex VI/VII)",
                "needs_controls": ["audit_logging"],
                "properties": {"applies_to_role": "joint"},
            },
            {
                "key": "euaiact_human_oversight",
                "name": "Human oversight measures (Art. 14)",
                "needs_controls": ["access_control"],
                "properties": {"applies_to_role": "joint"},
            },
            {
                "key": "euaiact_biometric_accuracy_and_bias_testing",
                "name": (
                    "Accuracy, robustness, and non-discrimination testing " "for biometric identification (Art. 15)"
                ),
                "needs_controls": ["bias_and_accuracy_testing"],
                "properties": {"applies_to_role": "joint"},
            },
        ],
        "limited": [],
        "minimal": [],
        "prohibited": [],
    },
}

JURISDICTIONS_EXPORT = {
    "items": [
        # NOTE: EU_AI_ACT deliberately NOT listed here -- see module
        # docstring "WHY EU_AI_ACT IS REACHABLE ONLY VIA data_triggers".
        {"key": "EU", "name": "European Union", "regulations": ["GDPR"]},
        {"key": "IN", "name": "India", "regulations": ["DPDP"]},
    ]
}


# --------------------------------------------------------------------------
# Hand-computed expected COMPLETE obligation/control set for
# sys-globalid-biometric, independently verifiable by reading the catalog
# above. This is what graph traversal (build_graph + derive_obligations)
# must return in full, and what naive_static_lookup.py must fail to return
# in full. Cross-checked again at runtime in eu_india_biometric_case.py
# (we do not just assert against this constant blindly -- see that file's
# "sanity: hand-computed set matches what the regulations catalog encodes"
# test).
# --------------------------------------------------------------------------
EXPECTED_COMPLETE_OBLIGATIONS = sorted(
    [
        # GDPR (EU) -- joint + controller + processor
        "gdpr_data_subject_rights",
        "gdpr_breach_notification",
        "gdpr_controller_accountability",
        "gdpr_processor_data_processing_agreement",
        # EU AI Act (EU, biometric-triggered) -- baseline + high-risk additions
        "euaiact_transparency_notice",
        "euaiact_conformity_assessment",
        "euaiact_human_oversight",
        "euaiact_biometric_accuracy_and_bias_testing",
        # DPDP (India) -- joint + processor
        "dpdp_consent_notice",
        "dpdp_data_principal_rights",
        "dpdp_processor_contractual_terms",
    ]
)

EXPECTED_COMPLETE_CONTROLS = sorted(
    [
        "access_control",
        "audit_logging",
        "records_of_processing_control",
        "data_processing_agreement_control",
        "transparency_documentation",
        "bias_and_accuracy_testing",
        "consent_management",
    ]
)

# Obligations tagged as controller-specific / processor-specific, used to
# assert the graph result demonstrably carries the role split (not just
# "a big undifferentiated bag of strings").
EXPECTED_CONTROLLER_OBLIGATIONS = sorted(["gdpr_controller_accountability"])
EXPECTED_PROCESSOR_OBLIGATIONS = sorted(
    [
        "gdpr_processor_data_processing_agreement",
        "dpdp_processor_contractual_terms",
    ]
)

# Regulation coverage the complete set must span -- used to assert "all
# three regulations" rather than just counting obligation strings.
EXPECTED_OBLIGATIONS_BY_REGULATION = {
    "GDPR": sorted(
        [
            "gdpr_data_subject_rights",
            "gdpr_breach_notification",
            "gdpr_controller_accountability",
            "gdpr_processor_data_processing_agreement",
        ]
    ),
    "EU_AI_ACT": sorted(
        [
            "euaiact_transparency_notice",
            "euaiact_conformity_assessment",
            "euaiact_human_oversight",
            "euaiact_biometric_accuracy_and_bias_testing",
        ]
    ),
    "DPDP": sorted(
        [
            "dpdp_consent_notice",
            "dpdp_data_principal_rights",
            "dpdp_processor_contractual_terms",
        ]
    ),
}
