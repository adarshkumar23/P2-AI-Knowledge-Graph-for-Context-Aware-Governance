"""
Failure-mode + observability hardening tests for src/p2_satellite/graph_builder.py.

Covers:
  - GraphBuildIncompleteError: fetch_and_build_graph() must raise this single,
    typed exception (wrapping the original httpx exception + which of the
    three export steps failed) instead of leaking an undifferentiated httpx
    exception, and must never produce a partial graph -- the failing fetch
    short-circuits the remaining fetches AND build_graph() entirely.
  - Structured logging: every stage logs via observability.timed_stage, and
    the module never lets the literal core_export_api_key value reach a log
    record, even at DEBUG level (defense in depth on top of
    install_secret_redaction).
"""

from __future__ import annotations

import logging

import pytest

from src.p2_satellite import graph_builder
from src.p2_satellite.config import settings
from tests.fixtures.sample_export import (
    AI_SYSTEMS_EXPORT,
    JURISDICTIONS_EXPORT,
    REGULATIONS_CATALOG_EXPORT,
)


class _BoomError(Exception):
    """Stand-in for whatever the underlying fetch raises (httpx or otherwise)."""


def _patch_all_fetchers(monkeypatch, calls, failing_step: str):
    def fake_fetch_ai_systems(changed_since=None):
        calls.append("ai_systems")
        if failing_step == "ai_systems":
            raise _BoomError("ai-systems export unreachable")
        return AI_SYSTEMS_EXPORT

    def fake_fetch_regulations_catalog(changed_since=None):
        calls.append("regulations_catalog")
        if failing_step == "regulations_catalog":
            raise _BoomError("regulations-catalog export unreachable")
        return REGULATIONS_CATALOG_EXPORT

    def fake_fetch_jurisdictions(changed_since=None):
        calls.append("jurisdictions")
        if failing_step == "jurisdictions":
            raise _BoomError("jurisdictions export unreachable")
        return JURISDICTIONS_EXPORT

    monkeypatch.setattr(graph_builder, "fetch_ai_systems", fake_fetch_ai_systems)
    monkeypatch.setattr(graph_builder, "fetch_regulations_catalog", fake_fetch_regulations_catalog)
    monkeypatch.setattr(graph_builder, "fetch_jurisdictions", fake_fetch_jurisdictions)


def test_regulations_catalog_failure_raises_typed_error_not_partial_graph(monkeypatch):
    calls: list[str] = []
    _patch_all_fetchers(monkeypatch, calls, failing_step="regulations_catalog")

    with pytest.raises(graph_builder.GraphBuildIncompleteError) as exc_info:
        graph_builder.fetch_and_build_graph(changed_since="2026-01-01T00:00:00Z")

    err = exc_info.value
    assert err.step == "regulations-catalog"
    # Original exception is preserved via exception chaining, not swallowed.
    assert isinstance(err.__cause__, _BoomError)

    # ai_systems (step #1) already ran to completion before the failure;
    # jurisdictions (step #3) must NOT have been wastefully called on top of
    # a build that can never succeed -- proving the sequential short-circuit.
    assert calls == ["ai_systems", "regulations_catalog"]
    assert "jurisdictions" not in calls


def test_ai_systems_failure_raises_typed_error_and_short_circuits_rest(monkeypatch):
    calls: list[str] = []
    _patch_all_fetchers(monkeypatch, calls, failing_step="ai_systems")

    with pytest.raises(graph_builder.GraphBuildIncompleteError) as exc_info:
        graph_builder.fetch_and_build_graph()

    assert exc_info.value.step == "ai-systems"
    assert calls == ["ai_systems"]


def test_jurisdictions_failure_raises_typed_error_after_first_two_succeed(monkeypatch):
    calls: list[str] = []
    _patch_all_fetchers(monkeypatch, calls, failing_step="jurisdictions")

    with pytest.raises(graph_builder.GraphBuildIncompleteError) as exc_info:
        graph_builder.fetch_and_build_graph()

    assert exc_info.value.step == "jurisdictions"
    assert calls == ["ai_systems", "regulations_catalog", "jurisdictions"]


def test_no_partial_graph_is_ever_returned_on_failure(monkeypatch):
    """fetch_and_build_graph() must not return None or any graph-shaped value
    on failure -- it must raise. This is a structural guarantee (the function
    has no `return` before the final line), but we assert it behaviorally
    too: the call either raises GraphBuildIncompleteError or returns a fully
    built graph, never anything in between."""
    calls: list[str] = []
    _patch_all_fetchers(monkeypatch, calls, failing_step="regulations_catalog")

    result = None
    try:
        result = graph_builder.fetch_and_build_graph()
        raised = False
    except graph_builder.GraphBuildIncompleteError:
        raised = True

    assert raised is True
    assert result is None


def test_successful_build_still_returns_full_graph(monkeypatch):
    """Sanity check the happy path is untouched by the new error-wrapping/
    logging: no failing step -> a normal, fully-built graph comes back."""
    calls: list[str] = []
    _patch_all_fetchers(monkeypatch, calls, failing_step="")

    graph = graph_builder.fetch_and_build_graph()

    assert calls == ["ai_systems", "regulations_catalog", "jurisdictions"]
    assert graph.number_of_nodes() > 0


# --------------------------------------------------------------------------
# Secret redaction / no-deliberate-secret-logging
# --------------------------------------------------------------------------


def test_fetch_never_logs_the_literal_api_key(monkeypatch):
    """Even at DEBUG level, no log record emitted while fetching must contain
    the literal core_export_api_key value. install_secret_redaction() is a
    safety net, but graph_builder.py must also simply never log the header/
    key value on purpose -- this test would catch either failure mode."""
    import httpx

    captured_records: list[logging.LogRecord] = []

    class _ListHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured_records.append(record)

    handler = _ListHandler()
    handler.setLevel(logging.DEBUG)
    graph_builder.logger.addHandler(handler)
    graph_builder.logger.setLevel(logging.DEBUG)

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return AI_SYSTEMS_EXPORT

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, headers=None, params=None):
            return FakeResponse()

    monkeypatch.setattr(httpx, "Client", FakeClient)

    try:
        graph_builder.fetch_ai_systems(changed_since="2026-05-01T00:00:00Z")

        secret = settings.core_export_api_key
        for record in captured_records:
            rendered = record.getMessage()
            assert secret not in rendered
            # Also check any structured `extra=` fields attached via log_event.
            for key, value in vars(record).items():
                if key.startswith("p2_"):
                    assert secret not in str(value)
    finally:
        graph_builder.logger.removeHandler(handler)
