"""
Patch for CompliVibe core (app.complivibe.in), lands on top of migration head 0175.
Field names / existing-pattern assumptions below are best-effort from PATENT.md and
must be verified against the real ai_system model / AuditService / permission system
before merging -- see core-side-patch/ASSUMPTIONS.md.

STUB. Real core has its own AuditService with a `write_audit_log(...)` method
(CLAUDE_CODE_GOAL_PROMPT.md is explicit that it's `write_audit_log`, not
`.log()` -- match the naming convention already established by the P6-P9
satellite integrations). We do not have access to its real signature, import
path, or storage backend from this satellite-only repo.

routers/patent_ingest_p2.py imports `AuditService` from THIS module only so it
is independently testable here. Before merging:
  1. delete this file's class and instead
     `from <core's real audit module> import AuditService`
  2. verify write_audit_log's real keyword arguments match what's called in
     routers/patent_ingest_p2.py (org_id, actor_id, event_type, payload) --
     rename call-site kwargs to match if the real signature differs
  3. verify whether the real AuditService requires the DB session to be
     passed in (ours accepts an optional one and ignores it)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, ClassVar

logger = logging.getLogger("core_side_patch.audit_service_stub")


@dataclass
class AuditLogEntry:
    org_id: Any
    actor_id: Any
    event_type: str
    payload: dict
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class AuditService:
    """STUB implementation -- see module docstring. Test-visible in-memory sink
    (`_written`) lets tests assert an audit row was produced without a real DB.
    """

    _written: ClassVar[list[AuditLogEntry]] = []

    @classmethod
    def write_audit_log(
        cls,
        *,
        session: Any | None = None,
        org_id: Any,
        actor_id: Any,
        event_type: str,
        payload: dict,
    ) -> AuditLogEntry:
        entry = AuditLogEntry(org_id=org_id, actor_id=actor_id, event_type=event_type, payload=dict(payload))
        cls._written.append(entry)
        # Structured `extra=` fields (matching routers/patent_ingest_p2.py's
        # convention -- see that module's ASSUMPTIONS.md item 17 note on why
        # core-side-patch/ uses stdlib `logging` directly instead of
        # src/p2_satellite/observability.py's log_event() helper) rather than
        # interpolating org_id/actor_id/event_type into the message string,
        # so a log aggregator can filter/search on them regardless of
        # output format.
        logger.info(
            "audit_log",
            extra={
                "event_type": event_type,
                "org_id": org_id,
                "actor_id": actor_id,
                "payload": payload,
            },
        )
        return entry

    @classmethod
    def _reset_for_tests(cls) -> None:
        cls._written.clear()
