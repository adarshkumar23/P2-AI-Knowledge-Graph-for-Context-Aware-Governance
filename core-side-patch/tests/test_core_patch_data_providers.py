# Patch for CompliVibe core (app.complivibe.in), lands on top of migration head 0175.
# Test-harness only (not part of the patch set shipped to core).
#
# Exercises the changed_since -> governance_graph_change_events join pattern in
# SQLAlchemyExportDataSource against the ASSUMED placeholder ai_system model
# (see data_providers.py's module docstring: field/table names are unverified,
# only the *join pattern* is being proven here) plus the simple
# FixtureBackedExportDataSource used by the router tests.
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from change_event_outbox import GovernanceGraphChangeEvent
from data_providers import FixtureBackedExportDataSource, SQLAlchemyExportDataSource, _AssumedAiSystem
from models import Base


def _make_session():
    engine = sa.create_engine("sqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine, tables=[_AssumedAiSystem.__table__, GovernanceGraphChangeEvent.__table__])
    return Session(engine)


def test_sqlalchemy_export_data_source_changed_since_join_filters_correctly():
    session = _make_session()
    now = datetime.now(UTC)
    old = now - timedelta(days=10)

    session.add_all(
        [
            _AssumedAiSystem(
                id="sys-alpha",
                org_id=1,
                name="Alpha",
                geographic_scope=["EU"],
                data_categories=["personal"],
                risk_tier="limited",
                deployment_status="active",
                updated_at=old,
            ),
            _AssumedAiSystem(
                id="sys-beta",
                org_id=1,
                name="Beta",
                geographic_scope=["EU", "IN"],
                data_categories=["biometric", "health"],
                risk_tier="high",
                deployment_status="active",
                updated_at=now,
            ),
        ]
    )
    # Only sys-beta has a recent change event.
    session.add(
        GovernanceGraphChangeEvent(
            org_id=1, ai_system_id="sys-beta", changed_field="risk_tier", changed_at=now, consumed_at=None
        )
    )
    session.commit()

    source = SQLAlchemyExportDataSource(session, regulations_catalog={"items": []}, jurisdictions=[])

    all_items = source.list_ai_systems(org_id=1, changed_since=None)
    assert {item["id"] for item in all_items} == {"sys-alpha", "sys-beta"}

    since_yesterday = now - timedelta(days=1)
    recent_items = source.list_ai_systems(org_id=1, changed_since=since_yesterday)
    assert {item["id"] for item in recent_items} == {"sys-beta"}

    session.close()


def test_sqlalchemy_export_data_source_only_returns_graph_relevant_fields():
    session = _make_session()
    session.add(
        _AssumedAiSystem(
            id="sys-alpha",
            org_id=1,
            name="Alpha",
            geographic_scope=["EU"],
            data_categories=["personal"],
            risk_tier="limited",
            deployment_status="active",
            updated_at=datetime.now(UTC),
        )
    )
    session.commit()

    source = SQLAlchemyExportDataSource(session, regulations_catalog={"items": []}, jurisdictions=[])
    items = source.list_ai_systems(org_id=1, changed_since=None)

    assert set(items[0].keys()) == {
        "id",
        "name",
        "geographic_scope",
        "data_categories",
        "risk_tier",
        "deployment_status",
    }
    session.close()


def test_fixture_backed_export_data_source_roundtrips_sample_export_shape():
    from tests.fixtures.sample_export import AI_SYSTEMS_EXPORT, JURISDICTIONS_EXPORT, REGULATIONS_CATALOG_EXPORT

    source = FixtureBackedExportDataSource(
        ai_systems=AI_SYSTEMS_EXPORT["items"],
        regulations_catalog=REGULATIONS_CATALOG_EXPORT,
        jurisdictions=JURISDICTIONS_EXPORT["items"],
    )

    assert source.list_ai_systems(org_id=1, changed_since=None) == AI_SYSTEMS_EXPORT["items"]
    assert source.list_regulations_catalog(org_id=1, changed_since=None) == REGULATIONS_CATALOG_EXPORT
    assert source.list_jurisdictions(org_id=1, changed_since=None) == JURISDICTIONS_EXPORT["items"]
