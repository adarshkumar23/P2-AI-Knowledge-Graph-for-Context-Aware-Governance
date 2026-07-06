"""
Patch for CompliVibe core (app.complivibe.in), lands on top of migration head 0175.
Field names / existing-pattern assumptions below are best-effort from PATENT.md and
must be verified against the real ai_system model / AuditService / permission system
before merging -- see core-side-patch/ASSUMPTIONS.md.

Data-source abstraction behind the three read-only export endpoints
(routers/patent_exports_p2.py). Response SHAPE (which fields go out) is
specified precisely by PATENT.md / tests/fixtures/sample_export.py and is not
in question. WHERE that data actually comes from in the real core (which ORM
models, which columns, how changed_since joins against the outbox table) is
the unverified part -- we don't have access to the real ai_system / regulation
/ jurisdiction models, only PATENT.md's description of the fields the graph
needs.

`ExportDataSource` is the seam: routers depend on this abstract interface, not
on a concrete query implementation, so:
  - this repo's tests can supply a fixture-backed implementation (matching
    tests/fixtures/sample_export.py's shape) without touching real core tables
  - a human merging this patch swaps in a real implementation that queries the
    actual ai_system / regulation / jurisdiction tables, without changing the
    router code at all

`SQLAlchemyExportDataSource` below is a best-effort REFERENCE implementation
of that real query pattern (including the changed_since -> outbox join), built
against placeholder ORM models (`_AssumedAiSystem` etc.) that stand in for
core's real models. It is deliberately marked ASSUMED/PLACEHOLDER; see
ASSUMPTIONS.md. It is exercised by core-side-patch/tests/test_core_patch_data_
providers.py against an in-memory SQLite schema built from those placeholders,
so the changed_since/outbox-join *pattern* is genuinely tested even though the
real table/column names are not.
"""

from __future__ import annotations

import abc
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from change_event_outbox import GovernanceGraphChangeEvent
from models import Base


class ExportDataSource(abc.ABC):
    """Abstract seam behind the three /api/v1/patent-exports/p2/* endpoints."""

    @abc.abstractmethod
    def list_ai_systems(self, org_id: Any, changed_since: datetime | None) -> list[dict]: ...

    @abc.abstractmethod
    def list_regulations_catalog(self, org_id: Any, changed_since: datetime | None) -> dict: ...

    @abc.abstractmethod
    def list_jurisdictions(self, org_id: Any, changed_since: datetime | None) -> list[dict]: ...


# ---------------------------------------------------------------------------
# ASSUMED/PLACEHOLDER models standing in for core's real ai_system / regulation
# / jurisdiction tables. DO NOT merge these as new tables -- they exist purely
# so SQLAlchemyExportDataSource's changed_since/outbox-join query pattern is
# demonstrable and testable in this repo. Replace with imports of the real
# models when merging. See ASSUMPTIONS.md.
# ---------------------------------------------------------------------------


class _AssumedAiSystem(Base):
    __tablename__ = "_assumed_ai_system_placeholder"

    id = sa.Column(sa.String(64), primary_key=True)
    org_id = sa.Column(sa.BigInteger, nullable=False)
    name = sa.Column(sa.String(255), nullable=False)
    geographic_scope = sa.Column(sa.JSON, nullable=False, default=list)
    data_categories = sa.Column(sa.JSON, nullable=False, default=list)
    risk_tier = sa.Column(sa.String(32), nullable=False)
    deployment_status = sa.Column(sa.String(32), nullable=False)
    updated_at = sa.Column(sa.DateTime(timezone=True), nullable=False)


class SQLAlchemyExportDataSource(ExportDataSource):
    """
    Best-effort REFERENCE implementation of the real query pattern. Only
    list_ai_systems is implemented against a placeholder model (to prove the
    changed_since -> governance_graph_change_events join works); regulations
    and jurisdictions are typically closer to static/reference data in most
    GRC platforms, so this reference implementation returns them from a
    provided static catalog instead of inventing more placeholder tables --
    a human merging this must replace all three with real queries.
    """

    def __init__(self, session: Session, regulations_catalog: dict, jurisdictions: list[dict]):
        self._session = session
        self._regulations_catalog = regulations_catalog
        self._jurisdictions = jurisdictions

    def list_ai_systems(self, org_id: Any, changed_since: datetime | None) -> list[dict]:
        query = self._session.query(_AssumedAiSystem).filter(_AssumedAiSystem.org_id == org_id)
        if changed_since is not None:
            changed_ids = (
                self._session.query(GovernanceGraphChangeEvent.ai_system_id)
                .filter(
                    GovernanceGraphChangeEvent.org_id == org_id,
                    GovernanceGraphChangeEvent.changed_at >= changed_since,
                )
                .subquery()
            )
            query = query.filter(_AssumedAiSystem.id.in_(sa.select(changed_ids)))
        return [
            {
                "id": row.id,
                "name": row.name,
                "geographic_scope": row.geographic_scope,
                "data_categories": row.data_categories,
                "risk_tier": row.risk_tier,
                "deployment_status": row.deployment_status,
            }
            for row in query.all()
        ]

    def list_regulations_catalog(self, org_id: Any, changed_since: datetime | None) -> dict:
        # Regulations/obligations catalogs are typically near-static reference
        # data (not per-org, rarely changing) in GRC platforms -- ASSUMPTION,
        # see ASSUMPTIONS.md. changed_since is accepted for contract symmetry
        # but not applied here; a human must confirm whether this catalog is
        # in fact per-org and outbox-tracked in the real core.
        return self._regulations_catalog

    def list_jurisdictions(self, org_id: Any, changed_since: datetime | None) -> list[dict]:
        return self._jurisdictions


class FixtureBackedExportDataSource(ExportDataSource):
    """Simple in-memory data source used by this repo's own tests (and a
    reasonable local-dev default) -- returns whatever dict/list data it was
    constructed with, applying a best-effort changed_since filter by looking
    for an `updated_at`/`changed_at` key on each item if present. Mirrors
    tests/fixtures/sample_export.py's shape exactly when fed that fixture."""

    def __init__(self, ai_systems: list[dict], regulations_catalog: dict, jurisdictions: list[dict]):
        self._ai_systems = ai_systems
        self._regulations_catalog = regulations_catalog
        self._jurisdictions = jurisdictions

    @staticmethod
    def _passes_changed_since(item: dict, changed_since: datetime | None) -> bool:
        if changed_since is None:
            return True
        changed_at = item.get("_changed_at")
        if changed_at is None:
            return True
        return bool(changed_at >= changed_since)

    def list_ai_systems(self, org_id: Any, changed_since: datetime | None) -> list[dict]:
        return [
            {k: v for k, v in item.items() if not k.startswith("_")}
            for item in self._ai_systems
            if self._passes_changed_since(item, changed_since)
        ]

    def list_regulations_catalog(self, org_id: Any, changed_since: datetime | None) -> dict:
        return self._regulations_catalog

    def list_jurisdictions(self, org_id: Any, changed_since: datetime | None) -> list[dict]:
        return self._jurisdictions
