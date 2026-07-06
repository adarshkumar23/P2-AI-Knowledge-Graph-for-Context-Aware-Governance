"""
Unit tests for observability.configure_json_logging() -- the opt-in
structlog-backed JSON renderer added in the open-source-tooling polish pass.

Covers:
  - it's idempotent (safe to call more than once)
  - it actually produces valid, parseable JSON
  - log_event()'s structured `p2_`-prefixed fields (ai_system_id, org_id, ...)
    show up as top-level JSON keys, with zero call-site changes
  - exc_info=True still attaches exception info in the JSON output
  - RedactSecretsFilter still scrubs secrets from extra fields once rendered
    as JSON (this matters MORE under JSON rendering -- see observability.py's
    module docstring -- so it's tested here, not just at the plain-text layer)

Every test restores global logging/structlog state afterward (root logger's
handlers/level, structlog's global config) so this file's use of
configure_json_logging() -- which mutates process-wide state by design --
can never bleed into any other test file's caplog-based assertions.
"""

from __future__ import annotations

import io
import json
import logging

import pytest
import structlog

from src.p2_satellite import observability


@pytest.fixture()
def isolated_root_logger():
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    original_marker = getattr(root, observability._JSON_LOGGING_MARKER, False)

    yield root

    root.handlers = original_handlers
    root.setLevel(original_level)
    setattr(root, observability._JSON_LOGGING_MARKER, original_marker)
    structlog.reset_defaults()


def _configure_with_captured_stream(root: logging.Logger) -> io.StringIO:
    observability.configure_json_logging()
    stream = io.StringIO()
    root.handlers[0].stream = stream
    return stream


def test_configure_json_logging_is_idempotent(isolated_root_logger):
    observability.configure_json_logging()
    first_handlers = list(isolated_root_logger.handlers)

    observability.configure_json_logging()

    assert isolated_root_logger.handlers == first_handlers
    assert len(isolated_root_logger.handlers) == 1


def test_json_output_is_valid_and_carries_structured_fields(isolated_root_logger):
    stream = _configure_with_captured_stream(isolated_root_logger)

    logger = observability.get_logger("test.json.logging")
    observability.log_event(logger, logging.INFO, "test.event", ai_system_id="sys-x", org_id=7)

    parsed = json.loads(stream.getvalue().strip())
    assert parsed["p2_event"] == "test.event"
    assert parsed["p2_ai_system_id"] == "sys-x"
    assert parsed["p2_org_id"] == 7
    assert parsed["level"] == "info"


def test_json_output_includes_traceback_on_exc_info(isolated_root_logger):
    stream = _configure_with_captured_stream(isolated_root_logger)

    logger = observability.get_logger("test.json.logging")
    try:
        raise ValueError("boom")
    except ValueError:
        observability.log_event(logger, logging.ERROR, "test.failed", exc_info=True, ai_system_id="sys-x")

    parsed = json.loads(stream.getvalue().strip())
    assert parsed["p2_ai_system_id"] == "sys-x"
    assert "ValueError" in parsed.get("exception", "")
    assert "boom" in parsed.get("exception", "")


def test_json_rendering_still_redacts_secrets_in_extra_fields(isolated_root_logger):
    stream = _configure_with_captured_stream(isolated_root_logger)

    logger = observability.get_logger("test.json.logging.redaction")
    secret = "super-secret-key-value"
    observability.install_secret_redaction(logger)
    # Monkeypatch the filter's secret list directly (avoids depending on
    # src.p2_satellite.config.settings' actual values in this unit test).
    for f in logger.filters:
        if isinstance(f, observability.RedactSecretsFilter):
            f._secrets = [secret]

    observability.log_event(logger, logging.INFO, "test.leak_attempt", token=secret)

    output = stream.getvalue()
    assert secret not in output
    assert "***REDACTED***" in output
