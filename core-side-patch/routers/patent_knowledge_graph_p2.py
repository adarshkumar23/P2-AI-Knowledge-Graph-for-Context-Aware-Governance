"""
Patch for CompliVibe core (app.complivibe.in), lands on top of migration head 0175.
Field names / existing-pattern assumptions below are best-effort from PATENT.md and
must be verified against the real ai_system model / AuditService / permission system
before merging -- see core-side-patch/ASSUMPTIONS.md.

The SIX customer-facing knowledge-graph endpoints from PATENT.md's "Features
Enabled" section. Unlike routers/patent_exports_p2.py and
routers/patent_ingest_p2.py (satellite-only, scoped-API-key auth), these are
reached by normal authenticated CompliVibe users (compliance officers) via
dependencies.get_current_organization / get_current_active_user +
dependencies.require_permission -- see permissions.GOVERNANCE_GRAPH_READ/WRITE.

ASSUMPTION: the path prefix below (`/ai-governance/knowledge-graph`) is taken
verbatim from the task spec and does NOT follow the `/api/v1/patent-*/p2`
prefix convention routers/patent_exports_p2.py and routers/patent_ingest_p2.py
already established for this same patent's satellite-facing endpoints. We
could not verify whether core's real routing convention wants these under
`/api/v1/...` too (most likely, given ~1,609 other endpoints presumably all
share one prefix scheme) -- flag for review before merge, see ASSUMPTIONS.md.

All six endpoints share one query/traversal layer (graph_query.py) rather
than each reimplementing subgraph-walking or traversal logic -- see that
module's docstring for the full map of which endpoint uses which function.

Core never calls the satellite from any of these six routes (agent-push /
inbound-only rule, same as the other two routers in this patch set) --
Feature 1 (on-demand derive) runs core's own reference CTE directly, and
Feature 5 (sync) only writes a row to the existing change-event outbox for
the satellite to notice next time it polls/exports.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from audit_service_stub import AuditService
from change_event_outbox import emit_manual_change_event
from dependencies import (
    ActiveUser,
    Organization,
    get_current_active_user,
    get_current_organization,
    get_db_session,
    require_permission,
)
from graph_query import (
    UnknownAiSystemError,
    derive_and_persist_traversal,
    envelope,
    find_coverage_gaps,
    find_upstream_ai_systems,
    get_subgraph,
    list_nodes,
    render_subgraph_html,
)
from models import create_manual_edge, get_node, resolve_ai_system_node_id
from permissions import GOVERNANCE_GRAPH_READ, GOVERNANCE_GRAPH_WRITE
from rate_limiter import require_on_demand_derive_rate_limit

router = APIRouter(prefix="/ai-governance/knowledge-graph", tags=["knowledge-graph-p2"])


def _ai_system_not_found(ai_system_id: str) -> HTTPException:
    return HTTPException(status_code=404, detail={"error": "ai_system_not_found", "ai_system_id": ai_system_id})


# ---------------------------------------------------------------------------
# Feature 1: POST .../systems/{id}/derive-obligations -- on-demand, synchronous
# ---------------------------------------------------------------------------


@router.post("/systems/{ai_system_id}/derive-obligations")
def derive_obligations_on_demand(
    ai_system_id: str = Path(...),
    org: Organization = Depends(get_current_organization),
    user: ActiveUser = Depends(get_current_active_user),
    _perm: None = Depends(require_permission(GOVERNANCE_GRAPH_READ)),
    session: Session = Depends(get_db_session),
) -> dict:
    """Run core's own reference traversal (the recursive CTE from PATENT.md)
    directly and SYNCHRONOUSLY -- this endpoint does NOT wait for or trigger
    the satellite. The satellite's job (routers/patent_ingest_p2.py) is
    periodic/event-driven BACKGROUND derivation, cross-checked against this
    same reference CTE when it's submitted; this endpoint is "give me the
    answer right now," which core can already answer on its own because it
    holds the same graph data the satellite's export was built from. See
    graph_query.derive_and_persist_traversal's docstring for the full
    rationale, including the "does this write ai_system_obligation_links
    immediately" design question flagged there.

    Validate the ai_system exists and belongs to the requesting org BEFORE
    charging the per-org rate limit budget -- a typo'd id shouldn't cost the
    org part of its on-demand-derivation quota for the window.
    """
    ai_system_node_id = resolve_ai_system_node_id(session, org.id, ai_system_id)
    if ai_system_node_id is None:
        raise _ai_system_not_found(ai_system_id)

    require_on_demand_derive_rate_limit(org.id)

    result = derive_and_persist_traversal(session, org.id, ai_system_id, trigger_reason="on_demand")

    AuditService.write_audit_log(
        session=session,
        org_id=org.id,
        actor_id=user.id,
        event_type="governance_graph.on_demand_derivation",
        payload={
            "ai_system_id": ai_system_id,
            "traversal_result_id": result["traversal_result_id"],
        },
    )

    return result


# ---------------------------------------------------------------------------
# Feature 2: GET .../systems/{id}/graph -- read existing graph data
# ---------------------------------------------------------------------------


@router.get("/systems/{ai_system_id}/graph", response_model=None)
def get_ai_system_graph(
    ai_system_id: str = Path(...),
    format: str = Query(
        default="json",
        pattern="^(json|html)$",
        description="'json' (default, the primary contract) or 'html' for a rendered, interactive graph view.",
    ),
    org: Organization = Depends(get_current_organization),
    _user: ActiveUser = Depends(get_current_active_user),
    _perm: None = Depends(require_permission(GOVERNANCE_GRAPH_READ)),
    session: Session = Depends(get_db_session),
) -> dict | HTMLResponse:
    """Return {"nodes": [...], "edges": [...]} for the subgraph reachable
    from this ai_system within MAX_TRAVERSAL_DEPTH. A READ of already-
    persisted graph rows -- no traversal is re-run, no write happens.

    `?format=html` is ADDITIVE, not a replacement for the JSON contract:
    it renders the exact same subgraph as a self-contained, interactive
    HTML page (pyvis, BSD-3) -- useful for debugging/demos before a real
    graph-visualization frontend consumes the JSON directly. The default
    remains JSON; nothing about the JSON response shape changes because
    this parameter exists.
    """
    try:
        subgraph = get_subgraph(session, org.id, ai_system_id)
    except UnknownAiSystemError:
        raise _ai_system_not_found(ai_system_id) from None

    if format == "html":
        return HTMLResponse(content=render_subgraph_html(subgraph))
    return subgraph


# ---------------------------------------------------------------------------
# Feature 3: POST .../edges -- manual edge addition
# ---------------------------------------------------------------------------


class ManualEdgeCreateRequest(BaseModel):
    source_node_id: int
    target_node_id: int
    edge_type: str
    weight: float = 1.0
    properties: dict = Field(default_factory=dict)


@router.post("/edges", status_code=201)
def create_manual_edge_endpoint(
    payload: ManualEdgeCreateRequest,
    org: Organization = Depends(get_current_organization),
    user: ActiveUser = Depends(get_current_active_user),
    _perm: None = Depends(require_permission(GOVERNANCE_GRAPH_WRITE)),
    session: Session = Depends(get_db_session),
) -> dict:
    """Let a compliance officer manually add a relationship the automated
    graph didn't infer (e.g. a jurisdiction-specific nuance). Both node ids
    must already exist AND belong to the requesting org -- reject dangling
    or cross-org references with 422, never silently create a half-valid
    edge.

    Audit-logged (a manual edge changes future derivation results, so it
    needs the same traceability an automated one gets) and tagged in
    `properties` as {"source": "manual", "added_by": <user_id>}.

    DESIGN DECISION: adding a manual edge DOES trigger re-derivation for
    every ai_system whose forward traversal could now reach the new edge
    (graph_query.find_upstream_ai_systems) -- reusing the SAME change-event
    outbox mechanism the automated watched-field trigger uses
    (change_event_outbox.emit_manual_change_event), not a second
    notification path. This mirrors Feature 5 (sync) exactly, and is
    deliberately fire-and-forget from this endpoint's perspective: it queues
    re-derivation, it does not run it inline (running N traversals inline in
    an edge-creation request would make this endpoint's latency depend on
    how many systems the graph happens to fan out to).
    """
    source = get_node(session, org.id, payload.source_node_id)
    if source is None:
        raise HTTPException(
            status_code=422,
            detail={"error": "unknown_source_node_id", "node_id": payload.source_node_id},
        )
    target = get_node(session, org.id, payload.target_node_id)
    if target is None:
        raise HTTPException(
            status_code=422,
            detail={"error": "unknown_target_node_id", "node_id": payload.target_node_id},
        )

    properties = dict(payload.properties)
    properties["source"] = "manual"
    properties["added_by"] = user.id

    edge = create_manual_edge(
        session,
        org.id,
        payload.source_node_id,
        payload.target_node_id,
        payload.edge_type,
        payload.weight,
        properties,
    )
    session.flush()  # populate edge.id

    AuditService.write_audit_log(
        session=session,
        org_id=org.id,
        actor_id=user.id,
        event_type="governance_graph.manual_edge_added",
        payload={
            "edge_id": edge.id,
            "source_node_id": payload.source_node_id,
            "target_node_id": payload.target_node_id,
            "edge_type": payload.edge_type,
        },
    )

    affected_ai_system_ids = find_upstream_ai_systems(session, org.id, payload.source_node_id)
    for affected_ai_system_id in affected_ai_system_ids:
        emit_manual_change_event(session, org_id=org.id, ai_system_id=affected_ai_system_id)

    session.commit()

    return {
        "id": edge.id,
        "source_node_id": edge.source_node_id,
        "target_node_id": edge.target_node_id,
        "edge_type": edge.edge_type,
        "weight": edge.weight,
        "properties": edge.properties,
        "affected_ai_system_ids": affected_ai_system_ids,
    }


# ---------------------------------------------------------------------------
# Feature 4: GET .../nodes?type=regulation -- browse nodes
# ---------------------------------------------------------------------------


@router.get("/nodes")
def browse_nodes(
    type: str | None = Query(default=None, description="Filter by node_type, e.g. 'regulation'"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    org: Organization = Depends(get_current_organization),
    _user: ActiveUser = Depends(get_current_active_user),
    _perm: None = Depends(require_permission(GOVERNANCE_GRAPH_READ)),
    session: Session = Depends(get_db_session),
) -> dict:
    """Paginated, optionally node_type-filtered node listing. ASSUMPTION:
    page/page_size query params are a guessed pagination convention -- this
    repo has no existing paginated endpoint to confirm core's real
    convention against (limit/offset? cursor-based?) -- see ASSUMPTIONS.md.

    See graph_query.list_nodes' docstring for the unresolved
    global-vs-org-scoped node_type design question (regulation/jurisdiction
    nodes are filtered by org_id here exactly like every other node_type,
    which may not be what core actually wants for reference-data node
    types)."""
    items, total = list_nodes(session, org.id, node_type=type, page=page, page_size=page_size)
    return envelope(items, {"page": page, "page_size": page_size, "total": total})


# ---------------------------------------------------------------------------
# Feature 5: POST .../systems/{id}/sync -- manual "something changed, re-check"
# ---------------------------------------------------------------------------


@router.post("/systems/{ai_system_id}/sync")
def sync_ai_system(
    ai_system_id: str = Path(...),
    org: Organization = Depends(get_current_organization),
    user: ActiveUser = Depends(get_current_active_user),
    _perm: None = Depends(require_permission(GOVERNANCE_GRAPH_WRITE)),
    session: Session = Depends(get_db_session),
) -> dict:
    """Manual "re-check this system now" trigger. Functionally similar to
    Feature 1 (both eventually produce a fresh reference-CTE derivation for
    this ai_system) but semantically distinct: Feature 1 answers "what's the
    current derivation" synchronously by running it right here; this
    endpoint instead fires the SAME change-event outbox mechanism the
    automated watched-field trigger uses
    (change_event_outbox.emit_manual_change_event) and returns immediately
    -- it does NOT call graph_query.derive_and_persist_traversal itself.

    This keeps exactly ONE code path responsible for "an ai_system needs
    re-derivation via the event-triggered flow" regardless of whether a
    human (this endpoint) or an automated column-watcher
    (change_event_outbox.emit_change_event, per its TODO) triggered it --
    both write to governance_graph_change_events, and whatever downstream
    consumer processes that outbox (the satellite, on its next export pull)
    is the ONLY thing that actually re-derives via this path. See
    core-side-patch/tests/test_core_patch_knowledge_graph_router.py's
    convergence test proving Feature 1's synchronous path and this event-
    triggered path bottom out in the identical reference-CTE result for the
    same ai_system.
    """
    ai_system_node_id = resolve_ai_system_node_id(session, org.id, ai_system_id)
    if ai_system_node_id is None:
        raise _ai_system_not_found(ai_system_id)

    event = emit_manual_change_event(session, org_id=org.id, ai_system_id=ai_system_id)
    session.flush()  # populate event.id

    AuditService.write_audit_log(
        session=session,
        org_id=org.id,
        actor_id=user.id,
        event_type="governance_graph.manual_sync_requested",
        payload={"ai_system_id": ai_system_id, "change_event_id": event.id},
    )

    session.commit()

    return {"status": "sync_queued", "ai_system_id": ai_system_id, "change_event_id": event.id}


# ---------------------------------------------------------------------------
# Feature 6: GET .../gaps -- coverage gap detection
# ---------------------------------------------------------------------------


@router.get("/gaps")
def get_coverage_gaps(
    org: Organization = Depends(get_current_organization),
    _user: ActiveUser = Depends(get_current_active_user),
    _perm: None = Depends(require_permission(GOVERNANCE_GRAPH_READ)),
    session: Session = Depends(get_db_session),
) -> dict:
    """Obligations derived (for any ai_system in this org) that have no
    control_type covering them, per PATENT.md's "Features Enabled" section.
    See graph_query.find_coverage_gaps' docstring for the DESIGN QUESTION
    this endpoint could not resolve: no controls-implementation-status
    concept could be located in this repo, so "covered" here means
    "structurally linked," not "marked implemented" -- flag for a human
    familiar with CompliVibe's four-pillar architecture before trusting this
    as a literal implementation-status signal."""
    gaps = find_coverage_gaps(session, org.id)
    return envelope(gaps, {"total": len(gaps)})
