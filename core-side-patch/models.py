"""
Patch for CompliVibe core (app.complivibe.in), lands on top of migration head 0175.
Field names / existing-pattern assumptions below are best-effort from PATENT.md and
must be verified against the real ai_system model / AuditService / permission system
before merging -- see core-side-patch/ASSUMPTIONS.md.

SQLAlchemy ORM models for the three new governance-graph tables (per PATENT.md's
"Core Database Tables" section) plus query/write helpers used by
routers/patent_exports_p2.py and routers/patent_ingest_p2.py.

These models are NOT the migration itself. migrations/0176_add_governance_graph_tables.py
hand-writes the DDL the way real Alembic scripts in mature codebases usually do
(so the migration keeps working even if this ORM layer is refactored later); the
column definitions here are kept in sync with that migration BY HAND. A human
merging this patch should double check the two haven't drifted.

Also includes a placeholder ORM mapping for `ai_system_obligation_links`, a table
this patch assumes ALREADY EXISTS in the real core (we do not create it in the
migration) -- its real column names are unverified, see ASSUMPTIONS.md.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.orm import Mapped, Session, declarative_base

Base = declarative_base()


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _big_int_pk_type():
    """BigInteger everywhere except SQLite, where a BigInteger primary key
    column doesn't get SQLite's ROWID-aliased autoincrement behavior (only a
    column whose type affinity is exactly INTEGER does). This repo's own
    tests run against SQLite (no live Postgres available -- see
    ASSUMPTIONS.md); production Postgres still gets a real BIGINT column."""
    return sa.BigInteger().with_variant(sa.Integer(), "sqlite")


class GovernanceGraphNode(Base):
    """governance_graph_nodes -- see PATENT.md 'Core Database Tables'."""

    __tablename__ = "governance_graph_nodes"

    # Explicit `Mapped[...]` left-hand annotations below (alongside the legacy
    # `= sa.Column(...)` right-hand assignment) are type-checking-only -- they
    # don't change runtime behavior, SQLAlchemy 2.0's documented way to add
    # static types to pre-2.0-style declarative columns. Added where the
    # SQLAlchemy mypy plugin couldn't infer a type on its own (id's type comes
    # from a function call, `_big_int_pk_type()`, not a literal column type;
    # embedding's from the third-party pgvector `Vector` type) -- see
    # pyproject.toml's mypy config.
    id: Mapped[int] = sa.Column(_big_int_pk_type(), primary_key=True, autoincrement=True)
    org_id: Mapped[int] = sa.Column(sa.BigInteger, nullable=False)
    node_type: Mapped[str] = sa.Column(sa.String(64), nullable=False)
    node_key: Mapped[str] = sa.Column(sa.String(255), nullable=False)
    properties: Mapped[dict] = sa.Column(sa.JSON, nullable=False, default=dict)
    # 384-dim to match sentence-transformers all-MiniLM-L6-v2 used satellite-side
    # (src/p2_satellite/config.py EMBEDDING_DIM). Requires the pgvector Postgres
    # extension -- see migration upgrade().
    embedding: Mapped[list[float] | None] = sa.Column(Vector(384), nullable=True)
    created_at: Mapped[datetime] = sa.Column(sa.DateTime(timezone=True), nullable=False, default=_utcnow)
    archived: Mapped[bool] = sa.Column(sa.Boolean, nullable=False, default=False)

    __table_args__ = (
        sa.Index("ix_governance_graph_nodes_org_id", "org_id"),
        sa.Index("ix_governance_graph_nodes_org_node_type", "org_id", "node_type"),
        # ASSUMPTION (not in PATENT.md literally): one node per (org, type, key)
        # so re-ingesting the same export never duplicates nodes. Judgment call --
        # verify this is what core actually wants before merge. See ASSUMPTIONS.md.
        sa.UniqueConstraint("org_id", "node_type", "node_key", name="uq_governance_graph_nodes_org_type_key"),
    )


class GovernanceGraphEdge(Base):
    """governance_graph_edges -- see PATENT.md 'Core Database Tables'."""

    __tablename__ = "governance_graph_edges"

    id: Mapped[int] = sa.Column(_big_int_pk_type(), primary_key=True, autoincrement=True)
    org_id: Mapped[int] = sa.Column(sa.BigInteger, nullable=False)
    source_node_id: Mapped[int] = sa.Column(
        sa.BigInteger, sa.ForeignKey("governance_graph_nodes.id", ondelete="CASCADE"), nullable=False
    )
    target_node_id: Mapped[int] = sa.Column(
        sa.BigInteger, sa.ForeignKey("governance_graph_nodes.id", ondelete="CASCADE"), nullable=False
    )
    edge_type: Mapped[str] = sa.Column(sa.String(64), nullable=False)
    weight = sa.Column(sa.Float, nullable=False, default=1.0)
    properties: Mapped[dict] = sa.Column(sa.JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = sa.Column(sa.DateTime(timezone=True), nullable=False, default=_utcnow)
    is_active: Mapped[bool] = sa.Column(sa.Boolean, nullable=False, default=True)

    __table_args__ = (
        sa.Index("ix_governance_graph_edges_org_id", "org_id"),
        sa.Index("ix_governance_graph_edges_org_source", "org_id", "source_node_id"),
        sa.Index("ix_governance_graph_edges_edge_type", "edge_type"),
    )


class GovernanceGraphTraversalResult(Base):
    """governance_graph_traversal_results -- see PATENT.md 'Core Database Tables'."""

    __tablename__ = "governance_graph_traversal_results"

    id: Mapped[int] = sa.Column(_big_int_pk_type(), primary_key=True, autoincrement=True)
    org_id: Mapped[int] = sa.Column(sa.BigInteger, nullable=False)
    # ASSUMPTION: ai_system_id stored as-is (string-compatible) -- see
    # ASSUMPTIONS.md re: whether the real ai_system.id is an integer PK.
    ai_system_id: Mapped[str] = sa.Column(sa.String(64), nullable=False)
    traversal_at: Mapped[datetime] = sa.Column(sa.DateTime(timezone=True), nullable=False, default=_utcnow)
    input_context: Mapped[dict] = sa.Column(sa.JSON, nullable=False, default=dict)
    derived_obligations: Mapped[list] = sa.Column(sa.JSON, nullable=False, default=list)
    derived_controls: Mapped[list] = sa.Column(sa.JSON, nullable=False, default=list)
    graph_path = sa.Column(sa.JSON, nullable=True)
    methodology_version: Mapped[str] = sa.Column(sa.String(32), nullable=False)
    trigger_reason: Mapped[str] = sa.Column(sa.String(16), nullable=False)
    validation_status: Mapped[str] = sa.Column(sa.String(32), nullable=False)

    __table_args__ = (
        sa.Index("ix_ggtr_org_id", "org_id"),
        sa.Index("ix_ggtr_org_ai_system", "org_id", "ai_system_id"),
    )


class AiSystemObligationLink(Base):
    """
    ASSUMED SCHEMA -- this table is assumed to already exist in the real core
    (it is NOT created by migrations/0176_add_governance_graph_tables.py). Real
    column names are unverified; see ASSUMPTIONS.md. Modeled here only so the
    ingest router's write path is independently testable in this repo.
    """

    __tablename__ = "ai_system_obligation_links"

    id: Mapped[int] = sa.Column(_big_int_pk_type(), primary_key=True, autoincrement=True)
    org_id: Mapped[int] = sa.Column(sa.BigInteger, nullable=False)
    ai_system_id: Mapped[str] = sa.Column(sa.String(64), nullable=False)
    # Exactly one of obligation_id / control_type_id is populated per row -- see
    # upsert_ai_system_obligation_links() docstring for why (payload carries two
    # separate flat lists, not paired obligation->control rows).
    obligation_id = sa.Column(sa.String(255), nullable=True)
    control_type_id = sa.Column(sa.String(255), nullable=True)
    created_at = sa.Column(sa.DateTime(timezone=True), nullable=False, default=_utcnow)


# --------------------------------------------------------------------------
# Query / write helpers used by the routers
# --------------------------------------------------------------------------


def load_active_catalog(session: Session, org_id: Any) -> dict[str, set[str]]:
    """Active (archived=false) obligation/control_type node_keys for one org.

    This is the "live catalog" referenced in PATENT.md's "Satellites Compute,
    Core Decides" step 1 -- used by validation.validate_obligation_control_ids.
    """
    rows = (
        session.query(GovernanceGraphNode.node_type, GovernanceGraphNode.node_key)
        .filter(
            GovernanceGraphNode.org_id == org_id,
            GovernanceGraphNode.node_type.in_(["obligation", "control_type"]),
            GovernanceGraphNode.archived.is_(False),
        )
        .all()
    )
    catalog: dict[str, set[str]] = {"obligation": set(), "control_type": set()}
    for node_type, node_key in rows:
        # node_type/node_key are `nullable=False` in the schema -- this guard
        # is defensive-only (satisfies the type checker's honest view of what
        # a Column *can* type as) rather than a condition expected at runtime.
        if node_type is None or node_key is None:
            continue
        catalog.setdefault(node_type, set()).add(node_key)
    return catalog


def get_node(session: Session, org_id: Any, node_id: Any) -> GovernanceGraphNode | None:
    """Fetch one governance_graph_nodes row, scoped to `org_id` and excluding
    archived nodes -- the org-scoping + dangling-reference check shared by
    the manual-edge-addition endpoint (routers/patent_knowledge_graph_p2.py
    Feature 3: reject edges referencing a node that doesn't exist OR belongs
    to a different org) and anything else that needs to validate a node_id
    a caller handed in.
    """
    return session.query(GovernanceGraphNode).filter_by(id=node_id, org_id=org_id, archived=False).one_or_none()


def create_manual_edge(
    session: Session,
    org_id: Any,
    source_node_id: Any,
    target_node_id: Any,
    edge_type: str,
    weight: float,
    properties: dict,
) -> GovernanceGraphEdge:
    """Insert one compliance-officer-authored edge (routers/patent_knowledge_graph_p2.py
    Feature 3). Callers MUST have already validated both node ids exist and
    belong to `org_id` (see get_node above) -- this helper does not re-check,
    matching upsert_ai_system_obligation_links' pattern of keeping DB-session
    concerns in models.py and validation/HTTP concerns in the router.

    `properties` is expected to already carry the
    {"source": "manual", "added_by": <user_id>} tag the task requires for
    traceability -- this helper just persists whatever dict it's given, it
    doesn't enforce the tag's presence (the router is the single place that
    constructs it, so there's only one place to keep in sync, not two).
    """
    edge = GovernanceGraphEdge(
        org_id=org_id,
        source_node_id=source_node_id,
        target_node_id=target_node_id,
        edge_type=edge_type,
        # SQLAlchemy mypy plugin infers sa.Float's Python-side type as its
        # internal numeric protocol ("_N"), which a plain `float` argument
        # doesn't structurally satisfy even though it's the correct runtime
        # type -- a known plugin-precision gap, not a real type mismatch.
        weight=weight,  # type: ignore[arg-type]
        properties=properties,
        is_active=True,
    )
    session.add(edge)
    return edge


def upsert_graph_structure(
    session: Session,
    org_id: Any,
    nodes: Iterable[Any],
    edges: Iterable[Any],
) -> dict[str, int]:
    """Upsert a full node/edge structure snapshot pushed by the satellite
    (POST .../graph-structure, routers/patent_ingest_p2.py) -- this is what
    closes ASSUMPTIONS.md item 22 ("nothing populates governance_graph_nodes/
    edges"): the satellite is now the sole source of truth for graph
    structure, pushing its whole freshly-built graph after every fetch.

    `nodes`/`edges` are any objects with the right attributes (in practice,
    routers/patent_ingest_p2.py's pydantic NodeStructureItem/EdgeStructureItem
    request-body items) -- this module stays pydantic-free by duck-typing on
    `.node_type`/`.node_key`/`.properties` and
    `.source_node_type`/`.source_node_key`/`.target_node_type`/
    `.target_node_key`/`.edge_type`/`.is_active`/`.weight`/`.properties`
    rather than importing the request models.

    Nodes are upserted by NATURAL key (org_id, node_type, node_key), reusing
    governance_graph_nodes' existing UNIQUE(org_id, node_type, node_key)
    constraint (migration 0176) as the matching key -- never a fresh id per
    push, so re-pushing an unchanged graph is a no-op and a changed one
    updates in place. An existing archived node is un-archived by a fresh
    push (the satellite pushing it again means it's back in the live
    export).

    Edges are upserted by an APPLICATION-level natural key
    (org_id, source_node_id, target_node_id, edge_type) -- there is
    currently no DB-level unique constraint enforcing this (see
    ASSUMPTIONS.md), so this is query-then-decide, the same dedup-then-write
    style upsert_ai_system_obligation_links already uses, not a real
    INSERT ... ON CONFLICT. Not safe under concurrent structure pushes for
    the same org as-is -- see ASSUMPTIONS.md.

    An edge referencing a (node_type, node_key) pair not present in THIS
    push's `nodes` list falls back to looking up an already-persisted node
    from a prior push before giving up and skipping that edge (logged
    nowhere yet -- see ASSUMPTIONS.md) -- defensive only; the satellite
    always pushes its whole graph in one call today (see
    src/p2_satellite/graph_builder.serialize_graph_structure), so this
    fallback path is not expected to be exercised in practice.

    Returns {"nodes_created", "nodes_updated", "edges_created", "edges_updated"}.
    """
    nodes_created = nodes_updated = 0
    node_id_by_key: dict[tuple[str, str], int] = {}

    for node in nodes:
        properties = dict(node.properties)
        existing = (
            session.query(GovernanceGraphNode)
            .filter_by(org_id=org_id, node_type=node.node_type, node_key=node.node_key)
            .one_or_none()
        )
        if existing is None:
            row = GovernanceGraphNode(
                org_id=org_id,
                node_type=node.node_type,
                node_key=node.node_key,
                properties=properties,
                archived=False,
            )
            session.add(row)
            session.flush()  # populate row.id for edges referencing this node in the same push
            node_id_by_key[(node.node_type, node.node_key)] = row.id
            nodes_created += 1
        else:
            changed = False
            if existing.properties != properties:
                existing.properties = properties
                changed = True
            if existing.archived:
                existing.archived = False
                changed = True
            if changed:
                nodes_updated += 1
            node_id_by_key[(node.node_type, node.node_key)] = existing.id

    def _resolve_node_id(node_type: str, node_key: str) -> int | None:
        key = (node_type, node_key)
        if key in node_id_by_key:
            return node_id_by_key[key]
        # Fallback: a node not in THIS push's node list but already
        # persisted from a prior push (see docstring).
        existing = get_node_by_natural_key(session, org_id, node_type, node_key)
        if existing is not None:
            node_id_by_key[key] = existing.id
            return existing.id
        return None

    edges_created = edges_updated = 0
    for edge in edges:
        source_id = _resolve_node_id(edge.source_node_type, edge.source_node_key)
        target_id = _resolve_node_id(edge.target_node_type, edge.target_node_key)
        if source_id is None or target_id is None:
            continue  # dangling reference within this push -- skip, see docstring

        edge_properties = dict(edge.properties)
        existing_edge = (
            session.query(GovernanceGraphEdge)
            .filter_by(org_id=org_id, source_node_id=source_id, target_node_id=target_id, edge_type=edge.edge_type)
            .one_or_none()
        )
        if existing_edge is None:
            session.add(
                GovernanceGraphEdge(
                    org_id=org_id,
                    source_node_id=source_id,
                    target_node_id=target_id,
                    edge_type=edge.edge_type,
                    weight=edge.weight,
                    properties=edge_properties,
                    is_active=edge.is_active,
                )
            )
            edges_created += 1
        else:
            changed = False
            if existing_edge.is_active != edge.is_active:
                existing_edge.is_active = edge.is_active
                changed = True
            if existing_edge.weight != edge.weight:
                existing_edge.weight = edge.weight
                changed = True
            if existing_edge.properties != edge_properties:
                existing_edge.properties = edge_properties
                changed = True
            if changed:
                edges_updated += 1

    return {
        "nodes_created": nodes_created,
        "nodes_updated": nodes_updated,
        "edges_created": edges_created,
        "edges_updated": edges_updated,
    }


def get_node_by_natural_key(session: Session, org_id: Any, node_type: str, node_key: str) -> GovernanceGraphNode | None:
    """Fetch one governance_graph_nodes row by its natural key (org_id,
    node_type, node_key) -- the upsert-matching key used by
    upsert_graph_structure above, and a convenience lookup for anything else
    that has a node's business key but not its integer PK."""
    return (
        session.query(GovernanceGraphNode)
        .filter_by(org_id=org_id, node_type=node_type, node_key=node_key, archived=False)
        .one_or_none()
    )


def resolve_ai_system_node_id(session: Session, org_id: Any, ai_system_id: Any) -> int | None:
    """Look up the governance_graph_nodes.id for this ai_system's graph node.

    ASSUMPTION: the ai_system node's node_key equals str(ai_system_id) -- i.e.
    graph_builder-equivalent core-side sync writes ai_system nodes keyed by the
    ai_system's own primary key/business key. See ASSUMPTIONS.md.
    """
    node = (
        session.query(GovernanceGraphNode)
        .filter_by(org_id=org_id, node_type="ai_system", node_key=str(ai_system_id), archived=False)
        .one_or_none()
    )
    return node.id if node is not None else None


def upsert_ai_system_obligation_links(
    session: Session,
    org_id: Any,
    ai_system_id: Any,
    derived_obligations: list[str],
    derived_controls: list[str],
) -> list[AiSystemObligationLink]:
    """Minimal upsert into the (assumed-schema) ai_system_obligation_links table.

    The ingest payload carries two independent flat lists (derived_obligations,
    derived_controls) rather than paired (obligation, control) tuples, and we
    don't know the real table's exact semantics -- so this writes one row per
    obligation (control_type_id left null) and one row per control (obligation_id
    left null), skipping rows that already exist for this (org, ai_system). A
    human must reconcile this against the real table shape before merge --
    see ASSUMPTIONS.md.
    """
    existing = {
        (row.obligation_id, row.control_type_id)
        for row in session.query(AiSystemObligationLink).filter_by(org_id=org_id, ai_system_id=str(ai_system_id))
    }
    to_add: list[AiSystemObligationLink] = []
    for obligation_id in derived_obligations:
        obligation_key: tuple[str | None, str | None] = (obligation_id, None)
        if obligation_key not in existing:
            to_add.append(
                AiSystemObligationLink(
                    org_id=org_id,
                    ai_system_id=str(ai_system_id),
                    obligation_id=obligation_id,
                    control_type_id=None,
                )
            )
    for control_type_id in derived_controls:
        key: tuple[str | None, str | None] = (None, control_type_id)
        if key not in existing:
            to_add.append(
                AiSystemObligationLink(
                    org_id=org_id,
                    ai_system_id=str(ai_system_id),
                    obligation_id=None,
                    control_type_id=control_type_id,
                )
            )
    session.add_all(to_add)
    return to_add
