"""
Structured logging helpers for the P2 satellite (production-hardening pass;
JSON-capable rendering added in the open-source-tooling polish pass).

Every event carries its context (ai_system_id, org_id, duration_ms, ...) via
the standard `extra=` mechanism rather than string-interpolated into the
message (see `log_event()`), so any log aggregator that reads structured
fields can filter/search on them regardless of output format.

`get_logger()`/`log_event()`/`timed_stage()` still return/accept plain
stdlib `logging.Logger` objects and populate `extra={"p2_...": ...}` exactly
as before -- every existing call site, and every existing test that asserts
on `caplog.records[i].p2_ai_system_id` etc., is unaffected. What's new is
`configure_json_logging()`: an OPT-IN function (never called automatically
at import time, consistent with this module's original design boundary of
"this module does not configure handlers/formatters itself, that's a
deployment concern") that layers `structlog` (Apache-2.0/MIT dual-licensed)
on TOP of stdlib logging via `structlog.stdlib.ProcessorFormatter`, so a
deployment that calls it gets real JSON log lines out of the exact same
`logger.log(...)` calls this module already makes -- no call-site changes
anywhere in the codebase.

Layering structlog on top of (not replacing) stdlib logging, specifically
via `ProcessorFormatter` attached to a handler, is what keeps pytest's
`caplog` fixture working unmodified: `caplog` intercepts raw `LogRecord`s
before they reach a handler's formatter, so it never sees the
JSON-rendering step at all -- only the eventual human/machine log consumer
(a real terminal or log shipper) does.

Also provides `install_secret_redaction()`, a defense-in-depth logging.Filter
that scrubs known secret values (HMAC shared secret, export/ingest API keys)
from a record's rendered message AND any `p2_`-prefixed extra field, before
it reaches a handler, so a stray `logger.debug(f"...{settings.core_ingest_api_key}...")`
mistake -- or a stray `log_event(..., some_field=settings.core_ingest_api_key)`
-- still can't leak the raw secret to log output. Scrubbing extra fields too
(not just the rendered message) matters more once `configure_json_logging()`
is in use, since JSON rendering surfaces every extra field verbatim.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log_event(logger: logging.Logger, level: int, event: str, *, exc_info: bool = False, **context: Any) -> None:
    """Log `event` (a short, stable, dot.separated name -- e.g.
    'graph_build.end', 'ingest_push.failed') with `context` carried as
    structured `extra` fields (prefixed `p2_` to avoid colliding with
    LogRecord's own attribute names), not interpolated into the message.

    `exc_info=True` attaches the current exception's traceback (same as
    `logger.exception(...)`) while still keeping every other field
    structured -- use this from an `except` block instead of
    `logger.exception("... %s", value)`, so ai_system_id/org_id/
    trigger_reason/etc. land as queryable structured fields rather than
    being interpolated into the free-text message.
    """
    safe_context = {f"p2_{key}": value for key, value in context.items()}
    logger.log(level, event, extra={"p2_event": event, **safe_context}, exc_info=exc_info)


@contextmanager
def timed_stage(logger: logging.Logger, stage_name: str, **context: Any) -> Iterator[None]:
    """Context manager logging '<stage_name>.start', then either
    '<stage_name>.end' (with duration_ms) on success or '<stage_name>.failed'
    (with duration_ms and the exception type/message) on exception -- the
    exception always re-raises unchanged, this only adds a structured log
    around it. Use for every external-call / expensive stage: export pull,
    graph build, traversal, ingest push.
    """
    start = time.monotonic()
    log_event(logger, logging.INFO, f"{stage_name}.start", **context)
    try:
        yield
    except Exception as exc:
        duration_ms = round((time.monotonic() - start) * 1000, 2)
        log_event(
            logger,
            logging.ERROR,
            f"{stage_name}.failed",
            duration_ms=duration_ms,
            error_type=type(exc).__name__,
            error=str(exc),
            **context,
        )
        raise
    else:
        duration_ms = round((time.monotonic() - start) * 1000, 2)
        log_event(logger, logging.INFO, f"{stage_name}.end", duration_ms=duration_ms, **context)


class RedactSecretsFilter(logging.Filter):
    """Scrubs any exact occurrence of a known secret value from a record's
    rendered message. Attach via `install_secret_redaction()` rather than
    constructing directly, so the secret list always comes from `settings`
    at call time (never hardcoded, never stale if a secret rotates within a
    process lifetime -- unlikely, but cheap to get right)."""

    def __init__(self, secrets: list[str]) -> None:
        super().__init__()
        # Ignore short/empty values -- redacting e.g. "" or a 1-char default
        # would corrupt unrelated log content instead of protecting anything.
        self._secrets = [s for s in secrets if s and len(s) >= 6]

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True

        rendered = record.getMessage()
        redacted = rendered
        for secret in self._secrets:
            if secret in redacted:
                redacted = redacted.replace(secret, "***REDACTED***")
        if redacted != rendered:
            record.msg = redacted
            record.args = ()

        # Also scrub any log_event()-style `p2_`-prefixed extra field, not
        # just the rendered message -- matters most once
        # configure_json_logging() is in use, since JSON rendering serializes
        # every extra field verbatim (a secret accidentally passed as
        # `log_event(..., some_field=settings.core_ingest_api_key)` would
        # otherwise bypass this filter entirely, since it never appears in
        # `record.getMessage()` at all).
        for key, value in vars(record).items():
            if not key.startswith("p2_") or not isinstance(value, str):
                continue
            redacted_value = value
            for secret in self._secrets:
                if secret in redacted_value:
                    redacted_value = redacted_value.replace(secret, "***REDACTED***")
            if redacted_value != value:
                setattr(record, key, redacted_value)

        return True


def install_secret_redaction(logger: logging.Logger) -> None:
    """Attach a RedactSecretsFilter sourced from the satellite's current
    settings (event_listener_shared_secret, core_export_api_key,
    core_ingest_api_key) to `logger`. Idempotent: calling this more than once
    on the same logger only installs one filter instance."""
    from src.p2_satellite.config import settings  # local import: avoid import cycle at module load

    if any(isinstance(f, RedactSecretsFilter) for f in logger.filters):
        return
    logger.addFilter(
        RedactSecretsFilter(
            [
                settings.event_listener_shared_secret,
                settings.core_export_api_key,
                settings.core_ingest_api_key,
            ]
        )
    )


_JSON_LOGGING_MARKER = "_p2_json_logging_configured"


def configure_json_logging(level: int = logging.INFO) -> None:
    """Opt-in: configure the ROOT logger to emit JSON-formatted structured
    logs via structlog, layered on top of (not replacing) stdlib `logging` --
    see module docstring for why this is safe to call without touching any
    existing call site or breaking `caplog`-based tests.

    Call this once, early, from a real deployment's process entrypoint (see
    README.md "Observability") -- e.g. before `uvicorn.run(...)` in
    event_listener.py's `if __name__ == "__main__":` block, or from
    scripts/ launcher. NOT called automatically at import time by this
    module or by event_listener.py itself: whether logs render as JSON is a
    deployment choice (plain text is more convenient for local dev), not
    something a library module should force on every importer.

    Idempotent -- calling this more than once is a no-op after the first
    call.
    """
    root = logging.getLogger()
    if getattr(root, _JSON_LOGGING_MARKER, False):
        return

    import structlog

    # Every log call in this codebase goes through plain stdlib
    # `logging.getLogger(...).log(...)` (via log_event()/timed_stage()) --
    # NEVER structlog's own `structlog.get_logger()` API. That means
    # structlog only ever sees these records as "foreign" (non-structlog)
    # LogRecords being formatted, which is exactly what `foreign_pre_chain`
    # is for: it runs shared_processors on a foreign record to build its
    # event dict BEFORE the main `processors` chain renders it. `ExtraAdder`
    # merges the LogRecord's `extra={"p2_...": ...}` fields (the exact
    # mechanism log_event() already uses) into that event dict, so the JSON
    # output gets p2_event/p2_ai_system_id/etc. as top-level keys with zero
    # call-site changes. (The `structlog.configure(...)` call below is not
    # strictly required for this foreign-record path, but is included so
    # any FUTURE code that calls `structlog.get_logger()` directly renders
    # consistently through the same formatter.)
    shared_processors: list[structlog.types.Processor] = [
        structlog.stdlib.add_log_level,
        structlog.stdlib.ExtraAdder(),
        structlog.processors.TimeStamper(fmt="iso"),
        # Converts a `log_event(..., exc_info=True)` call's raw exc_info
        # tuple into a rendered "exception" string field (a traceback object
        # isn't JSON-serializable on its own).
        structlog.processors.format_exc_info,
    ]
    structlog.configure(
        processors=[*shared_processors, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
    )
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    root.handlers = [handler]
    root.setLevel(level)
    setattr(root, _JSON_LOGGING_MARKER, True)
