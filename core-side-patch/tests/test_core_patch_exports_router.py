# Patch for CompliVibe core (app.complivibe.in), lands on top of migration head 0175.
# Test-harness only (not part of the patch set shipped to core).
#
# FastAPI TestClient tests for routers/patent_exports_p2.py: auth scoping
# (401/403) and response envelope/shape matching tests/fixtures/sample_export.py.
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from routers import patent_exports_p2

from tests.fixtures.sample_export import AI_SYSTEMS_EXPORT, JURISDICTIONS_EXPORT, REGULATIONS_CATALOG_EXPORT

from data_providers import FixtureBackedExportDataSource


@pytest.fixture()
def app_and_client():
    app = FastAPI()
    app.include_router(patent_exports_p2.router)

    fixture_source = FixtureBackedExportDataSource(
        ai_systems=AI_SYSTEMS_EXPORT["items"],
        regulations_catalog=REGULATIONS_CATALOG_EXPORT,
        jurisdictions=JURISDICTIONS_EXPORT["items"],
    )
    app.dependency_overrides[patent_exports_p2.get_export_data_source] = lambda: fixture_source

    client = TestClient(app)
    yield client

    app.dependency_overrides.clear()


def test_ai_systems_requires_authorization_header(app_and_client):
    resp = app_and_client.get("/api/v1/patent-exports/p2/ai-systems")
    assert resp.status_code == 401


def test_ai_systems_rejects_wrong_scope(app_and_client):
    resp = app_and_client.get(
        "/api/v1/patent-exports/p2/ai-systems", headers={"Authorization": "Bearer dev-ingest-key"}
    )
    assert resp.status_code == 403


def test_ai_systems_accepts_correct_scope_and_matches_fixture_shape(app_and_client):
    resp = app_and_client.get(
        "/api/v1/patent-exports/p2/ai-systems", headers={"Authorization": "Bearer dev-export-key"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["items"] == AI_SYSTEMS_EXPORT["items"]
    assert body["meta"]["count"] == len(AI_SYSTEMS_EXPORT["items"])
    assert body["meta"]["changed_since"] is None
    assert set(body["items"][0].keys()) == {
        "id",
        "name",
        "geographic_scope",
        "data_categories",
        "risk_tier",
        "deployment_status",
    }


def test_regulations_catalog_matches_fixture_shape(app_and_client):
    resp = app_and_client.get(
        "/api/v1/patent-exports/p2/regulations-catalog", headers={"Authorization": "Bearer dev-export-key"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["items"] == REGULATIONS_CATALOG_EXPORT["items"]
    assert body["risk_tier_obligations"] == REGULATIONS_CATALOG_EXPORT["risk_tier_obligations"]
    first_reg = body["items"][0]
    assert set(first_reg.keys()) == {"key", "name", "triggered_by_data_categories", "requires_obligations"}
    assert set(first_reg["requires_obligations"][0].keys()) == {"key", "name", "needs_controls"}


def test_jurisdictions_matches_fixture_shape(app_and_client):
    resp = app_and_client.get(
        "/api/v1/patent-exports/p2/jurisdictions", headers={"Authorization": "Bearer dev-export-key"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["items"] == JURISDICTIONS_EXPORT["items"]
    assert set(body["items"][0].keys()) == {"key", "name", "regulations"}


def test_changed_since_is_echoed_in_meta(app_and_client):
    resp = app_and_client.get(
        "/api/v1/patent-exports/p2/ai-systems",
        headers={"Authorization": "Bearer dev-export-key"},
        params={"changed_since": "2026-01-01T00:00:00Z"},
    )
    assert resp.status_code == 200
    assert resp.json()["meta"]["changed_since"] == "2026-01-01T00:00:00+00:00"
