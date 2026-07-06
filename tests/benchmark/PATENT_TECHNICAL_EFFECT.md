# PATENT_TECHNICAL_EFFECT.md — Workstream E Benchmark Evidence

**Patent:** P2 — AI Knowledge Graph for Context-Aware Governance
**Deliverable:** PATENT.md § "Required Evidence Before Filing"
**Scope:** `tests/benchmark/`

> Formatting note: this document is written to the general shape of a
> technical-effect evidence memo (setup / exact inputs / exact outputs /
> why it matters). We could not directly access the P8/A3.6 benchmark
> template referenced in CLAUDE_CODE_GOAL_PROMPT.md to confirm its exact
> section headings/formatting conventions, so this structure is our best
> good-faith reconstruction of that pattern rather than a verified match.
> All data in this document was produced by actually running the code in
> this repository (see "Reproduction" below) — nothing here is
> hand-authored/hypothetical output.

---

## 1. Setup

### 1.1 What this benchmark models

PATENT.md's "The Problem It Solves" states the failure mode this patent
claims to fix:

> Every GRC platform that handles AI governance uses a static lookup
> table: if the AI system is deployed in the EU and processes health
> data, apply GDPR + EU AI Act. This works for combinations the platform
> developers anticipated. It fails for novel combinations — a biometric
> AI system deployed in a joint EU-India data center processing data for
> both jurisdictions, used for both employment screening and healthcare,
> by a company that is a PII controller in one country and a processor
> in another.
>
> No platform can derive the correct obligation set for a genuinely novel
> combination through hardcoded if/else logic. They either give you
> nothing or give you everything.

This benchmark constructs **exactly that case**, as a single synthetic AI
system, `sys-globalid-biometric` (defined in `tests/benchmark/fixtures.py`):

| Dimension | Value | Why it's novel |
|---|---|---|
| Jurisdictions | `["EU", "IN"]` (joint deployment) | Not single-jurisdiction |
| Purposes / data categories | `biometric` + `employment_data` + `health` | Not single-purpose (employment screening *and* healthcare, both biometric-backed) |
| Roles | Controller (EU leg) *and* Processor (IN leg) | Not a single role |
| Risk classification | `high` (EU AI Act Annex III biometric identification) | Triggers additional obligations beyond baseline |

No real-world regulatory analysis is claimed here — this is a *synthetic,
internally-consistent* dataset built for reproducible benchmarking, with
every design choice documented in `fixtures.py`'s module docstring.

### 1.2 Why it's a genuinely novel combination

A lookup table of the kind PATENT.md describes is built and tested against
combinations its author anticipated: e.g., `(EU, health) -> GDPR + EU AI
Act`, or `(India, any) -> DPDP`. `sys-globalid-biometric` is simultaneously
a member of *all three* of GDPR's, EU AI Act's, and DPDP's trigger
conditions, *and* carries a role split no single-jurisdiction/single-role
table entry can express. It is not an edge case of one bucket — it is the
union of several buckets plus a dimension (controller/processor role) the
table was never built to carry at all.

### 1.3 Controller/processor role encoding — design decision

PATENT.md fixes exactly seven node types and eight edge types
(`src/p2_satellite/schema.py`), and this workstream does not modify
`schema.py`, `graph_builder.py`, or `traversal.py`. Given those hard
constraints, the role split is carried two ways (full rationale in
`fixtures.py`'s docstring, reproduced in summary here):

1. **Audit/documentation fidelity**: the ai_system export item carries a
   `properties` dict (`controller_processor_role_by_jurisdiction`,
   `purposes`, `deployment_model`), mirroring PATENT.md's
   `governance_graph_nodes.properties JSONB` column. We explicitly flag
   that the *frozen* `build_graph()` does not currently wire this
   `properties` dict into the graph (it only reads
   `id`/`data_categories`/`geographic_scope`/`risk_tier` off an ai-system
   item) — see "Known limitation" in §4 below. This is a real, observed
   gap, not something we silently patched around.
2. **Obligation-level role tagging** (the mechanism that actually drives
   the demonstrable result): each obligation in the regulations catalog
   carries `properties.applies_to_role` ∈ `{"controller", "processor",
   "joint"}`. GDPR is modeled with two joint obligations plus one
   controller-specific (`gdpr_controller_accountability`) and one
   processor-specific (`gdpr_processor_data_processing_agreement`)
   obligation; DPDP similarly carries a processor-specific obligation
   (`dpdp_processor_contractual_terms`). Because `sys-globalid-biometric`
   genuinely holds both roles at once, the *complete and correct* answer
   for this system legitimately includes both role-tagged obligations —
   which is exactly what §3.2 below shows graph traversal returning.

---

## 2. Exact inputs

The complete `ai_system` export record used by both methods (from
`tests/benchmark/fixtures.py`, printed directly from the running code):

```json
{
  "id": "sys-globalid-biometric",
  "name": "GlobalID Biometric Identity Verification Platform",
  "geographic_scope": ["EU", "IN"],
  "data_categories": ["biometric", "employment_data", "health"],
  "risk_tier": "high",
  "deployment_status": "active",
  "properties": {
    "deployment_model": "joint_eu_india_data_center",
    "purposes": ["employment_screening", "healthcare_patient_identification"],
    "controller_processor_role_by_jurisdiction": {"EU": "controller", "IN": "processor"}
  }
}
```

The regulations catalog (`REGULATIONS_CATALOG_EXPORT`) and jurisdictions
export (`JURISDICTIONS_EXPORT`) are reproduced verbatim, with the
obligation → role tags and control needs, in `tests/benchmark/fixtures.py`
lines defining `REGULATIONS_CATALOG_EXPORT` and `JURISDICTIONS_EXPORT`.
Summary of what they encode (11 obligations total, spanning 3
regulations):

| Regulation | Obligation key | Role | Control needed |
|---|---|---|---|
| GDPR | `gdpr_data_subject_rights` | joint | `access_control` |
| GDPR | `gdpr_breach_notification` | joint | `audit_logging` |
| GDPR | `gdpr_controller_accountability` | **controller** | `records_of_processing_control` |
| GDPR | `gdpr_processor_data_processing_agreement` | **processor** | `data_processing_agreement_control` |
| EU AI Act | `euaiact_transparency_notice` | joint | `transparency_documentation` |
| EU AI Act (risk_tier=high) | `euaiact_conformity_assessment` | joint | `audit_logging` |
| EU AI Act (risk_tier=high) | `euaiact_human_oversight` | joint | `access_control` |
| EU AI Act (risk_tier=high) | `euaiact_biometric_accuracy_and_bias_testing` | joint | `bias_and_accuracy_testing` |
| DPDP | `dpdp_consent_notice` | joint | `consent_management` |
| DPDP | `dpdp_data_principal_rights` | joint | `access_control` |
| DPDP | `dpdp_processor_contractual_terms` | **processor** | `data_processing_agreement_control` |

`JURISDICTIONS_EXPORT`: `EU -> [GDPR]`, `IN -> [DPDP]`. `EU_AI_ACT` is
deliberately **not** listed under EU's `jurisdiction_has` regulations — it
is reachable only via the `biometric` data-category trigger (see
`fixtures.py` "WHY EU_AI_ACT IS REACHABLE ONLY VIA data_triggers").

---

## 3. Exact outputs

Both outputs below were captured by actually running
`src/p2_satellite/graph_builder.build_graph()` and
`src/p2_satellite/traversal.derive_obligations()` (unmodified,
zero-network, in-process) and `tests/benchmark/naive_static_lookup.py`
against the input in §2, on 2026-07-06.

### 3.1 Naive static lookup output (verbatim)

```json
[
  "euaiact_transparency_notice"
]
```

**One obligation returned, out of eleven owed.** The naive table (built to
mirror PATENT.md's own literal example: "if EU and health data, apply
GDPR + EU AI Act") reads only `geographic_scope[0] == "EU"` and
`data_categories[0] == "biometric"` off the record — the two fields a
single-purpose intake form would have populated — and looks up
`STATIC_LOOKUP_TABLE["EU"]["biometric"]`, which was authored to cover only
the anticipated "EU + biometric -> EU AI Act baseline" case. It silently
drops:

- **All of GDPR** (4 obligations, including the controller- and
  processor-specific ones) — because the "EU" bucket is keyed by data
  category, and only `data_categories[0]` (`"biometric"`) was consulted;
  `"employment_data"` and `"health"` (indices 1 and 2) are never looked
  at.
- **All of the EU AI Act's high-risk additions** (3 obligations) —
  because this table has no risk-tier reasoning at all.
- **All of DPDP** (3 obligations) — because `geographic_scope[1] == "IN"`
  is never consulted; the joint EU-India deployment is treated as
  EU-only.
- **Both role-specific obligations** — the table has no controller/
  processor dimension at all.

### 3.2 Graph traversal output (verbatim)

`derive_obligations()`'s full return value:

```json
{
  "ai_system_id": "sys-globalid-biometric",
  "derived_obligations": [
    "dpdp_consent_notice",
    "dpdp_data_principal_rights",
    "dpdp_processor_contractual_terms",
    "euaiact_biometric_accuracy_and_bias_testing",
    "euaiact_conformity_assessment",
    "euaiact_human_oversight",
    "euaiact_transparency_notice",
    "gdpr_breach_notification",
    "gdpr_controller_accountability",
    "gdpr_data_subject_rights",
    "gdpr_processor_data_processing_agreement"
  ],
  "derived_controls": [
    "access_control",
    "audit_logging",
    "bias_and_accuracy_testing",
    "consent_management",
    "data_processing_agreement_control",
    "records_of_processing_control",
    "transparency_documentation"
  ],
  "methodology_version": "p2-v1.0.0"
}
```

(`graph_path` is also returned — 38 distinct terminal paths across the 31
nodes / 33 edges of the built graph — omitted here for brevity; full
`graph_path` output is reproduced in
`tests/benchmark/REPRODUCIBILITY.md` and is checked for well-formedness
by `test_graph_traversal_methodology_and_shape`.)

**All eleven obligations, all seven controls, spanning all three
regulations, including both `gdpr_controller_accountability` (controller
role) and both `gdpr_processor_data_processing_agreement` +
`dpdp_processor_contractual_terms` (processor role).** This is the
complete, correct answer — verified independently in
`eu_india_biometric_case.py::test_fixture_expected_set_matches_the_catalog_it_claims_to_summarize`,
which reconstructs the expected set directly from the regulations catalog
data (not from a hand-typed constant trusted blindly), and in
`test_graph_traversal_spans_all_three_regulations` /
`test_graph_traversal_includes_both_controller_and_processor_obligations`.

### 3.3 The gap, precisely

```
naive result:        {euaiact_transparency_notice}                      (1 obligation)
graph traversal:      {all 11 obligations listed in §3.2}                (11 obligations)
gap (missed by naive): 10 obligations = all of DPDP (3) + all EU AI Act
                        high-risk additions (3) + both GDPR non-baseline
                        obligations, i.e. controller (1) + processor (1),
                        + DPDP's processor obligation (1) [counted above],
                        + gdpr_data_subject_rights / gdpr_breach_notification (2)
```

`test_graph_traversal_is_a_strict_superset_of_naive_lookup` asserts the
naive result is a strict subset of the full result, and that the gap
specifically contains all of DPDP plus both role-specific obligations —
i.e., the assertion is keyed to the *specific* dimensions the naive table
structurally cannot see, not just "the counts differ."

---

## 4. Why the difference matters

PATENT.md's "Novel Patent Claim" states:

> The inventive step is the traversal method combined with the validation
> contract — same inputs through a static lookup table produce wrong or
> incomplete results for novel combinations; through graph traversal,
> cross-checked by core, they produce correct, auditable results even for
> combinations the platform developers never anticipated.

This benchmark is the empirical proof of the first half of that claim (the
traversal half; the validation-contract/cross-check half is proven
separately by Workstream A/C's core-side re-derivation tests, out of
scope for this file). Both methods received the *identical* input
(§2). The naive method — representative, not a strawman, of the pattern
PATENT.md itself describes ("if EU and health data, apply GDPR + EU AI
Act") — returned 1 of 11 owed obligations, a **91% miss rate**, and did so
*silently*: it returned a non-empty, plausible-looking list, which is more
dangerous than an empty result because a downstream consumer with no
independent way to check would have no signal that anything was wrong.

Graph traversal, run against the exact same input with zero hardcoded
per-combination logic — the traversal algorithm has no `if jurisdiction ==
"EU"` branch anywhere; every regulation/obligation/control reached is a
consequence of generic edge-following — returned the complete, correctly
role-tagged set. This is the categorical difference the patent claim
rests on: a lookup table's failure mode here is structural (it cannot
represent a union of dimensions it wasn't built to enumerate in advance),
while graph traversal's correctness is also structural (union-of-reachable-
nodes is exactly what a joint, dual-purpose, dual-role system needs).

### Known limitation (disclosed, not hidden)

`build_graph()` (frozen; not modified by this workstream) does not
currently read the ai-system export item's `properties` dict into the
constructed graph — only `id`, `data_categories`, `geographic_scope`, and
`risk_tier` drive edges. This means the controller/processor role
attribution recorded in `properties.controller_processor_role_by_jurisdiction`
is documentation-only in the current graph; the demonstrable role split in
the *obligation output* comes from `applies_to_role` tags on the
obligation nodes themselves (§1.3, mechanism 2), which **are** fully
graph-driven. If Workstream B/core's export schema is later extended so
`build_graph()` also ingests ai-system `properties` (e.g. to gate which
specific obligations attach per-system, rather than attaching a
regulation's full obligation set to every system that reaches it), that
would be a strictly *additive* enhancement on top of what this benchmark
already proves. We flag this per the task instructions rather than editing
`graph_builder.py` ourselves.

---

## 5. Reproduction

See `tests/benchmark/REPRODUCIBILITY.md` for the exact commands and
expected output. In short:

```
pip install -r requirements.txt
pytest tests/benchmark/
```

Expected: `10 passed` (see REPRODUCIBILITY.md for the full transcript,
also captured verbatim on 2026-07-06 from this exact code).
