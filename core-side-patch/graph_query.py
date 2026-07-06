"""
Patch for CompliVibe core (app.complivibe.in), lands on top of migration head 0175.
Field names / existing-pattern assumptions below are best-effort from PATENT.md and
must be verified against the real ai_system model / AuditService / permission system
before merging -- see core-side-patch/ASSUMPTIONS.md.

Shared query/traversal layer behind the SIX customer-facing knowledge-graph
endpoints (routers/patent_knowledge_graph_p2.py), per PATENT.md's "Features
Enabled" section:

  1. POST .../systems/{id}/derive-obligations  -> derive_and_persist_traversal()
  2. GET  .../systems/{id}/graph               -> get_subgraph()
  3. POST .../edges                            -> find_upstream_ai_systems()
  4. GET  .../nodes                            -> list_nodes()
  5. POST .../systems/{id}/sync                -> (no traversal call -- fires
                                                    change_event_outbox.emit_manual_change_event
                                                    instead; see that router
                                                    function's docstring)
  6. GET  .../gaps                             -> find_coverage_gaps()

This module exists so subgraph extraction, node filtering, and the on-demand
reference-CTE call are each implemented exactly ONCE and shared by every
route that needs them, instead of six endpoints reimplementing overlapping
graph-walking logic. It has ZERO import on src/p2_satellite or tests/fixtures
(same hard rule as reference_traversal_cte.py / models.py) and ZERO import on
fastapi (kept DB-session-only/pure so it's testable without an HTTP layer,
same seam style as validation.py).
"""

from __future__ import annotations

from collections import deque
from typing import Any

from sqlalchemy.orm import Session

from models import (
    GovernanceGraphEdge,
    GovernanceGraphNode,
    GovernanceGraphTraversalResult,
    resolve_ai_system_node_id,
    upsert_ai_system_obligation_links,
)
from reference_traversal_cte import derive_obligations_reference

# ASSUMPTION: same as ASSUMPTIONS.md item 11 (routers/patent_ingest_p2.py's
# now-removed private copy of this function) -- core's real settings
# mechanism for MAX_TRAVERSAL_DEPTH is unknown from this satellite-only repo.
# Moved here (from being duplicated per-router) so there is exactly ONE
# place this hardcoded default lives on the core side, per PATENT.md's
# CHANGE LOG requirement that it stay "referenced from one place." Both
# routers/patent_ingest_p2.py and routers/patent_knowledge_graph_p2.py import
# this same function -- neither hardcodes its own copy of `6`.
_DEFAULT_MAX_TRAVERSAL_DEPTH = 6


def resolve_max_traversal_depth() -> int:
    """See module-level ASSUMPTION note above. Must be wired to core's real
    settings object (env var / feature flag / whatever core uses) before
    merge -- see ASSUMPTIONS.md and MERGE_NOTES.md."""
    return _DEFAULT_MAX_TRAVERSAL_DEPTH


# ASSUMPTION: methodology_version tag for traversals NOT submitted by the
# satellite (on-demand / manual-sync-triggered, both computed directly by
# core's own reference_traversal_cte, never by the satellite's NetworkX
# implementation). Distinct from whatever methodology_version string the
# satellite sends on its own ingest payloads, so the two are never confused
# when reading governance_graph_traversal_results rows. Real core may already
# have its own versioning convention for this -- unverified, see
# ASSUMPTIONS.md.
CORE_REFERENCE_METHODOLOGY_VERSION = "core-reference-v1.0.0"

# governance_graph_traversal_results.validation_status for rows produced by
# THIS module (core computing its own answer directly, with nothing
# submitted by the satellite to cross-check against) -- distinct from
# "validated"/"flagged_mismatch", which describe the satellite-submission
# cross-check contract in routers/patent_ingest_p2.py. ASSUMPTION: if
# validation_status is backed by a real DB enum type (not a free varchar) in
# core, this new value needs registering there too -- see ASSUMPTIONS.md.
SELF_DERIVED_VALIDATION_STATUS = "self_derived"


class UnknownAiSystemError(ValueError):
    """Raised when `ai_system_id` has no governance_graph_nodes row for this
    org -- i.e. either it doesn't exist or it belongs to a different org.
    Routers translate this to a 404, matching the existing 422 the ingest
    router already uses for the same underlying condition (that route uses
    422 because it's rejecting a satellite-submitted payload referencing an
    unknown system; these customer-facing routes are resolving a path
    parameter, where 404 is the conventional status -- see ASSUMPTIONS.md,
    this repo can't verify which status core's own convention prefers)."""


def _require_ai_system_node_id(session: Session, org_id: Any, ai_system_id: Any) -> int:
    node_id = resolve_ai_system_node_id(session, org_id, ai_system_id)
    if node_id is None:
        raise UnknownAiSystemError(ai_system_id)
    return node_id


# ---------------------------------------------------------------------------
# Feature 1 (on-demand derive) shared traversal call. Feature 5 (sync) does
# NOT call this directly -- it only emits a change event (see
# change_event_outbox.emit_manual_change_event) -- but this function is what
# a satellite/consumer of that change event would eventually call to
# actually re-derive, so both paths bottom out in the exact same traversal
# implementation. See core-side-patch/tests/test_core_patch_knowledge_graph_router.py's
# convergence test.
# ---------------------------------------------------------------------------


def derive_and_persist_traversal(
    session: Session,
    org_id: Any,
    ai_system_id: Any,
    trigger_reason: str,
    *,
    persist_links: bool = True,
) -> dict:
    """Run core's own reference CTE traversal for one ai_system RIGHT NOW
    (synchronously) and persist the result to
    governance_graph_traversal_results, per PATENT.md's recursive-CTE
    "Traversal Algorithm" section.

    DESIGN DECISION (Feature 1, POST .../systems/{id}/derive-obligations):
    this function runs core's OWN traversal directly against
    governance_graph_nodes/edges -- it does NOT call out to the satellite and
    does NOT wait for the satellite's periodic/event-driven background
    derivation (routers/patent_ingest_p2.py's ingest path). Core already has
    the same graph data the satellite computed the export from (per
    PATENT.md's "Satellites Compute, Core Decides" architecture), so an
    on-demand "give me the answer right now" request can and should be
    answered synchronously and independently, using the exact same
    reference_traversal_cte module the ingest router uses to cross-check
    satellite submissions. This also means an on-demand call here NEVER
    itself calls the satellite -- consistent with the hard "core never calls
    out to the satellite" agent-push rule.

    Unlike routers/patent_ingest_p2.py's `_process_one_derivation` (which
    validates a satellite-SUBMITTED derivation against this same reference
    traversal), there is nothing submitted to compare against here -- core's
    own answer is authoritative by construction, so validation_status is
    SELF_DERIVED_VALIDATION_STATUS, never "validated"/"flagged_mismatch".

    `persist_links=True` (the default) also upserts into
    ai_system_obligation_links, treating core's on-demand computation as
    equally authoritative as a satellite-submitted-and-validated one --
    this is a DESIGN QUESTION worth a human product decision, not just an
    engineering assumption (see ASSUMPTIONS.md): should an on-demand/manual
    traversal immediately become the org's system-of-record obligation
    links, or should it be treated as a preview that doesn't persist until
    corroborated by the satellite's own methodology? This function defaults
    to "yes, persist" (core trusts its own reference implementation
    unconditionally -- that's the entire premise of "Core Decides"), but
    `persist_links=False` is exposed so a caller can get preview-only
    semantics without duplicating this function.

    Raises UnknownAiSystemError if `ai_system_id` has no graph node for this
    org (caller/router maps this to 404).
    """
    ai_system_node_id = _require_ai_system_node_id(session, org_id, ai_system_id)

    reference_derived = derive_obligations_reference(
        session, ai_system_node_id, max_traversal_depth=resolve_max_traversal_depth()
    )

    traversal_result = GovernanceGraphTraversalResult(
        org_id=org_id,
        ai_system_id=str(ai_system_id),
        input_context={"trigger_reason": trigger_reason},
        derived_obligations=reference_derived["derived_obligations"],
        derived_controls=reference_derived["derived_controls"],
        graph_path=None,
        methodology_version=CORE_REFERENCE_METHODOLOGY_VERSION,
        trigger_reason=trigger_reason,
        validation_status=SELF_DERIVED_VALIDATION_STATUS,
    )
    session.add(traversal_result)
    session.flush()  # populate traversal_result.id without requiring a full commit

    if persist_links:
        upsert_ai_system_obligation_links(
            session,
            org_id,
            ai_system_id,
            reference_derived["derived_obligations"],
            reference_derived["derived_controls"],
        )

    session.commit()

    return {
        "ai_system_id": str(ai_system_id),
        "traversal_result_id": traversal_result.id,
        "derived_obligations": reference_derived["derived_obligations"],
        "derived_controls": reference_derived["derived_controls"],
        "methodology_version": CORE_REFERENCE_METHODOLOGY_VERSION,
        "trigger_reason": trigger_reason,
        "validation_status": SELF_DERIVED_VALIDATION_STATUS,
        "traversal_at": traversal_result.traversal_at.isoformat(),
    }


# ---------------------------------------------------------------------------
# Internal: load one org's active nodes/edges into adjacency maps, shared by
# get_subgraph() and find_upstream_ai_systems() below. Deliberately org-
# scoped (unlike reference_traversal_cte.py's SQLite-fallback loader, which
# loads every org's rows -- fine there because it still only ever starts a
# walk from one org's own ai_system node id and PKs never collide across
# orgs, but wasteful; this module scopes the query up front instead, both
# for efficiency and so a defense-in-depth org filter exists at the query
# layer for every one of these customer-facing reads).
# ---------------------------------------------------------------------------


def _load_org_graph(
    session: Session, org_id: Any
) -> tuple[dict[int, GovernanceGraphNode], dict[int, list[GovernanceGraphEdge]], dict[int, list[GovernanceGraphEdge]]]:
    nodes = session.query(GovernanceGraphNode).filter_by(org_id=org_id, archived=False).all()
    nodes_by_id: dict[int, GovernanceGraphNode] = {n.id: n for n in nodes}

    edges = session.query(GovernanceGraphEdge).filter_by(org_id=org_id, is_active=True).all()
    outgoing: dict[int, list[GovernanceGraphEdge]] = {}
    incoming: dict[int, list[GovernanceGraphEdge]] = {}
    for edge in edges:
        outgoing.setdefault(edge.source_node_id, []).append(edge)
        incoming.setdefault(edge.target_node_id, []).append(edge)

    return nodes_by_id, outgoing, incoming


def _node_to_dict(node: GovernanceGraphNode) -> dict:
    properties = node.properties or {}
    return {
        "id": node.id,
        "type": node.node_type,
        # ASSUMPTION: no dedicated "label" column exists on
        # governance_graph_nodes (see models.py) -- a human-readable label
        # falls back to node_key, unless a `properties.label` override is
        # present. Flag for review if core's real graph-visualization
        # frontend needs a distinct label convention.
        "label": properties.get("label", node.node_key),
        "properties": properties,
    }


def _edge_to_dict(edge: GovernanceGraphEdge) -> dict:
    return {
        "source": edge.source_node_id,
        "target": edge.target_node_id,
        "type": edge.edge_type,
    }


# ---------------------------------------------------------------------------
# Feature 2: GET .../systems/{id}/graph
# ---------------------------------------------------------------------------


def get_subgraph(session: Session, org_id: Any, ai_system_id: Any, max_depth: int | None = None) -> dict:
    """Return the subgraph reachable FORWARD from `ai_system_id`'s graph node
    within `max_depth` hops (default: resolve_max_traversal_depth()), shaped
    for a graph-visualization frontend: {"nodes": [...], "edges": [...]}
    with node objects {id, type, label, properties} and edge objects
    {source, target, type} (see module... er, function docstring / task spec).

    This is a READ of already-persisted governance_graph_nodes/edges rows --
    unlike derive_and_persist_traversal() above, it does NOT run/re-run the
    reference CTE and does NOT filter down to terminal
    obligation/control_type nodes; it walks and returns every node type
    encountered (data_category, jurisdiction, regulation, risk_tier,
    obligation, control_type, ...) so a visualization can render the whole
    reasoning path, not just the final derived set.

    Raises UnknownAiSystemError if `ai_system_id` has no graph node for this
    org (caller/router maps this to 404).
    """
    if max_depth is None:
        max_depth = resolve_max_traversal_depth()

    ai_system_node_id = _require_ai_system_node_id(session, org_id, ai_system_id)
    nodes_by_id, outgoing, _incoming = _load_org_graph(session, org_id)

    visited_node_ids: set[int] = {ai_system_node_id}
    visited_edges: dict[tuple[int, int, str], GovernanceGraphEdge] = {}

    queue: deque[tuple[int, int]] = deque([(ai_system_node_id, 0)])
    while queue:
        current_id, depth = queue.popleft()
        if depth >= max_depth:
            continue
        for edge in outgoing.get(current_id, []):
            visited_edges[(edge.source_node_id, edge.target_node_id, edge.edge_type)] = edge
            if edge.target_node_id not in visited_node_ids:
                visited_node_ids.add(edge.target_node_id)
                queue.append((edge.target_node_id, depth + 1))

    node_dicts = [_node_to_dict(nodes_by_id[nid]) for nid in visited_node_ids if nid in nodes_by_id]
    edge_dicts = [_edge_to_dict(edge) for edge in visited_edges.values()]

    return {"nodes": node_dicts, "edges": edge_dicts}


# Fixed, small palette keyed by node_type -- purely cosmetic (debugging/demo
# aid, see render_subgraph_html's docstring), not a claim about a real
# design system. Falls back to a neutral gray for any node_type not listed
# (e.g. a future node_type this patch doesn't know about yet).
_NODE_TYPE_COLORS = {
    "ai_system": "#4C72B0",
    "regulation": "#DD8452",
    "jurisdiction": "#55A868",
    "data_category": "#C44E52",
    "risk_tier": "#8172B2",
    "obligation": "#937860",
    "control_type": "#64B5CD",
}
_DEFAULT_NODE_COLOR = "#999999"


def render_subgraph_html(subgraph: dict) -> str:
    """Render a get_subgraph()-shaped {"nodes": [...], "edges": [...]} dict
    as a single, self-contained, interactive HTML page via pyvis (BSD-3
    licensed) -- additive to Feature 2's JSON response (see
    routers/patent_knowledge_graph_p2.py's `?format=html` handling), useful
    for debugging/demos before a real frontend consumes the JSON contract.
    The JSON shape remains the primary, versioned contract; this is a
    convenience rendering of the exact same data, not a second data model.

    `cdn_resources="in_line"` is what makes this fully self-contained (the
    visualization JS library is inlined into the returned string, not
    fetched from a CDN at view time) -- deliberately NOT pyvis's default
    ("local"), which would try to write/reference separate asset files next
    to an on-disk output file. This function never touches the filesystem:
    `Network.generate_html()` returns a plain string, nothing is written to
    disk, which matters here since this runs inside an HTTP request handler
    (routers/patent_knowledge_graph_p2.py) where writing files as a
    side effect of a GET request would be a bad practice regardless of
    licensing.
    """
    from pyvis.network import Network

    net = Network(height="750px", width="100%", directed=True, cdn_resources="in_line")

    for node in subgraph["nodes"]:
        color = _NODE_TYPE_COLORS.get(node["type"], _DEFAULT_NODE_COLOR)
        title = f"{node['type']}: {node['label']}"
        if node["properties"]:
            title += f"\n{node['properties']}"
        net.add_node(node["id"], label=node["label"], title=title, color=color)

    for edge in subgraph["edges"]:
        net.add_edge(edge["source"], edge["target"], label=edge["type"], arrows="to")

    html: str = net.generate_html()
    return html


# ---------------------------------------------------------------------------
# Feature 4: GET .../nodes?type=regulation
# ---------------------------------------------------------------------------


def list_nodes(
    session: Session,
    org_id: Any,
    node_type: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[dict], int]:
    """Paginated, optionally node_type-filtered listing of one org's active
    governance_graph_nodes.

    DESIGN QUESTION (not just an assumption -- flagged explicitly, see
    ASSUMPTIONS.md and the task's own callout): governance_graph_nodes.org_id
    is NOT NULL (see migrations/0176_add_governance_graph_tables.py), so
    every node row -- including node_type='regulation'/'jurisdiction' rows
    that are conceptually GLOBAL reference data shared across all tenants
    (regulations/jurisdictions don't differ per customer) -- is currently
    modeled as belonging to exactly one org. That almost certainly means
    today's satellite-side graph_builder.py-equivalent ingestion duplicates
    identical regulation/jurisdiction nodes once per org rather than storing
    them once, globally. This function filters strictly by org_id for ALL
    node types (including regulation/jurisdiction) because that's what the
    current schema supports -- a human on the core team needs to decide
    whether that duplication is intentional (acceptable, even desirable, for
    per-tenant archival/versioning independence) or whether
    regulation/jurisdiction nodes should instead be de-duplicated into a
    shared global org_id sentinel (or a nullable org_id meaning "global"),
    which would change this function's WHERE clause for those node types.
    """
    query = session.query(GovernanceGraphNode).filter_by(org_id=org_id, archived=False)
    if node_type is not None:
        query = query.filter_by(node_type=node_type)

    total = query.count()
    rows = query.order_by(GovernanceGraphNode.id).offset((page - 1) * page_size).limit(page_size).all()
    return [_node_to_dict(row) for row in rows], total


def envelope(items: list, meta_extra: dict | None = None) -> dict:
    """Response envelope shared by the paginated/listing endpoints in
    routers/patent_knowledge_graph_p2.py (Feature 4's node listing, Feature
    6's gap listing). Mirrors routers/patent_exports_p2.py's `_envelope`
    helper (items + meta) -- the only real precedent for a response shape
    available in this repo. ASSUMPTION: the other ~1,609 existing core
    endpoints this task references may use a different envelope shape
    entirely (this repo has no way to check) -- flag for review and
    reconcile with core's real convention before merge, see ASSUMPTIONS.md.
    """
    meta = {"count": len(items)}
    if meta_extra:
        meta.update(meta_extra)
    return {"items": items, "meta": meta}


# ---------------------------------------------------------------------------
# Feature 3: POST .../edges -- "which ai_systems does this new edge affect?"
# ---------------------------------------------------------------------------


def find_upstream_ai_systems(session: Session, org_id: Any, node_id: Any, max_depth: int | None = None) -> list[str]:
    """Return the ai_system_id (node_key) of every ai_system node that can
    reach `node_id` via a forward path of active edges within `max_depth`
    hops (default: resolve_max_traversal_depth()), plus `node_id` itself if
    it IS an ai_system node.

    Used by the manual-edge-addition endpoint: adding an edge
    source_node_id -> target_node_id can only change the derived obligation
    set of an ai_system that could already reach source_node_id (that
    ai_system's traversal will now additionally flow through the new edge to
    target_node_id and beyond) -- so this is exactly the set of "affected"
    ai_systems that should have their derivation re-triggered
    (change_event_outbox.emit_manual_change_event, one event per system).

    Implemented as a reverse BFS over the org's incoming-edge adjacency map
    (built once via _load_org_graph, shared with get_subgraph's forward
    walk). ASSUMPTION/SCALABILITY NOTE: this is correct but is a full
    reverse-reachability walk bounded only by max_depth, not by how many
    ai_systems exist in the org -- for a very large org graph this could
    become expensive per manual edge addition. A production implementation
    might instead queue a background full-org rescan rather than compute
    this synchronously inline in the request path; flagged for review, see
    ASSUMPTIONS.md.
    """
    if max_depth is None:
        max_depth = resolve_max_traversal_depth()

    nodes_by_id, _outgoing, incoming = _load_org_graph(session, org_id)

    ancestor_ids: set[int] = set()
    queue: deque[tuple[int, int]] = deque([(node_id, 0)])
    seen: set[int] = {node_id}
    while queue:
        current_id, depth = queue.popleft()
        if depth >= max_depth:
            continue
        for edge in incoming.get(current_id, []):
            if edge.source_node_id not in seen:
                seen.add(edge.source_node_id)
                ancestor_ids.add(edge.source_node_id)
                queue.append((edge.source_node_id, depth + 1))

    candidate_ids = ancestor_ids | {node_id}
    return sorted(
        node.node_key
        for nid in candidate_ids
        if (node := nodes_by_id.get(nid)) is not None and node.node_type == "ai_system"
    )


# ---------------------------------------------------------------------------
# Feature 6: GET .../gaps -- "obligations derived but no control covering them"
# ---------------------------------------------------------------------------

# ASSUMPTION: mirrors src/p2_satellite/schema.py's EDGE_OBLIGATION_NEEDS
# constant value ("obligation_needs") -- core-side-patch/ must not import
# src/p2_satellite (hard rule), so this is a duplicated literal, not a shared
# import. If core's real graph-population mechanism (see ASSUMPTIONS.md new
# item on how governance_graph_nodes/edges actually get populated in the real
# core -- this repo's ingest router never writes them) uses a different
# edge_type string for "this obligation needs this control_type", this
# constant must be updated to match.
OBLIGATION_NEEDS_CONTROL_EDGE_TYPE = "obligation_needs"


def find_coverage_gaps(session: Session, org_id: Any) -> list[dict]:
    """Return obligations that were derived for some ai_system but have no
    control_type marked as covering them, per PATENT.md's "Features Enabled"
    section ("Obligations derived but no controls covering them").

    DESIGN QUESTION -- escalate to a human, do not treat this function's
    approach as verified (see ASSUMPTIONS.md): "covered" requires a concept
    of control-IMPLEMENTATION-status (e.g. implemented / planned / not
    started) that this repo could not locate anywhere in the codebase. The
    only related table available here, ai_system_obligation_links (itself an
    ASSUMED, unverified schema -- see ASSUMPTIONS.md item 7), stores
    obligation_id and control_type_id as two independent flat lists with NO
    per-row implementation-status field and NO pairing between a specific
    obligation and the specific control_type(s) that satisfy it. Given that,
    this function defines "covered" the most defensible way the AVAILABLE
    data supports:
        for each ai_system's latest derived obligation set, an obligation is
        "covered" if at least one of the control_types the GRAPH says it
        needs (via OBLIGATION_NEEDS_CONTROL_EDGE_TYPE edges from the
        obligation node) is ALSO present in that ai_system's linked
        control_type_ids in ai_system_obligation_links.
    This is presence-of-a-linked-control, NOT implemented-vs-not-implemented
    status -- if/when a real controls-implementation-status concept is found
    elsewhere in core (this is exactly the kind of cross-cutting concern the
    task flagged as likely to already exist), this function's coverage
    predicate should be rewritten against that instead.

    Obligations with ZERO OBLIGATION_NEEDS_CONTROL_EDGE_TYPE edges in the
    graph (i.e. the graph doesn't say what control satisfies them at all)
    are deliberately NOT reported as gaps -- there's no basis to claim
    "uncovered" vs. "coverage isn't modeled for this obligation" with the
    data available, and conflating the two would make this endpoint noisy
    in a way that erodes trust in it.
    """
    from models import AiSystemObligationLink  # local import: avoid a module-level

    # cycle (models.py doesn't import graph_query.py, but keeping this import
    # local documents that AiSystemObligationLink is only needed here, not by
    # the rest of this module).

    ai_system_nodes = (
        session.query(GovernanceGraphNode).filter_by(org_id=org_id, node_type="ai_system", archived=False).all()
    )

    obligation_needs: dict[str, set[str]] = {}
    for edge in (
        session.query(GovernanceGraphEdge)
        .filter_by(org_id=org_id, edge_type=OBLIGATION_NEEDS_CONTROL_EDGE_TYPE, is_active=True)
        .all()
    ):
        source = session.get(GovernanceGraphNode, edge.source_node_id)
        target = session.get(GovernanceGraphNode, edge.target_node_id)
        if source is None or target is None:
            continue
        obligation_needs.setdefault(source.node_key, set()).add(target.node_key)

    gaps: list[dict] = []
    for ai_system_node in ai_system_nodes:
        ai_system_id = ai_system_node.node_key

        latest_result = (
            session.query(GovernanceGraphTraversalResult)
            .filter_by(org_id=org_id, ai_system_id=ai_system_id)
            .order_by(GovernanceGraphTraversalResult.traversal_at.desc())
            .first()
        )
        if latest_result is None:
            continue

        linked_control_types = {
            row.control_type_id
            for row in session.query(AiSystemObligationLink).filter_by(org_id=org_id, ai_system_id=ai_system_id)
            if row.control_type_id is not None
        }

        for obligation_id in latest_result.derived_obligations:
            required = obligation_needs.get(obligation_id)
            if not required:
                continue  # graph doesn't model required controls for this obligation -- not reportable, see docstring
            if required & linked_control_types:
                continue  # at least one required control_type is linked -- treated as covered
            gaps.append(
                {
                    "ai_system_id": ai_system_id,
                    "obligation_id": obligation_id,
                    "required_control_types": sorted(required),
                    "linked_control_types": sorted(required & linked_control_types),
                }
            )

    return gaps
