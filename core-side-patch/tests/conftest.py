# Patch for CompliVibe core (app.complivibe.in), lands on top of migration head 0175.
# Test-harness only (not part of the patch set shipped to core). Puts
# core-side-patch/ itself on sys.path so the patch-set modules (permissions.py,
# models.py, validation.py, reference_traversal_cte.py, change_event_outbox.py,
# dependencies.py, data_providers.py, audit_service_stub.py, routers/*) are
# importable as flat top-level modules -- core-side-patch/ is not a normal
# dotted-importable package name (it contains a hyphen), and since these files
# are meant to be individually dropped into core's real package structure at
# merge time anyway, they intentionally use flat sibling imports rather than
# relative-package imports.
from __future__ import annotations

import sys
from pathlib import Path

CORE_SIDE_PATCH_DIR = Path(__file__).resolve().parent.parent
if str(CORE_SIDE_PATCH_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_SIDE_PATCH_DIR))


def build_populated_session(org_id: int = 1):
    """Shared test-only helper: an in-memory SQLite Session pre-populated
    with tests/fixtures/reference_cte.py's sys-alpha/sys-beta fixture graph,
    tagged to `org_id`. Parameterized by org_id (unlike
    test_core_patch_ingest_router.py's local, org-1-only
    `_build_populated_session`) so tests that need TWO distinct orgs'
    worth of graph rows (org-scoping enforcement tests for the customer-
    facing knowledge-graph endpoints, routers/patent_knowledge_graph_p2.py)
    can call this twice against the SAME in-memory engine/session and get
    two physically distinct sets of node/edge rows sharing the same
    abstract shape -- node ids are globally unique autoincrement PKs, so
    org 1's and org 2's rows never collide even though both are built from
    the same fixture.

    Returns (engine, session, string_id_to_pk) -- the third element maps the
    fixture's business ids (e.g. "control_type:access_control") to this
    org's real integer PKs, so a test can look up a specific node's PK
    without re-deriving it. For a second org's worth of rows against the
    SAME session, call seed_org_graph(session, other_org_id) directly.
    """
    import sqlalchemy as sa
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import StaticPool

    from change_event_outbox import GovernanceGraphChangeEvent
    from models import (
        AiSystemObligationLink,
        Base,
        GovernanceGraphEdge,
        GovernanceGraphNode,
        GovernanceGraphTraversalResult,
    )

    engine = sa.create_engine("sqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(
        engine,
        tables=[
            GovernanceGraphNode.__table__,
            GovernanceGraphEdge.__table__,
            GovernanceGraphTraversalResult.__table__,
            AiSystemObligationLink.__table__,
            GovernanceGraphChangeEvent.__table__,
        ],
    )
    session = Session(engine)
    string_id_to_pk = seed_org_graph(session, org_id)
    return engine, session, string_id_to_pk


def seed_org_graph(session, org_id: int) -> dict[str, int]:
    """Seed one org's worth of the sys-alpha/sys-beta fixture graph into an
    already-open session (does not create tables or commit) -- lets a test
    call this twice, once per org_id, against one shared session/engine.
    Returns the string-id -> real PK mapping for that org's rows."""
    from tests.fixtures.reference_cte import _build_node_edge_rows

    from models import GovernanceGraphEdge, GovernanceGraphNode

    nodes, edges = _build_node_edge_rows()
    string_id_to_pk: dict[str, int] = {}
    for string_id, node_type, node_key in nodes:
        row = GovernanceGraphNode(org_id=org_id, node_type=node_type, node_key=node_key, properties={})
        session.add(row)
        session.flush()
        string_id_to_pk[string_id] = row.id

    for source_string_id, target_string_id, edge_type, is_active in edges:
        session.add(
            GovernanceGraphEdge(
                org_id=org_id,
                source_node_id=string_id_to_pk[source_string_id],
                target_node_id=string_id_to_pk[target_string_id],
                edge_type=edge_type,
                is_active=bool(is_active),
            )
        )
    session.commit()
    return string_id_to_pk
