"""
Graph construction for the P2 satellite (Workstream B).

Two responsibilities, kept strictly separate:
  1. Fetching: httpx-based client functions that pull the three read-only
     core export payloads (ai-systems, regulations-catalog, jurisdictions),
     with tenacity retries on transient failures.
  2. Building: a PURE function, build_graph(), with zero network dependency,
     that turns the three already-parsed response dicts into a
     networkx.DiGraph. This mirrors tests/fixtures/reference_cte.py's
     _build_node_edge_rows() exactly — same node ids (via schema.node_id),
     same edges, same edge_type/is_active semantics.

Never import anything starting with `app.` — see PATENT.md / CLAUDE_CODE_GOAL_PROMPT.md
agent-push / inbound-only rule. This module only ever calls OUT to core's
read-only export endpoints; core never calls the satellite.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
import networkx as nx
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from src.p2_satellite import observability, schema
from src.p2_satellite.config import settings

logger = observability.get_logger(__name__)
# Defense in depth: this module constructs the Authorization header carrying
# core_export_api_key. We never deliberately log the header/key value (see
# _auth_headers below and its callers), but install redaction anyway so an
# accidental future debug-log can never leak the raw secret.
observability.install_secret_redaction(logger)

AI_SYSTEMS_PATH = "/api/v1/patent-exports/p2/ai-systems"
REGULATIONS_CATALOG_PATH = "/api/v1/patent-exports/p2/regulations-catalog"
JURISDICTIONS_PATH = "/api/v1/patent-exports/p2/jurisdictions"


class GraphBuildIncompleteError(Exception):
    """Raised by fetch_and_build_graph() when one of the three required core
    exports could not be fetched, so the graph build could not proceed.

    Wraps the original exception (an httpx exception, in practice) along with
    which export step failed -- 'ai-systems', 'regulations-catalog', or
    'jurisdictions' -- so callers get one unambiguous exception type to catch
    instead of needing to know httpx's exception hierarchy. There is no path
    that produces a partial/incomplete graph object: fetch_and_build_graph()
    always either returns a fully-built graph or raises this error.
    """

    def __init__(self, step: str, original_exc: BaseException) -> None:
        self.step = step
        self.original_exc = original_exc
        super().__init__(
            f"graph build incomplete: export step '{step}' failed " f"({type(original_exc).__name__}: {original_exc})"
        )


def _is_retryable_http_error(exc: BaseException) -> bool:
    """Retry on connection/timeout errors and 5xx responses only — never on 4xx."""
    if isinstance(exc, (httpx.ConnectError, httpx.TimeoutException)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return 500 <= exc.response.status_code < 600
    return False


def _retry_decorator():
    return retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
        retry=retry_if_exception(_is_retryable_http_error),
        reraise=True,
    )


def _auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.core_export_api_key}"}


def _get_json(path: str, changed_since: str | None = None) -> dict[str, Any]:
    """Fetch a single export endpoint. Wrapped with tenacity retry."""

    @_retry_decorator()
    def _do_request() -> dict[str, Any]:
        params: dict[str, str] = {}
        if changed_since is not None:
            params["changed_since"] = changed_since
        url = f"{settings.core_base_url}{path}"
        with httpx.Client(timeout=10.0) as client:
            response = client.get(url, headers=_auth_headers(), params=params or None)
        response.raise_for_status()
        payload: dict[str, Any] = response.json()
        return payload

    # tenacity's `retry` decorator doesn't preserve _do_request's precise
    # return type in its stubs (widens to Any) -- explicit annotation here
    # restates what's already true at runtime (and enforced by _do_request's
    # own -> dict[str, Any] signature) rather than a real type gap.
    result: dict[str, Any] = _do_request()
    return result


def fetch_ai_systems(changed_since: str | None = None) -> dict[str, Any]:
    """GET {core_base_url}/api/v1/patent-exports/p2/ai-systems"""
    return _get_json(AI_SYSTEMS_PATH, changed_since=changed_since)


def fetch_regulations_catalog(changed_since: str | None = None) -> dict[str, Any]:
    """GET {core_base_url}/api/v1/patent-exports/p2/regulations-catalog"""
    return _get_json(REGULATIONS_CATALOG_PATH, changed_since=changed_since)


def fetch_jurisdictions(changed_since: str | None = None) -> dict[str, Any]:
    """GET {core_base_url}/api/v1/patent-exports/p2/jurisdictions"""
    return _get_json(JURISDICTIONS_PATH, changed_since=changed_since)


def build_graph(
    ai_systems: dict[str, Any],
    regulations_catalog: dict[str, Any],
    jurisdictions: dict[str, Any],
) -> nx.DiGraph:
    """Pure transformation: three parsed export dicts -> networkx.DiGraph.

    ZERO network/httpx dependency. Mirrors
    tests/fixtures/reference_cte.py's _build_node_edge_rows() derivation
    rules exactly:
      ai_system   -system_uses->          data_category
      ai_system   -system_deploys_in->    jurisdiction
      ai_system   -system_classified_as-> risk_tier
      data_category -data_triggers->      regulation
      jurisdiction  -jurisdiction_has->   regulation
      regulation    -regulation_requires-> obligation
      obligation    -obligation_needs->   control_type
      risk_tier     -risk_tier_adds->     obligation (+ obligation_needs -> control_type)
    """
    g = nx.DiGraph()

    def add_node(node_type: str, node_key: str) -> str:
        nid = schema.node_id(node_type, node_key)
        if not g.has_node(nid):
            g.add_node(nid, node_type=node_type, node_key=node_key)
        return nid

    def add_edge(edge_type: str, source_id: str, target_id: str) -> None:
        schema.validate_edge_type(edge_type)
        g.add_edge(source_id, target_id, edge_type=edge_type, is_active=True)

    # jurisdictions + jurisdiction_has -> regulation
    for j in jurisdictions["items"]:
        jid = add_node(schema.NODE_JURISDICTION, j["key"])
        for reg_key in j["regulations"]:
            rid = add_node(schema.NODE_REGULATION, reg_key)
            add_edge(schema.EDGE_JURISDICTION_HAS, jid, rid)

    # regulations -> requires -> obligations -> needs -> control_types
    # + data_category -> data_triggers -> regulation
    for reg in regulations_catalog["items"]:
        rid = add_node(schema.NODE_REGULATION, reg["key"])
        for dc_key in reg["triggered_by_data_categories"]:
            dcid = add_node(schema.NODE_DATA_CATEGORY, dc_key)
            add_edge(schema.EDGE_DATA_TRIGGERS, dcid, rid)
        for ob in reg["requires_obligations"]:
            oid = add_node(schema.NODE_OBLIGATION, ob["key"])
            add_edge(schema.EDGE_REGULATION_REQUIRES, rid, oid)
            for ctrl_key in ob["needs_controls"]:
                cid = add_node(schema.NODE_CONTROL_TYPE, ctrl_key)
                add_edge(schema.EDGE_OBLIGATION_NEEDS, oid, cid)

    # risk_tier -> risk_tier_adds -> obligation -> needs -> control_type
    for tier_key, obligations in regulations_catalog["risk_tier_obligations"].items():
        tid = add_node(schema.NODE_RISK_TIER, tier_key)
        for ob in obligations:
            oid = add_node(schema.NODE_OBLIGATION, ob["key"])
            add_edge(schema.EDGE_RISK_TIER_ADDS, tid, oid)
            for ctrl_key in ob["needs_controls"]:
                cid = add_node(schema.NODE_CONTROL_TYPE, ctrl_key)
                add_edge(schema.EDGE_OBLIGATION_NEEDS, oid, cid)

    # ai systems -> uses/deploys_in/classified_as
    for sysd in ai_systems["items"]:
        sid = add_node(schema.NODE_AI_SYSTEM, sysd["id"])
        for dc_key in sysd["data_categories"]:
            dcid = add_node(schema.NODE_DATA_CATEGORY, dc_key)
            add_edge(schema.EDGE_SYSTEM_USES, sid, dcid)
        for geo in sysd["geographic_scope"]:
            jid = add_node(schema.NODE_JURISDICTION, geo)
            add_edge(schema.EDGE_SYSTEM_DEPLOYS_IN, sid, jid)
        tid = add_node(schema.NODE_RISK_TIER, sysd["risk_tier"])
        add_edge(schema.EDGE_SYSTEM_CLASSIFIED_AS, sid, tid)

    return g


def serialize_graph_structure(graph: nx.DiGraph) -> dict[str, Any]:
    """Serialize a built graph into the wire payload for
    `ingest_client.push_graph_structure()` / core's
    `POST /api/v1/patent-ingest/p2/graph-structure` (see that router
    function's docstring in core-side-patch/routers/patent_ingest_p2.py).

    Uses NATURAL keys (node_type, node_key) for every node and edge
    endpoint, never the internal NetworkX node id (`schema.node_id()`'s
    "{node_type}:{node_key}" string) or a fresh identifier per push -- core
    upserts by this same natural key against `governance_graph_nodes`'
    existing `UNIQUE(org_id, node_type, node_key)` constraint, so re-pushing
    an unchanged graph is a safe no-op and re-pushing a changed one updates
    in place rather than duplicating rows.

    Node/edge lists are sorted before returning so the payload (and
    therefore `ingest_client.compute_structure_hash()`'s digest of it) is
    deterministic across repeated builds of the same underlying export data
    -- NetworkX's iteration order is insertion-order-dependent, which would
    otherwise make the hash flap between two pushes that describe the exact
    same graph, defeating the idempotency-key's purpose.
    """
    nodes = []
    for node_id, _data in graph.nodes(data=True):
        node_type, node_key = schema.split_node_id(node_id)
        nodes.append({"node_type": node_type, "node_key": node_key})
    nodes.sort(key=lambda n: (n["node_type"], n["node_key"]))

    edges = []
    for source_id, target_id, data in graph.edges(data=True):
        source_type, source_key = schema.split_node_id(source_id)
        target_type, target_key = schema.split_node_id(target_id)
        edges.append(
            {
                "source_node_type": source_type,
                "source_node_key": source_key,
                "target_node_type": target_type,
                "target_node_key": target_key,
                "edge_type": data.get("edge_type"),
                "is_active": bool(data.get("is_active", True)),
            }
        )
    edges.sort(
        key=lambda e: (
            e["source_node_type"],
            e["source_node_key"],
            e["target_node_type"],
            e["target_node_key"],
            e["edge_type"],
        )
    )

    return {"nodes": nodes, "edges": edges}


def fetch_and_build_graph(changed_since: str | None = None) -> nx.DiGraph:
    """Convenience: fetch all three exports then build the graph.

    Sequential and short-circuiting: if any fetch fails, later fetches never
    run and build_graph() is never called -- there is no code path that can
    hand back a partial/incomplete graph. A failure is instead raised as
    GraphBuildIncompleteError, tagged with which export step failed, so
    upstream logging/alerting doesn't need to know httpx's exception
    hierarchy to tell the three export steps apart.
    """
    with observability.timed_stage(logger, "graph_build", changed_since=changed_since):
        try:
            with observability.timed_stage(logger, "fetch_ai_systems", changed_since=changed_since):
                ai_systems = fetch_ai_systems(changed_since=changed_since)
        except Exception as exc:
            raise GraphBuildIncompleteError("ai-systems", exc) from exc

        try:
            with observability.timed_stage(logger, "fetch_regulations_catalog", changed_since=changed_since):
                regulations_catalog = fetch_regulations_catalog(changed_since=changed_since)
        except Exception as exc:
            raise GraphBuildIncompleteError("regulations-catalog", exc) from exc

        try:
            with observability.timed_stage(logger, "fetch_jurisdictions", changed_since=changed_since):
                jurisdictions = fetch_jurisdictions(changed_since=changed_since)
        except Exception as exc:
            raise GraphBuildIncompleteError("jurisdictions", exc) from exc

        with observability.timed_stage(logger, "build_graph"):
            graph = build_graph(ai_systems, regulations_catalog, jurisdictions)
            observability.log_event(
                logger,
                logging.INFO,
                "build_graph.counts",
                node_count=graph.number_of_nodes(),
                edge_count=graph.number_of_edges(),
            )

    return graph
