"""
Patch for CompliVibe core (app.complivibe.in), lands on top of migration head 0175.
Field names / existing-pattern assumptions below are best-effort from PATENT.md and
must be verified against the real ai_system model / AuditService / permission system
before merging -- see core-side-patch/ASSUMPTIONS.md.

POST /api/v1/patent-ingest/p2/obligation-derivation -- the satellite's ONLY
write path into core (agent-push / inbound-only; core never calls the
satellite). Implements PATENT.md's mandatory "Satellites Compute, Core
Decides" validation contract in full:

  1. Re-validate every obligation_id/control_type_id against the live,
     active catalog -> reject (422) on any unknown/inactive reference.
  2. Independently re-derive the obligation set via the reference CTE
     (reference_traversal_cte.derive_obligations_reference) and compare
     exactly to the submitted payload -> flag mismatches instead of
     silently overwriting.
  3. Only write to ai_system_obligation_links when validation passes.
  4. Always audit-log the derivation event (methodology_version,
     trigger_reason, validation_status), whether validated or flagged.

Also, on every ingest (validated or flagged):
  - records the outcome in mismatch_metrics.MismatchMetrics so the
    validation-mismatch rate is queryable (see that module's docstring), and
  - emits a WARNING-level structured log line specifically when a mismatch is
    flagged (governance_graph.obligation_derivation_mismatch), so a human
    watching core's logs -- not just querying governance_graph_traversal_results
    rows -- notices in near-real-time, not only after the fact.
  - is subject to a per-scoped-key rate limit (rate_limiter.py) -- a
    compromised or buggy satellite cannot flood core with derivation writes.

Auth: `Authorization: Bearer <api_key>` scoped key carrying
patent_ingest:p2:write (see dependencies.require_patent_ingest_scope).
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from audit_service_stub import AuditService
from dependencies import (
    ActiveUser,
    Organization,
    get_current_active_user,
    get_current_organization,
    get_db_session,
    require_patent_ingest_scope,
)
from graph_query import resolve_max_traversal_depth
from mismatch_metrics import MismatchMetrics
from models import (
    GovernanceGraphTraversalResult,
    load_active_catalog,
    resolve_ai_system_node_id,
    upsert_ai_system_obligation_links,
    upsert_graph_structure,
)
from rate_limiter import require_ingest_rate_limit, require_ingest_rate_limit_n
from reference_traversal_cte import derive_obligations_reference
from validation import compare_derivation, validate_obligation_control_ids

logger = logging.getLogger("core_side_patch.patent_ingest_p2")

router = APIRouter(prefix="/api/v1/patent-ingest/p2", tags=["patent-ingest-p2"])


def _rate_limited_ingest_scope(
    scoped_key: str = Depends(require_patent_ingest_scope()),
) -> str:
    """Composes scope validation with the per-key ingest rate limit: the scope
    check (401/403) always runs first, and only a caller already carrying a
    valid patent_ingest:p2:write key can even be rate-limited/rejected with a
    429 -- an invalid key never contributes to (or drains) another key's
    budget."""
    require_ingest_rate_limit(scoped_key)
    return scoped_key


class ObligationDerivationRequest(BaseModel):
    # ASSUMPTION: ai_system_id modeled as an opaque string (matches
    # tests/fixtures/sample_export.py's "sys-alpha"/"sys-beta" style ids).
    # Real core's ai_system.id is very likely an integer PK -- see
    # ASSUMPTIONS.md; adjust type + node_key stringification consistently if so.
    ai_system_id: str
    derived_obligations: list[str] = Field(default_factory=list)
    derived_controls: list[str] = Field(default_factory=list)
    graph_path: Any = None
    methodology_version: str
    trigger_reason: Literal["event", "scheduled"]
    derivation_hash: str


def _bad_reference_detail(bad_ids: list[str]) -> dict:
    return {"error": "unknown_or_inactive_obligation_control_ids", "ids": bad_ids}


def _process_one_derivation(
    payload: ObligationDerivationRequest,
    org: Organization,
    user: ActiveUser,
    session: Session,
) -> dict:
    """The full "Satellites Compute, Core Decides" contract for ONE
    derivation payload -- steps 1-4 from the module docstring. Shared by both
    the single-item route and the batch route (see
    `post_obligation_derivations_batch` below) so batching never means a
    second, divergent implementation of the validation contract.

    Raises HTTPException(422) for bad-reference / unknown-ai-system cases,
    exactly as the single-item route always has. The batch route catches
    this per-item so one bad item doesn't fail the rest of the batch.
    """
    # --- Step 1: re-validate every id against the live, active catalog -----
    catalog = load_active_catalog(session, org.id)
    bad_ids = validate_obligation_control_ids(
        {
            "derived_obligations": payload.derived_obligations,
            "derived_controls": payload.derived_controls,
        },
        catalog,
    )
    if bad_ids:
        raise HTTPException(status_code=422, detail=_bad_reference_detail(bad_ids))

    # --- Step 2: independently re-derive and compare exactly ----------------
    ai_system_node_id = resolve_ai_system_node_id(session, org.id, payload.ai_system_id)
    if ai_system_node_id is None:
        raise HTTPException(
            status_code=422,
            detail={"error": "unknown_ai_system_node", "ai_system_id": payload.ai_system_id},
        )

    # MAX_TRAVERSAL_DEPTH is a configurable safety bound, never a hardcoded
    # magic number in the query/loop itself (see PATENT.md CHANGE LOG). Real
    # core should read this from its own settings object the same way
    # src/p2_satellite/config.py's settings.max_traversal_depth does
    # satellite-side; we don't have access to that settings object here, so
    # it's threaded through as a plain parameter, resolved from
    # graph_query.resolve_max_traversal_depth() -- the ONE place this
    # hardcoded default lives on the core side (also used by
    # routers/patent_knowledge_graph_p2.py) -- see ASSUMPTIONS.md.
    reference_derived = derive_obligations_reference(
        session, ai_system_node_id, max_traversal_depth=resolve_max_traversal_depth()
    )
    submitted = {
        "derived_obligations": payload.derived_obligations,
        "derived_controls": payload.derived_controls,
    }
    matches = compare_derivation(submitted, reference_derived)
    validation_status = "validated" if matches else "flagged_mismatch"

    # Record every ingest outcome (validated AND flagged) so the mismatch
    # rate's denominator is correct -- see mismatch_metrics.py.
    MismatchMetrics.record(org.id, validation_status)

    if not matches:
        # A row in governance_graph_traversal_results is queryable, but that's
        # not the same as a human noticing. Emit a WARNING-level structured
        # log line specifically for this case (distinct from AuditService's
        # unconditional INFO-level audit log below, which fires either way).
        logger.warning(
            "governance_graph.obligation_derivation_mismatch",
            extra={
                "org_id": org.id,
                "ai_system_id": payload.ai_system_id,
                "methodology_version": payload.methodology_version,
                "trigger_reason": payload.trigger_reason,
            },
        )

    # --- Step 3: persist the traversal result; only write links if valid ---
    traversal_result = GovernanceGraphTraversalResult(
        org_id=org.id,
        ai_system_id=payload.ai_system_id,
        input_context={"trigger_reason": payload.trigger_reason, "derivation_hash": payload.derivation_hash},
        derived_obligations=payload.derived_obligations,
        derived_controls=payload.derived_controls,
        graph_path=payload.graph_path,
        methodology_version=payload.methodology_version,
        trigger_reason=payload.trigger_reason,
        validation_status=validation_status,
    )
    session.add(traversal_result)
    session.flush()  # populate traversal_result.id without requiring a full commit

    if matches:
        upsert_ai_system_obligation_links(
            session,
            org.id,
            payload.ai_system_id,
            payload.derived_obligations,
            payload.derived_controls,
        )

    session.commit()

    # --- Step 4: audit log, unconditionally, pass or flagged ----------------
    AuditService.write_audit_log(
        session=session,
        org_id=org.id,
        actor_id=user.id,
        event_type="governance_graph.obligation_derivation_ingest",
        payload={
            "ai_system_id": payload.ai_system_id,
            "methodology_version": payload.methodology_version,
            "trigger_reason": payload.trigger_reason,
            "validation_status": validation_status,
            "derivation_hash": payload.derivation_hash,
        },
    )

    return {
        "status": validation_status,
        "validation_status": validation_status,
        "traversal_result_id": traversal_result.id,
        "reference_derived_obligations": reference_derived["derived_obligations"],
        "reference_derived_controls": reference_derived["derived_controls"],
    }


@router.post("/obligation-derivation")
def post_obligation_derivation(
    payload: ObligationDerivationRequest,
    org: Organization = Depends(get_current_organization),
    user: ActiveUser = Depends(get_current_active_user),
    _scope: str = Depends(_rate_limited_ingest_scope),
    session: Session = Depends(get_db_session),
) -> dict:
    return _process_one_derivation(payload, org, user, session)


class BatchObligationDerivationRequest(BaseModel):
    derivations: list[ObligationDerivationRequest] = Field(default_factory=list)


class BatchItemResult(BaseModel):
    ai_system_id: str
    ok: bool
    result: dict | None = None
    error: dict | None = None


@router.post("/obligation-derivations/batch")
def post_obligation_derivations_batch(
    payload: BatchObligationDerivationRequest,
    org: Organization = Depends(get_current_organization),
    user: ActiveUser = Depends(get_current_active_user),
    scoped_key: str = Depends(require_patent_ingest_scope()),
    session: Session = Depends(get_db_session),
) -> dict:
    """Batch variant of /obligation-derivation -- one HTTP round-trip for N
    derivations (e.g. a safety-net poll sweeping thousands of ai_systems)
    instead of N round-trips. Each item goes through the EXACT SAME
    `_process_one_derivation` validation contract as the single-item route --
    this is a transport-level batching optimization, not a second
    implementation of "Satellites Compute, Core Decides".

    A bad item (unknown obligation id, unknown ai_system) fails ONLY that
    item (captured as `{"ok": false, "error": {...}}` in its slot) -- it does
    NOT abort or roll back the rest of the batch. Rate limiting charges the
    WHOLE batch size (len(derivations)) against the caller's per-key budget
    in one atomic check (see rate_limiter.require_ingest_rate_limit_n) --
    NOT `_rate_limited_ingest_scope` (that charges a flat 1 unit per HTTP
    call, which would let a batch of thousands bypass the limit almost for
    free); scope validation still happens via `require_patent_ingest_scope()`
    exactly as every other route uses.
    """
    require_ingest_rate_limit_n(scoped_key, n=len(payload.derivations))

    results: list[dict] = []
    for item in payload.derivations:
        try:
            result = _process_one_derivation(item, org, user, session)
            results.append({"ai_system_id": item.ai_system_id, "ok": True, "result": result})
        except HTTPException as exc:
            session.rollback()
            results.append(
                {
                    "ai_system_id": item.ai_system_id,
                    "ok": False,
                    "error": {"status_code": exc.status_code, "detail": exc.detail},
                }
            )

    return {"results": results}


# ---------------------------------------------------------------------------
# POST /obligation-derivation and /obligation-derivations/batch (above) push
# DERIVED RESULTS; this route pushes GRAPH STRUCTURE itself
# (governance_graph_nodes/edges rows), closing the gap flagged in
# ASSUMPTIONS.md item 22: nothing in this patch set previously wrote to
# those two tables at all -- the ingest routes above only ever READ them
# (load_active_catalog, resolve_ai_system_node_id). The satellite is now the
# sole source of truth for graph structure, pushing its whole freshly-built
# graph (src/p2_satellite/graph_builder.serialize_graph_structure) after
# every fetch -- see that module and src/p2_satellite/ingest_client.py's
# push_graph_structure for the satellite side of this contract.
# ---------------------------------------------------------------------------


class NodeStructureItem(BaseModel):
    node_type: str
    node_key: str
    properties: dict = Field(default_factory=dict)


class EdgeStructureItem(BaseModel):
    source_node_type: str
    source_node_key: str
    target_node_type: str
    target_node_key: str
    edge_type: str
    is_active: bool = True
    weight: float = 1.0
    properties: dict = Field(default_factory=dict)


class GraphStructureRequest(BaseModel):
    nodes: list[NodeStructureItem] = Field(default_factory=list)
    edges: list[EdgeStructureItem] = Field(default_factory=list)
    structure_hash: str


@router.post("/graph-structure")
def post_graph_structure(
    payload: GraphStructureRequest,
    org: Organization = Depends(get_current_organization),
    user: ActiveUser = Depends(get_current_active_user),
    _scope: str = Depends(_rate_limited_ingest_scope),
    session: Session = Depends(get_db_session),
) -> dict:
    """Upsert a full graph-structure snapshot from the satellite.

    Reuses the exact same scope (`patent_ingest:p2:write`) and per-scoped-key
    rate limit as `/obligation-derivation` (`_rate_limited_ingest_scope`) --
    ASSUMPTION: this treats one structure push as 1 rate-limit unit just
    like one derivation push, even though a structure push typically carries
    a much larger payload (the whole graph vs. one ai_system's result).
    Revisit if real satellite traffic shows this needs its own budget -- see
    ASSUMPTIONS.md.

    Upsert-by-natural-key (models.upsert_graph_structure), never a
    duplicate-inserting write: insert new nodes/edges, update ones whose
    properties/weight/is_active changed, leave untouched ones that didn't.
    Always audit-logged (mirrors the unconditional audit log on the
    derivation-ingest path above), whether anything actually changed or the
    push was a no-op repeat of an already-applied structure_hash.
    """
    result = upsert_graph_structure(session, org.id, payload.nodes, payload.edges)
    session.commit()

    AuditService.write_audit_log(
        session=session,
        org_id=org.id,
        actor_id=user.id,
        event_type="governance_graph.structure_ingest",
        payload={"structure_hash": payload.structure_hash, **result},
    )

    return {"structure_hash": payload.structure_hash, **result}
