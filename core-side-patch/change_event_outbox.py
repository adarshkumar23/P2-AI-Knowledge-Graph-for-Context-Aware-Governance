"""
Patch for CompliVibe core (app.complivibe.in), lands on top of migration head 0175.
Field names / existing-pattern assumptions below are best-effort from PATENT.md and
must be verified against the real ai_system model / AuditService / permission system
before merging -- see core-side-patch/ASSUMPTIONS.md.

Outbox-style change-event table + emitter for the P2 hybrid trigger's
event-triggered path (PATENT.md "HYBRID TRIGGER" (a)): when a watched
ai_system property changes, core writes one row here; the satellite's
export endpoints (routers/patent_exports_p2.py) filter on `changed_since`
against this table so the satellite can pull just the changed rows.

WE COULD NOT VERIFY whether core already has a generic outbox/change-event
pattern elsewhere in the codebase (297 tables, none of which we can inspect
from this satellite-only repo). This file is written AS IF no such pattern
exists yet. Before merging, a human MUST check for an existing outbox/event-
log table (e.g. something used by webhooks, search-indexing, or other
integrations) and, if one exists, reuse/extend it instead of adding a second
outbox mechanism -- see ASSUMPTIONS.md.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, Session

from models import Base, _big_int_pk_type

# Only these three ai_system columns are "watched" per PATENT.md's hybrid
# trigger definition ("event-triggered traversal when a watched ai_system
# property changes ... deployment_jurisdiction, data_categories, or
# risk_tier"). Deliberately NOT free-form -- emit_change_event() rejects
# anything else so this can't silently become a generic audit-everything log.
WATCHED_AI_SYSTEM_FIELDS = frozenset({"deployment_jurisdiction", "data_categories", "risk_tier"})

# Sentinel `changed_field` value for HUMAN/manually-triggered re-derivation
# requests (the customer-facing "sync this system now" endpoint, and manual
# graph-edge additions that affect an ai_system's derivation) -- see
# emit_manual_change_event() below. NOT one of WATCHED_AI_SYSTEM_FIELDS
# because it isn't a column-change event at all; it rides in the same
# governance_graph_change_events table/consumption path deliberately, per
# the "reuse the outbox pattern, don't build a second one" requirement (see
# ASSUMPTIONS.md).
MANUAL_TRIGGER_REASON = "manual_sync"


class GovernanceGraphChangeEvent(Base):
    """governance_graph_change_events -- NOT one of PATENT.md's three named
    "Core Database Tables"; this is Workstream A's own addition implementing
    the "internal change-event mechanism" required by
    CLAUDE_CODE_GOAL_PROMPT.md's Workstream A bullet 4. If core already has an
    outbox/event-log pattern, drop this table and point emit_change_event /
    the export routers' changed_since filter at that instead.
    """

    __tablename__ = "governance_graph_change_events"

    # See models.py's GovernanceGraphNode for why this explicit `Mapped[int]`
    # annotation is needed alongside the legacy `= sa.Column(...)` assignment
    # (the SQLAlchemy mypy plugin can't infer a type through
    # `_big_int_pk_type()`'s function call).
    id: Mapped[int] = sa.Column(_big_int_pk_type(), primary_key=True, autoincrement=True)
    org_id: Mapped[int] = sa.Column(sa.BigInteger, nullable=False)
    ai_system_id: Mapped[str] = sa.Column(sa.String(64), nullable=False)
    changed_field: Mapped[str] = sa.Column(sa.String(64), nullable=False)
    changed_at: Mapped[datetime] = sa.Column(sa.DateTime(timezone=True), nullable=False)
    consumed_at = sa.Column(sa.DateTime(timezone=True), nullable=True)

    __table_args__ = (
        sa.Index("ix_ggce_org_ai_system", "org_id", "ai_system_id"),
        sa.Index("ix_ggce_changed_at", "changed_at"),
    )


def _write_change_event(
    session: Session, org_id: Any, ai_system_id: Any, changed_field: str
) -> GovernanceGraphChangeEvent:
    event = GovernanceGraphChangeEvent(
        org_id=org_id,
        ai_system_id=str(ai_system_id),
        changed_field=changed_field,
        changed_at=datetime.now(UTC),
        consumed_at=None,
    )
    session.add(event)
    return event


def emit_change_event(
    session: Session,
    ai_system_id: Any,
    changed_field: str,
    org_id: Any | None = None,
) -> GovernanceGraphChangeEvent:
    """Write one outbox row recording that `changed_field` changed on this
    ai_system, for the satellite's event-triggered traversal path to pick up.

    TODO (must be done by a human on the core team, we have no access to the
    real code path here): wire a call to this function into wherever
    ai_system.deployment_jurisdiction / data_categories / risk_tier actually
    get updated in the real core codebase -- e.g. a SQLAlchemy
    `@sa.event.listens_for(AiSystem, "after_update")` listener that diffs
    `sa.inspect(ai_system).attrs.<field>.history` for EXACTLY these three
    columns and calls emit_change_event once per changed watched column. Do
    NOT fire this on any other column change -- see WATCHED_AI_SYSTEM_FIELDS.
    org_id is a required column but is not resolvable from this stub alone;
    the real wiring must supply the ai_system's org_id from the ORM instance
    being updated.
    """
    if changed_field not in WATCHED_AI_SYSTEM_FIELDS:
        raise ValueError(
            f"emit_change_event called with unwatched field {changed_field!r}; "
            f"only {sorted(WATCHED_AI_SYSTEM_FIELDS)} should ever trigger a change event"
        )
    if org_id is None:
        raise ValueError("emit_change_event requires org_id -- see TODO in this function's docstring")

    return _write_change_event(session, org_id, ai_system_id, changed_field)


def emit_manual_change_event(
    session: Session,
    org_id: Any,
    ai_system_id: Any,
    reason: str = MANUAL_TRIGGER_REASON,
) -> GovernanceGraphChangeEvent:
    """Write one outbox row for a HUMAN/manually-triggered re-derivation
    request -- the customer-facing "sync this system now" endpoint
    (POST .../systems/{id}/sync), and manual graph-edge additions
    (POST .../edges) that affect an already-derived ai_system.

    Deliberately NOT gated by WATCHED_AI_SYSTEM_FIELDS -- that guard exists
    to stop the automatic column-watcher (emit_change_event, wired per the
    TODO above) from becoming a generic audit-everything log. A manual
    trigger is an explicit, human-initiated action, not a column diff, so it
    has no "field" to validate against that allowlist. Writes into the exact
    same governance_graph_change_events table, consumed by the exact same
    changed_since-filtered export path, per the "reuse the outbox pattern,
    don't build a second one" requirement -- see ASSUMPTIONS.md.
    """
    return _write_change_event(session, org_id, ai_system_id, reason)
