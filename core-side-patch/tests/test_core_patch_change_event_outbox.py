# Patch for CompliVibe core (app.complivibe.in), lands on top of migration head 0175.
# Test-harness only (not part of the patch set shipped to core).
from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from change_event_outbox import WATCHED_AI_SYSTEM_FIELDS, GovernanceGraphChangeEvent, emit_change_event
from models import Base


@pytest.fixture()
def session():
    engine = sa.create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[GovernanceGraphChangeEvent.__table__])
    s = Session(engine)
    yield s
    s.close()


def test_watched_fields_are_exactly_the_three_named_in_patent_md():
    assert {"deployment_jurisdiction", "data_categories", "risk_tier"} == WATCHED_AI_SYSTEM_FIELDS


def test_emit_change_event_writes_a_row(session):
    event = emit_change_event(session, ai_system_id="sys-alpha", changed_field="risk_tier", org_id=1)
    session.commit()

    assert event.id is not None
    assert event.consumed_at is None
    row = session.query(GovernanceGraphChangeEvent).one()
    assert row.ai_system_id == "sys-alpha"
    assert row.changed_field == "risk_tier"
    assert row.org_id == 1


def test_emit_change_event_rejects_unwatched_field(session):
    with pytest.raises(ValueError):
        emit_change_event(session, ai_system_id="sys-alpha", changed_field="name", org_id=1)


def test_emit_change_event_requires_org_id(session):
    with pytest.raises(ValueError):
        emit_change_event(session, ai_system_id="sys-alpha", changed_field="risk_tier")
