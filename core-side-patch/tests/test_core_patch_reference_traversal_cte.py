# Patch for CompliVibe core (app.complivibe.in), lands on top of migration head 0175.
# Test-harness only (not part of the patch set shipped to core).
#
# Cross-checks core-side-patch/reference_traversal_cte.py against the shared
# fixture contract (tests/fixtures/sample_export.py, reference_cte.py,
# expected_traversal.py) that every P2 workstream builds against -- this is
# the "byte-for-byte-comparable" integration checkpoint from
# CLAUDE_CODE_GOAL_PROMPT.md, played from core's side.
#
# IMPORTANT: reference_traversal_cte.py itself (the shipped patch file) has
# ZERO import on src/p2_satellite or tests/fixtures -- only THIS TEST FILE
# does, and only because this satellite-only repo happens to colocate both
# "core" and "satellite" code for demonstration purposes. A real core repo
# would have its own fixtures for this; the point being tested (algorithmic
# equivalence of the reference CTE across implementations) is the same either
# way.
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from tests.fixtures.expected_traversal import EXPECTED, MIN_DEPTH_FOR_FULL_RESULT
from tests.fixtures.reference_cte import _build_node_edge_rows, reference_derive_obligations
from tests.fixtures.sample_export import AI_SYSTEMS_EXPORT

from models import Base, GovernanceGraphEdge, GovernanceGraphNode
from reference_traversal_cte import REFERENCE_CTE_SQL_POSTGRES, derive_obligations_reference


def test_reference_cte_sql_is_verbatim_from_patent_md():
    # Spot-check the literal Postgres syntax elements PATENT.md's traversal
    # algorithm section specifies -- this string must not be "cleaned up".
    assert "WITH RECURSIVE obligation_graph AS" in REFERENCE_CTE_SQL_POSTGRES
    assert "ARRAY[source_node_id] as path" in REFERENCE_CTE_SQL_POSTGRES
    assert "NOT (e.target_node_id = ANY(og.path))" in REFERENCE_CTE_SQL_POSTGRES
    assert "og.depth < :max_traversal_depth" in REFERENCE_CTE_SQL_POSTGRES
    assert "node_type IN ('obligation', 'control_type')" in REFERENCE_CTE_SQL_POSTGRES


def _populate_sqlite_session_from_fixture() -> tuple[Session, dict[str, int]]:
    """Load tests/fixtures/reference_cte.py's node/edge rows (built from
    tests/fixtures/sample_export.py) into an in-memory SQLite DB via the
    core-side-patch ORM models, and return a session plus a node_id ->
    surrogate integer id map (GovernanceGraphNode.id is autoincrement, but the
    fixture's node ids are strings like 'ai_system:sys-alpha')."""
    engine = sa.create_engine("sqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine, tables=[GovernanceGraphNode.__table__, GovernanceGraphEdge.__table__])
    session = Session(engine)

    nodes, edges = _build_node_edge_rows()
    string_id_to_pk: dict[str, int] = {}
    for string_id, node_type, node_key in nodes:
        row = GovernanceGraphNode(org_id=1, node_type=node_type, node_key=node_key, properties={})
        session.add(row)
        session.flush()
        string_id_to_pk[string_id] = row.id

    for source_string_id, target_string_id, edge_type, is_active in edges:
        session.add(
            GovernanceGraphEdge(
                org_id=1,
                source_node_id=string_id_to_pk[source_string_id],
                target_node_id=string_id_to_pk[target_string_id],
                edge_type=edge_type,
                is_active=bool(is_active),
            )
        )
    session.commit()
    return session, string_id_to_pk


def test_pure_python_fallback_matches_hand_computed_expected_traversal():
    session, string_id_to_pk = _populate_sqlite_session_from_fixture()
    try:
        for ai_system in AI_SYSTEMS_EXPORT["items"]:
            ai_system_key = ai_system["id"]
            ai_system_node_id = string_id_to_pk[f"ai_system:{ai_system_key}"]

            result = derive_obligations_reference(session, ai_system_node_id, max_traversal_depth=6)

            expected = EXPECTED[ai_system_key]
            assert result["derived_obligations"] == expected["derived_obligations"]
            assert result["derived_controls"] == expected["derived_controls"]
    finally:
        session.close()


def test_pure_python_fallback_matches_sqlite_json1_reference_implementation():
    """Cross-check core-side-patch's ORM/pure-Python fallback against
    tests/fixtures/reference_cte.py's independent SQLite/JSON1 port of the
    exact same CTE -- both must agree on every ai_system in the fixture."""
    session, string_id_to_pk = _populate_sqlite_session_from_fixture()
    try:
        for ai_system in AI_SYSTEMS_EXPORT["items"]:
            ai_system_key = ai_system["id"]
            ai_system_node_id = string_id_to_pk[f"ai_system:{ai_system_key}"]

            ours = derive_obligations_reference(session, ai_system_node_id, max_traversal_depth=6)
            theirs = reference_derive_obligations(ai_system_key, max_traversal_depth=6)

            assert ours["derived_obligations"] == theirs["derived_obligations"]
            assert ours["derived_controls"] == theirs["derived_controls"]
    finally:
        session.close()


def test_max_traversal_depth_is_load_bearing_not_a_magic_number():
    """A shallower cap than MIN_DEPTH_FOR_FULL_RESULT must truncate results --
    proves depth is a genuine, wired-through config value, not a number that
    happens to never bind (per PATENT.md CHANGE LOG)."""
    session, string_id_to_pk = _populate_sqlite_session_from_fixture()
    try:
        ai_system_node_id = string_id_to_pk["ai_system:sys-beta"]
        shallow_result = derive_obligations_reference(
            session, ai_system_node_id, max_traversal_depth=MIN_DEPTH_FOR_FULL_RESULT - 1
        )
        full_result = derive_obligations_reference(
            session, ai_system_node_id, max_traversal_depth=MIN_DEPTH_FOR_FULL_RESULT
        )
        full_count = len(full_result["derived_obligations"]) + len(full_result["derived_controls"])
        shallow_count = len(shallow_result["derived_obligations"]) + len(shallow_result["derived_controls"])
        assert shallow_count < full_count
    finally:
        session.close()
