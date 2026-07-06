"""
Patch for CompliVibe core (app.complivibe.in), lands on top of migration head 0175.
Field names / existing-pattern assumptions below are best-effort from PATENT.md and
must be verified against the real ai_system model / AuditService / permission system
before merging -- see core-side-patch/ASSUMPTIONS.md.

STUB FastAPI dependencies. The real core has its own get_current_organization /
get_current_active_user dependencies (returning ORM-backed objects, per
CLAUDE_CODE_GOAL_PROMPT.md's explicit "dependency style returns objects, not
dicts" requirement) and its own scoped-API-key infrastructure for the two new
patent_export:p2:read / patent_ingest:p2:write permissions. None of that is
accessible from this satellite-only repo -- everything below is a best-effort
stand-in. See ASSUMPTIONS.md for the full list of what's unverified.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from permissions import PATENT_EXPORT_P2_READ, PATENT_INGEST_P2_WRITE


@dataclass(frozen=True)
class Organization:
    """STUB. Real core's Organization model almost certainly has many more
    fields; `.id` is the important one for this patch. `.org_id` is included
    only because CLAUDE_CODE_GOAL_PROMPT.md's dependency-style requirement
    names both `.id` and `.org_id` -- verify whether the real Organization
    object actually exposes `.org_id` (redundant with `.id`) or whether that
    was really describing the *User* object below; see ASSUMPTIONS.md.
    """

    id: int
    org_id: int
    name: str = "stub-org"


@dataclass(frozen=True)
class ActiveUser:
    """STUB. Real core's user object has many more fields; only `.id` and
    `.org_id` are used by this patch (for AuditService actor_id and to confirm
    the acting user belongs to the organization being read/written)."""

    id: int
    org_id: int
    is_active: bool = True
    email: str = "stub-user@example.invalid"


def get_current_organization() -> Organization:
    """STUB. Real core resolves this from request/session state (likely via
    the authenticated user or an org-scoped header/subdomain). Returns a fixed
    dev object so routers are exercisable/testable in this repo. Tests
    override this dependency via FastAPI's dependency_overrides. MUST be
    replaced with the real dependency before merge."""
    return Organization(id=1, org_id=1)


def get_current_active_user() -> ActiveUser:
    """STUB -- see get_current_organization."""
    return ActiveUser(id=1, org_id=1)


# --------------------------------------------------------------------------
# Scoped API key validation for patent_export:p2:read / patent_ingest:p2:write
# --------------------------------------------------------------------------
# These are NOT normal user permissions (see permissions.py docstring). We
# don't know how/whether core already stores issued scoped keys (hashed, in a
# dedicated table? in a secrets manager?) -- see ASSUMPTIONS.md. The validator
# below is deliberately pluggable (a module-level function reference, not
# hardcoded logic) so:
#   (a) tests can monkeypatch `dependencies.validate_scoped_api_key` directly
#   (b) a human merging this can swap in a real hashed-key lookup without
#       touching the dependency functions or the routers that use them.
ScopedKeyValidator = Callable[[str, str], bool]

# Dev/test-only in-memory registry. NEVER use plaintext key comparison like
# this in production -- real core must hash incoming keys and compare against
# stored hashes. See ASSUMPTIONS.md.
_DEV_SCOPED_KEYS: dict[str, str] = {
    "dev-export-key": PATENT_EXPORT_P2_READ,
    "dev-ingest-key": PATENT_INGEST_P2_WRITE,
}


def _default_validate_scoped_api_key(token: str, required_permission: str) -> bool:
    return _DEV_SCOPED_KEYS.get(token) == required_permission


validate_scoped_api_key: ScopedKeyValidator = _default_validate_scoped_api_key


def _extract_bearer_token(authorization: str | None) -> str:
    if not authorization:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing Authorization header")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Expected 'Authorization: Bearer <api_key>'",
        )
    return token


def require_patent_export_scope() -> Callable[..., str]:
    """FastAPI dependency factory validating the patent_export:p2:read scoped
    key. Returns 401 if the Authorization header is missing/malformed, 403 if
    the key doesn't carry the required permission."""

    def _dependency(authorization: str | None = Header(default=None)) -> str:
        token = _extract_bearer_token(authorization)
        if not validate_scoped_api_key(token, PATENT_EXPORT_P2_READ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Scoped key missing patent_export:p2:read",
            )
        return token

    return _dependency


def require_patent_ingest_scope() -> Callable[..., str]:
    """FastAPI dependency factory validating the patent_ingest:p2:write scoped key."""

    def _dependency(authorization: str | None = Header(default=None)) -> str:
        token = _extract_bearer_token(authorization)
        if not validate_scoped_api_key(token, PATENT_INGEST_P2_WRITE):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Scoped key missing patent_ingest:p2:write",
            )
        return token

    return _dependency


def require_permission(permission: str) -> Callable[..., None]:
    """STUB FastAPI dependency factory for the customer-facing knowledge-graph
    endpoints (routers/patent_knowledge_graph_p2.py), which are reached by
    normal authenticated human users (or their configured automations), not
    the satellite's scoped API keys.

    ASSUMPTION (see ASSUMPTIONS.md): we have no access to core's real
    human-RBAC permission-check dependency (distinct from the scoped-API-key
    validator above -- see permissions.GOVERNANCE_GRAPH_READ/WRITE's
    docstring). This stub always allows, and its `permission` argument is
    currently unused -- it exists only to pin the call-site shape
    (`Depends(require_permission(GOVERNANCE_GRAPH_READ))`) so a human wiring
    the real check later only has to fill in this one function's body,
    without touching every router that calls it. MUST be replaced with a
    real permission check (e.g. `permission in current_user.role.permissions`)
    before this ships to real users.
    """

    def _dependency(user: ActiveUser = Depends(get_current_active_user)) -> None:
        return None

    return _dependency


def get_db_session() -> Session:
    """STUB. Real core almost certainly has its own get_db()/session-scoping
    dependency (e.g. a SQLAlchemy sessionmaker scoped per-request, or an async
    session dependency). Not accessible here. Routers depend on this so tests
    can override it with a real (SQLite, in this repo) Session via FastAPI's
    dependency_overrides. MUST be replaced with the real dependency before
    merge -- see ASSUMPTIONS.md."""
    raise NotImplementedError(
        "get_db_session is a stub; override with core's real DB session dependency "
        "(or, in tests, with a concrete SQLAlchemy Session -- see core-side-patch/tests/conftest.py)."
    )
