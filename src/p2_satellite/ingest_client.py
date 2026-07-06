"""
Pushes satellite-derived obligation results to core's ingest endpoint
(Workstream D).

POST {settings.core_base_url}/api/v1/patent-ingest/p2/obligation-derivation

Auth: Authorization: Bearer {settings.core_ingest_api_key}
(permission patent_ingest:p2:write — see PATENT.md "Satellite Architecture").

Retries (tenacity): transient errors only -- httpx.ConnectError,
httpx.TimeoutException, and 5xx responses -- stop_after_attempt(3) with
exponential backoff. 4xx responses are treated as PERMANENT rejections (e.g.
core rejected an unknown obligation/control id during its own re-validation
per PATENT.md "Satellites Compute, Core Decides") and are never retried.

IDEMPOTENCY
-----------
`derivation_hash` = sha256 hex digest of the canonical JSON (json.dumps with
sort_keys=True, no extra whitespace) of exactly:
    {ai_system_id, derived_obligations, derived_controls, methodology_version}

`graph_path` and any timestamp-ish fields are deliberately EXCLUDED from the
hash: two independent traversal runs over the same underlying graph state
should produce byte-identical obligation/control sets even if the specific
paths recorded or the wall-clock time differ, and such re-runs must hash
identically so they are safe to re-push without duplicating audit rows on
the core side.

The hash is sent twice, redundantly, for defense in depth:
  - as a request body field `derivation_hash`
  - as the `Idempotency-Key` request header

Core's ingest endpoint (Workstream A) is expected to dedupe writes on this
hash -- that is core's responsibility, not this module's. This module's job
is only to compute the hash consistently and attach it both places every
time, so that re-pushing the same derivation (e.g. a safety-net poll
re-deriving a system whose graph neighborhood hasn't changed since the last
event-triggered push) is always safe and never produces duplicate audit rows.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from src.p2_satellite.config import settings
from src.p2_satellite.observability import get_logger, install_secret_redaction, timed_stage

logger = get_logger(__name__)
install_secret_redaction(logger)

INGEST_PATH = "/api/v1/patent-ingest/p2/obligation-derivation"

# Fields that make up the idempotency hash. Deliberately excludes graph_path
# and any timestamp-ish fields -- see module docstring.
_HASH_FIELDS = ("ai_system_id", "derived_obligations", "derived_controls", "methodology_version")


class TransientIngestError(Exception):
    """Retryable ingest failure: connection/timeout error or a 5xx response."""


class PermanentIngestError(Exception):
    """Non-retryable ingest failure: a 4xx response (permanent rejection,
    e.g. core's re-validation found a bad obligation/control id)."""

    def __init__(self, status_code: int, body: str) -> None:
        super().__init__(f"permanent ingest rejection ({status_code}): {body}")
        self.status_code = status_code
        self.body = body


def compute_derivation_hash(derivation: dict[str, Any]) -> str:
    """sha256 hex digest of the canonical (sort_keys=True) JSON of the
    idempotency-relevant subset of `derivation` -- see module docstring for
    exactly which fields are included/excluded and why."""
    canonical_subset = {field: derivation[field] for field in _HASH_FIELDS}
    canonical_json = json.dumps(canonical_subset, sort_keys=True)
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def _is_transient(exc: BaseException) -> bool:
    return isinstance(exc, TransientIngestError)


@retry(
    retry=retry_if_exception(_is_transient),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
    reraise=True,
)
def _post_with_retry(url: str, body: dict[str, Any], headers: dict[str, str]) -> httpx.Response:
    try:
        with httpx.Client(timeout=10.0) as client:
            response = client.post(url, json=body, headers=headers)
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise TransientIngestError(str(exc)) from exc

    if response.status_code >= 500:
        raise TransientIngestError(f"server error {response.status_code}: {response.text}")
    if 400 <= response.status_code < 500:
        raise PermanentIngestError(response.status_code, response.text)

    return response


def push_derivation(derivation: dict[str, Any], trigger_reason: str) -> dict[str, Any]:
    """POST a derivation result (the dict shape returned by
    traversal.derive_obligations) to core's ingest endpoint.

    `trigger_reason` ("event" or "scheduled") is decided by the caller
    (event_listener.py / scheduler.py respectively), never by this function.

    Adds `trigger_reason` and `derivation_hash` to the outgoing body, and
    sends `derivation_hash` again as the `Idempotency-Key` header (see
    module docstring for the idempotency contract).

    Raises PermanentIngestError immediately on a 4xx (no retry).
    Raises TransientIngestError after exhausting retries on repeated
    connection/timeout errors or 5xx responses.
    """
    if trigger_reason not in ("event", "scheduled"):
        raise ValueError(f"trigger_reason must be 'event' or 'scheduled', got {trigger_reason!r}")

    derivation_hash = compute_derivation_hash(derivation)

    body = dict(derivation)
    body["trigger_reason"] = trigger_reason
    body["derivation_hash"] = derivation_hash

    url = f"{settings.core_base_url}{INGEST_PATH}"
    headers = {
        "Authorization": f"Bearer {settings.core_ingest_api_key}",
        "Idempotency-Key": derivation_hash,
        "Content-Type": "application/json",
    }

    # timed_stage logs "ingest_push.start"/"ingest_push.end" on success, or
    # "ingest_push.failed" (ERROR, with duration_ms + error_type + error) if
    # _post_with_retry raises after exhausting retries (TransientIngestError)
    # or hits an immediate permanent rejection (PermanentIngestError) -- this
    # is the one place that knows ai_system_id/trigger_reason/derivation_hash
    # at the moment of failure, so it logs structurally here in addition to
    # the broad catch-and-log the callers (event_listener.py / scheduler.py)
    # already do as a last line of defense.
    with timed_stage(
        logger,
        "ingest_push",
        ai_system_id=derivation.get("ai_system_id"),
        trigger_reason=trigger_reason,
        derivation_hash=derivation_hash,
    ):
        response = _post_with_retry(url, body, headers)

    result: dict[str, Any] = response.json()
    return result


BATCH_INGEST_PATH = "/api/v1/patent-ingest/p2/obligation-derivations/batch"


def push_derivations_batch(derivations: list[dict[str, Any]], trigger_reason: str) -> dict[str, Any]:
    """Batch variant of push_derivation: one HTTP round-trip for N
    derivations instead of N (used by scheduler.py's safety-net sweep, which
    can cover thousands of ai_systems per run -- see PATENT.md's
    performance-at-scale hardening pass).

    Each derivation keeps its OWN `derivation_hash` (computed the same way as
    the single-item path -- content-derived, excludes graph_path/timestamps),
    since core's batch route dedupes/validates per item, not per HTTP call.
    There is no single batch-level Idempotency-Key: the meaningful dedupe
    unit is still one derivation, not one HTTP request, so each item's own
    hash is what matters (see compute_derivation_hash's docstring).

    Retries the WHOLE batch (same tenacity policy as the single-item path) on
    a transient error -- a 4xx from core here means the request envelope
    itself was rejected (e.g. malformed batch body), not a validation failure
    of an individual item; per-item validation failures come back as
    `{"ok": false, ...}` entries in a 200 response, not as an HTTP error.

    Returns core's `{"results": [{"ai_system_id", "ok", "result"|"error"}, ...]}`
    body unchanged -- callers (scheduler.py) are responsible for inspecting
    per-item outcomes.
    """
    if trigger_reason not in ("event", "scheduled"):
        raise ValueError(f"trigger_reason must be 'event' or 'scheduled', got {trigger_reason!r}")
    if not derivations:
        return {"results": []}

    items = []
    for derivation in derivations:
        item = dict(derivation)
        item["trigger_reason"] = trigger_reason
        item["derivation_hash"] = compute_derivation_hash(derivation)
        items.append(item)

    url = f"{settings.core_base_url}{BATCH_INGEST_PATH}"
    headers = {
        "Authorization": f"Bearer {settings.core_ingest_api_key}",
        "Content-Type": "application/json",
    }

    with timed_stage(
        logger,
        "ingest_push_batch",
        trigger_reason=trigger_reason,
        batch_size=len(items),
    ):
        response = _post_with_retry(url, {"derivations": items}, headers)

    result: dict[str, Any] = response.json()
    return result


# ---------------------------------------------------------------------------
# Graph-structure push (closes the "who populates governance_graph_nodes/
# edges in core" gap -- see core-side-patch/ASSUMPTIONS.md item 22). Pushes
# the FULL node/edge set the satellite just built (graph_builder.build_graph's
# output, via serialize_graph_structure()), not a per-ai_system subset -- the
# satellite always fetches/builds the whole graph in one pass anyway (see
# graph_builder.py), so there is no meaningfully smaller "this traversal's
# structure" to push instead. Core-side upsert-by-natural-key
# (core-side-patch/models.py's upsert_graph_structure) makes repeated pushes
# of an unchanged graph a cheap no-op, so callers (event_listener.py /
# scheduler.py) push unconditionally after every fetch_and_build_graph()
# call rather than trying to detect "did the structure actually change"
# client-side first.
# ---------------------------------------------------------------------------

GRAPH_STRUCTURE_PATH = "/api/v1/patent-ingest/p2/graph-structure"


def compute_structure_hash(structure: dict[str, Any]) -> str:
    """sha256 hex digest of the canonical (sort_keys=True) JSON of the
    `nodes`/`edges` lists -- the graph-structure analogue of
    compute_derivation_hash(). structure["nodes"]/["edges"] are already
    sorted by serialize_graph_structure(), so this hash is stable across
    repeated builds of the same underlying export data, not just within one
    process."""
    canonical_subset = {"nodes": structure["nodes"], "edges": structure["edges"]}
    canonical_json = json.dumps(canonical_subset, sort_keys=True)
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def push_graph_structure(structure: dict[str, Any]) -> dict[str, Any]:
    """POST a graph-structure snapshot (the dict shape returned by
    graph_builder.serialize_graph_structure) to core's graph-structure
    ingest endpoint.

    Same idempotency approach as push_derivation: `structure_hash` is
    attached both as a request body field and as the `Idempotency-Key`
    header. Same retry policy (transient errors retried up to 3 attempts,
    4xx treated as permanent and never retried) via the shared
    `_post_with_retry` helper.
    """
    structure_hash = compute_structure_hash(structure)

    body = dict(structure)
    body["structure_hash"] = structure_hash

    url = f"{settings.core_base_url}{GRAPH_STRUCTURE_PATH}"
    headers = {
        "Authorization": f"Bearer {settings.core_ingest_api_key}",
        "Idempotency-Key": structure_hash,
        "Content-Type": "application/json",
    }

    with timed_stage(
        logger,
        "graph_structure_push",
        node_count=len(structure["nodes"]),
        edge_count=len(structure["edges"]),
        structure_hash=structure_hash,
    ):
        response = _post_with_retry(url, body, headers)

    result: dict[str, Any] = response.json()
    return result
