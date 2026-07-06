"""
Integration checkpoint 2 (CLAUDE_CODE_GOAL_PROMPT.md): "run an end-to-end dry
run -- satellite pulls from a local/mock core export, derives, pushes to a
local/mock core ingest, confirm validation and audit logging fire correctly."

This is deliberately NOT a mocked/monkeypatched call graph. It boots a real
uvicorn server hosting Workstream A's actual FastAPI routers
(routers/patent_exports_p2.py + routers/patent_ingest_p2.py, wired with
in-memory stand-ins for its DB/data-source dependencies) on localhost, points
the satellite's real settings.core_base_url at it, and then drives the actual
production call path:

    src.p2_satellite.graph_builder.fetch_and_build_graph()   (Workstream B)
    src.p2_satellite.traversal.derive_obligations()          (Workstream C)
    src.p2_satellite.ingest_client.push_derivation()         (Workstream D)

over real HTTP, against the real core-side-patch validation contract
(Workstream A). If satellite and core disagree about the traversal result,
this test fails with validation_status="flagged_mismatch" -- that's the
"Satellites Compute, Core Decides" contract actually firing, end to end.
"""

from __future__ import annotations

import dataclasses
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
import sqlalchemy as sa
import uvicorn
from fastapi import FastAPI
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

CORE_SIDE_PATCH_DIR = Path(__file__).resolve().parent.parent.parent / "core-side-patch"
if str(CORE_SIDE_PATCH_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_SIDE_PATCH_DIR))

from routers import patent_exports_p2, patent_ingest_p2  # noqa: E402

from src.p2_satellite import graph_builder, ingest_client, schema, traversal  # noqa: E402
from src.p2_satellite.config import settings as satellite_settings  # noqa: E402
from tests.fixtures.reference_cte import _build_node_edge_rows  # noqa: E402
from tests.fixtures.sample_export import (  # noqa: E402
    AI_SYSTEMS_EXPORT,
    JURISDICTIONS_EXPORT,
    REGULATIONS_CATALOG_EXPORT,
)

import dependencies  # noqa: E402  (core-side-patch flat module, see conftest pattern)
from audit_service_stub import AuditService  # noqa: E402
from data_providers import FixtureBackedExportDataSource  # noqa: E402
from models import (  # noqa: E402
    AiSystemObligationLink,
    Base,
    GovernanceGraphEdge,
    GovernanceGraphNode,
    GovernanceGraphTraversalResult,
)

DRY_RUN_PORT = 18765
DRY_RUN_BASE_URL = f"http://127.0.0.1:{DRY_RUN_PORT}"


def _build_seeded_session() -> Session:
    """Seed governance_graph_nodes/edges from the exact same fixture graph
    used by tests/unit/test_traversal.py and core-side-patch's own ingest
    tests, so core's re-validation catalog lookup + reference re-derivation
    have real data to check the satellite's submission against."""
    engine = sa.create_engine("sqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(
        engine,
        tables=[
            GovernanceGraphNode.__table__,
            GovernanceGraphEdge.__table__,
            GovernanceGraphTraversalResult.__table__,
            AiSystemObligationLink.__table__,
        ],
    )
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
    return session


def _build_mock_core_app(session: Session) -> FastAPI:
    app = FastAPI()
    app.include_router(patent_exports_p2.router)
    app.include_router(patent_ingest_p2.router)

    data_source = FixtureBackedExportDataSource(
        ai_systems=list(AI_SYSTEMS_EXPORT["items"]),
        regulations_catalog=REGULATIONS_CATALOG_EXPORT,
        jurisdictions=list(JURISDICTIONS_EXPORT["items"]),
    )
    app.dependency_overrides[dependencies.get_db_session] = lambda: session
    app.dependency_overrides[patent_exports_p2.get_export_data_source] = lambda: data_source
    return app


class _MockCoreServerThread(threading.Thread):
    def __init__(self, app: FastAPI, host: str, port: int) -> None:
        super().__init__(daemon=True)
        self._config = uvicorn.Config(app, host=host, port=port, log_level="warning")
        self.server = uvicorn.Server(self._config)

    def run(self) -> None:
        self.server.run()

    def stop(self) -> None:
        self.server.should_exit = True


def _wait_until_up(base_url: str, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            httpx.get(f"{base_url}/api/v1/patent-exports/p2/jurisdictions", timeout=0.5)
            return
        except httpx.TransportError as exc:
            last_exc = exc
            time.sleep(0.05)
    raise RuntimeError(f"mock core server did not come up in time: {last_exc}")


@pytest.fixture()
def mock_core_server(monkeypatch):
    AuditService._reset_for_tests()
    session = _build_seeded_session()
    app = _build_mock_core_app(session)

    thread = _MockCoreServerThread(app, host="127.0.0.1", port=DRY_RUN_PORT)
    thread.start()
    try:
        _wait_until_up(DRY_RUN_BASE_URL)

        # Point the satellite's real settings.core_base_url + scoped keys at
        # this mock server, in every module that already bound `settings` by
        # reference at import time (settings is a frozen dataclass singleton).
        patched_settings = dataclasses.replace(
            satellite_settings,
            core_base_url=DRY_RUN_BASE_URL,
            core_export_api_key="dev-export-key",
            core_ingest_api_key="dev-ingest-key",
        )
        monkeypatch.setattr(graph_builder, "settings", patched_settings)
        monkeypatch.setattr(ingest_client, "settings", patched_settings)

        yield session
    finally:
        thread.stop()
        thread.join(timeout=5.0)


def test_satellite_pulls_derives_and_pushes_to_live_mock_core(mock_core_server):
    session = mock_core_server

    # 1. Satellite pulls from core's real (mock) export endpoints over HTTP.
    graph = graph_builder.fetch_and_build_graph()
    ai_system_node_id = schema.node_id(schema.NODE_AI_SYSTEM, "sys-beta")
    assert ai_system_node_id in graph

    # 2. Satellite derives obligations locally via NetworkX traversal.
    derivation = traversal.derive_obligations(graph, ai_system_node_id)
    assert derivation["derived_obligations"]  # non-empty: sys-beta is the joint EU-India case

    # 3. Satellite pushes the derivation to core's real (mock) ingest endpoint.
    response = ingest_client.push_derivation(derivation, trigger_reason="event")

    # 4. Core independently re-validated and re-derived -- since traversal.py
    # is cross-checked byte-for-byte against the reference CTE (Workstream C's
    # tests), core's independent re-derivation must agree, i.e. NOT be flagged.
    assert response["validation_status"] == "validated"
    assert sorted(response["reference_derived_obligations"]) == sorted(derivation["derived_obligations"])
    assert sorted(response["reference_derived_controls"]) == sorted(derivation["derived_controls"])

    # 5. Core wrote the validated links (not withheld, as it would be on a mismatch).
    obligation_links = {
        row.obligation_id
        for row in session.query(AiSystemObligationLink).filter_by(ai_system_id="sys-beta")
        if row.obligation_id is not None
    }
    assert obligation_links == set(derivation["derived_obligations"])

    # 6. Core wrote exactly one traversal_result row, validated, tagged event-triggered.
    traversal_rows = session.query(GovernanceGraphTraversalResult).filter_by(ai_system_id="sys-beta").all()
    assert len(traversal_rows) == 1
    assert traversal_rows[0].validation_status == "validated"
    assert traversal_rows[0].trigger_reason == "event"

    # 7. Core fired exactly one audit log entry for this ingest, with the
    # required fields (PATENT.md "Everything derived is auditable").
    assert len(AuditService._written) == 1
    audit_entry = AuditService._written[0]
    assert audit_entry.payload["validation_status"] == "validated"
    assert audit_entry.payload["trigger_reason"] == "event"
    assert audit_entry.payload["methodology_version"] == derivation["methodology_version"]


def test_repushing_identical_derivation_is_idempotency_safe(mock_core_server):
    """Re-pushing the same derivation (e.g. a safety-net poll re-deriving a
    system whose graph neighborhood hasn't changed) must compute the same
    derivation_hash both times -- the actual dedupe-on-hash write path is
    core's responsibility (see ingest_client.py docstring), so this test
    checks the satellite-side half of the contract: the hash is stable."""
    graph = graph_builder.fetch_and_build_graph()
    ai_system_node_id = schema.node_id(schema.NODE_AI_SYSTEM, "sys-alpha")
    derivation = traversal.derive_obligations(graph, ai_system_node_id)

    hash_1 = ingest_client.compute_derivation_hash(derivation)
    hash_2 = ingest_client.compute_derivation_hash(derivation)
    assert hash_1 == hash_2

    response_1 = ingest_client.push_derivation(derivation, trigger_reason="event")
    response_2 = ingest_client.push_derivation(derivation, trigger_reason="scheduled")
    assert response_1["validation_status"] == "validated"
    assert response_2["validation_status"] == "validated"


def test_satellite_pushes_graph_structure_to_live_mock_core(mock_core_server):
    """Item 22 (ASSUMPTIONS.md): the satellite is the sole source of truth
    for governance_graph_nodes/edges, pushing its whole built graph over
    real HTTP to core's POST /api/v1/patent-ingest/p2/graph-structure. This
    is the same router (patent_ingest_p2) already mounted on the mock core
    server above, so this exercises the real upsert path end to end, not
    just core-side-patch's own TestClient-level tests.

    The mock session is pre-seeded (_build_seeded_session) from the exact
    same fixture data (tests/fixtures/reference_cte.py's _build_node_edge_rows,
    which mirrors graph_builder.build_graph() node-for-node/edge-for-edge) --
    so pushing the satellite's serialized structure should find every
    node/edge ALREADY present by natural key and create nothing, proving the
    satellite's own serialization reconciles with independently-constructed
    identical graph data without duplicating a single row.
    """
    session = mock_core_server
    graph = graph_builder.fetch_and_build_graph()
    structure = graph_builder.serialize_graph_structure(graph)

    node_count_before = session.query(GovernanceGraphNode).filter_by(org_id=1).count()
    edge_count_before = session.query(GovernanceGraphEdge).filter_by(org_id=1).count()
    assert node_count_before > 0 and edge_count_before > 0  # sanity: the seed actually has rows

    response_1 = ingest_client.push_graph_structure(structure)
    assert response_1["nodes_created"] == 0  # already seeded under the same natural keys
    assert response_1["edges_created"] == 0
    assert session.query(GovernanceGraphNode).filter_by(org_id=1).count() == node_count_before
    assert session.query(GovernanceGraphEdge).filter_by(org_id=1).count() == edge_count_before

    # Re-pushing again (e.g. the next safety-net poll cycle) must stay a
    # pure no-op -- never duplicate rows on repeat pushes of an unchanged graph.
    response_2 = ingest_client.push_graph_structure(structure)
    assert response_2["nodes_created"] == 0
    assert response_2["edges_created"] == 0
    assert session.query(GovernanceGraphNode).filter_by(org_id=1).count() == node_count_before
    assert session.query(GovernanceGraphEdge).filter_by(org_id=1).count() == edge_count_before
