"""
Patch for CompliVibe core (app.complivibe.in), lands on top of migration head 0175.
Field names / existing-pattern assumptions below are best-effort from PATENT.md and
must be verified against the real ai_system model / AuditService / permission system
before merging -- see core-side-patch/ASSUMPTIONS.md.

Three READ-ONLY export endpoints the P2 satellite pulls from on its own
schedule/trigger (PATENT.md "Satellite Architecture" -- agent-push /
inbound-only: core NEVER calls the satellite). Response envelope and per-item
field shape are pinned to tests/fixtures/sample_export.py, the shared contract
other P2 workstreams (graph_builder.py, reference_cte.py) also build against.

Auth: `Authorization: Bearer <api_key>` where the key is a dedicated scoped
key carrying permission `patent_export:p2:read` (NOT a normal user session --
see dependencies.require_patent_export_scope and permissions.py).
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query

from data_providers import ExportDataSource
from dependencies import (
    ActiveUser,
    Organization,
    get_current_active_user,
    get_current_organization,
    require_patent_export_scope,
)

router = APIRouter(prefix="/api/v1/patent-exports/p2", tags=["patent-exports-p2"])


def get_export_data_source() -> ExportDataSource:
    """STUB dependency. Must be overridden (via FastAPI dependency_overrides,
    same as get_db_session) with a real ExportDataSource -- see
    data_providers.py's SQLAlchemyExportDataSource for the intended query
    pattern (including the changed_since -> outbox join)."""
    raise NotImplementedError(
        "get_export_data_source is a stub; override with a real data_providers.ExportDataSource "
        "implementation before serving traffic."
    )


def _envelope(items: list, changed_since: datetime | None) -> dict:
    return {
        "items": items,
        "meta": {
            "count": len(items),
            "changed_since": changed_since.isoformat() if changed_since else None,
        },
    }


@router.get("/ai-systems")
def get_ai_systems(
    changed_since: datetime | None = Query(default=None),
    org: Organization = Depends(get_current_organization),
    _user: ActiveUser = Depends(get_current_active_user),
    _scope: str = Depends(require_patent_export_scope()),
    data_source: ExportDataSource = Depends(get_export_data_source),
) -> dict:
    """Only the fields the graph needs (PATENT.md 'Graph Structure' /
    tests/fixtures/sample_export.py AI_SYSTEMS_EXPORT): id, name,
    geographic_scope, data_categories, risk_tier, deployment_status."""
    items = data_source.list_ai_systems(org_id=org.id, changed_since=changed_since)
    return _envelope(items, changed_since)


@router.get("/regulations-catalog")
def get_regulations_catalog(
    changed_since: datetime | None = Query(default=None),
    org: Organization = Depends(get_current_organization),
    _user: ActiveUser = Depends(get_current_active_user),
    _scope: str = Depends(require_patent_export_scope()),
    data_source: ExportDataSource = Depends(get_export_data_source),
) -> dict:
    """Shape matches tests/fixtures/sample_export.py REGULATIONS_CATALOG_EXPORT:
    items[].key/name/triggered_by_data_categories/requires_obligations
    (each with key/name/needs_controls), plus top-level risk_tier_obligations."""
    catalog = data_source.list_regulations_catalog(org_id=org.id, changed_since=changed_since)
    items = catalog.get("items", [])
    envelope = _envelope(items, changed_since)
    # risk_tier_obligations rides alongside items (not itself a list of "items"
    # in the changed_since sense) -- included at the top level to match
    # tests/fixtures/sample_export.py's REGULATIONS_CATALOG_EXPORT shape exactly.
    envelope["risk_tier_obligations"] = catalog.get("risk_tier_obligations", {})
    return envelope


@router.get("/jurisdictions")
def get_jurisdictions(
    changed_since: datetime | None = Query(default=None),
    org: Organization = Depends(get_current_organization),
    _user: ActiveUser = Depends(get_current_active_user),
    _scope: str = Depends(require_patent_export_scope()),
    data_source: ExportDataSource = Depends(get_export_data_source),
) -> dict:
    """Shape matches tests/fixtures/sample_export.py JURISDICTIONS_EXPORT:
    items[].key/name/regulations."""
    items = data_source.list_jurisdictions(org_id=org.id, changed_since=changed_since)
    return _envelope(items, changed_since)
