"""
Event listener (Workstream D) -- the PRIMARY half of the hybrid trigger.

Receives HMAC-signed change-event notifications from core's outbox (see
PATENT.md "Satellite Architecture" -> HYBRID TRIGGER (a)) when a watched
ai_system property (deployment_jurisdiction, data_categories, risk_tier)
changes, and immediately re-runs traversal for that one ai_system.

This is EVENT-TRIGGERED DERIVATION, not "real-time" sync -- see PATENT.md
CHANGE LOG. The safety-net reconciliation poll in scheduler.py is the other,
independent half of the hybrid trigger and exists to catch events this
listener misses or fails to process.

Satellite remains agent-push / inbound-only: this endpoint only *receives*
a notification that something changed; the satellite still pulls the graph
data itself via graph_builder.fetch_and_build_graph() and pushes results out
via ingest_client.push_derivation(). Core never calls into satellite logic
directly, and the satellite never calls back into core except through the
documented export/ingest HTTP surface.

WIRE FORMAT (production-hardening pass)
----------------------------------------
    X-P2-Signature: t=<unix_epoch_seconds>,v1=sha256=<hex>

where <hex> = HMAC-SHA256(shared_secret, f"{t}.".encode() + raw_body).

This replaces an earlier signature-only scheme (sha256=<hex over body alone>)
that had no freshness/replay component -- a captured valid signed payload
could have been replayed forever. Binding the timestamp into the HMAC input
(rather than checking it out-of-band) means an attacker cannot take a
genuine (body, signature) pair from time T and claim a new timestamp T' at
replay time: the signature would no longer match, since the timestamp is
part of what was signed. Nothing in production calls this webhook yet, so
this is a deliberate breaking wire-format change with no backward-compat
shim; see tests/unit/test_event_listener.py for updated tests.

Two independent checks reject a request:
  1. Freshness: |now - t| > settings.event_webhook_max_clock_skew_seconds.
     `now` always comes from time.time() at verification time -- never from
     anything caller-supplied, since accepting a caller-supplied "now" would
     let a replay attacker simply claim a fresh timestamp.
  2. Replay: the exact (t, hex) pair has already been accepted once before.
     Freshness alone is not a replay guard -- it still permits replaying a
     captured payload as many times as the attacker likes within the whole
     skew window. See `_is_first_use` for the in-process TTL-bounded
     seen-set used to catch this.

IP allowlist (defense in depth, opt-in)
----------------------------------------
settings.event_listener_ip_allowlist (comma-separated IPs, default empty)
adds an optional exact-IP check ahead of signature verification. Empty (the
default) leaves behavior unchanged -- HMAC alone gates access, exactly as
before. This is intentionally NOT a replacement for HMAC verification: this
satellite-only repo has no visibility into core's real network topology
(whether it has stable static egress IPs at all, or sits behind a NAT/shared
proxy pool), so the allowlist is opt-in hardening for deployments where a
stable IP set is known, not a assumed default. CIDR support would be a
reasonable future enhancement if core's real egress turns out to be a range
rather than static IPs -- out of scope here, exact-match is sufficient today.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request, Response, status
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel

from src.p2_satellite import metrics, schema
from src.p2_satellite.concurrency import try_acquire_ai_system_processing
from src.p2_satellite.config import settings

# TODO(integration): confirm exact signature once graph_builder.py / traversal.py land
from src.p2_satellite.graph_builder import fetch_and_build_graph, serialize_graph_structure
from src.p2_satellite.ingest_client import push_derivation, push_graph_structure
from src.p2_satellite.observability import (
    get_logger,
    install_secret_redaction,
    log_event,
    timed_stage,
)
from src.p2_satellite.scheduler import start_scheduler, stop_scheduler
from src.p2_satellite.traversal import derive_obligations

logger = get_logger(__name__)
install_secret_redaction(logger)

SIGNATURE_HEADER = "X-P2-Signature"
SIGNATURE_PREFIX = "sha256="

# --------------------------------------------------------------------------
# Replay guard: in-process TTL-bounded, size-capped set of (timestamp, signature)
# pairs already accepted. Plain dict keyed by (t, provided_hex) -> monotonic expiry,
# swept lazily on each check (no background thread/dependency needed).
#
# WHY A SIZE CAP IS NEEDED (stress-test finding):
# The previous comment claimed this "can never grow beyond roughly (request rate)
# x (skew window) entries." That is correct for attackers using INVALID HMACs
# (those fail before _is_first_use). But a legitimate flood — or an attacker who
# knows the shared secret — with unique timestamps within the skew window CAN
# grow this dict at (request rate) × (skew_window + 5s). With the default 300s
# window, 1000 req/s would put 305,000 entries in the dict. MAX_REPLAY_CACHE_SIZE
# caps the physical dict size and evicts the entry with the EARLIEST (soonest-
# to-expire) expiry when the cap is reached — this is the least-valuable entry
# since it will expire and become un-replayable soonest regardless.
# --------------------------------------------------------------------------
MAX_REPLAY_CACHE_SIZE: int = 10_000  # hard cap; exported for regression tests
_seen_signatures_lock = threading.Lock()
_seen_signatures: dict[tuple[int, str], float] = {}

# Wire the Prometheus gauge to a live callable rather than updating it at
# every insert/evict site inside _is_first_use -- see metrics.py's
# set_replay_cache_size_source docstring.
metrics.set_replay_cache_size_source(lambda: len(_seen_signatures))


def _is_first_use(t: int, provided_hex: str) -> bool:
    """Returns True (and records the pair) if (t, provided_hex) has not been
    seen before; returns False if this is a replay of an already-accepted
    signature. Module-level singleton so it persists across requests within
    one process -- that's sufficient here since event_listener.py and
    scheduler.py both run in-process (see concurrency.py for the analogous
    reasoning on the processing lock).

    Size cap: if the cache is at MAX_REPLAY_CACHE_SIZE after TTL eviction,
    evict the single entry with the earliest expiry (soonest-to-expire =
    least-valuable replay-protection) before inserting the new one. This
    prevents unbounded growth under high-volume valid-signature floods.
    See stress test ST-4 and the module-level comment above."""
    now_monotonic = time.monotonic()
    key = (t, provided_hex)
    with _seen_signatures_lock:
        expired = [k for k, expiry in _seen_signatures.items() if expiry <= now_monotonic]
        for k in expired:
            del _seen_signatures[k]

        if key in _seen_signatures:
            return False

        # Enforce size cap: evict the soonest-to-expire entry if we're at max.
        if len(_seen_signatures) >= MAX_REPLAY_CACHE_SIZE:
            # min() over all values to find the entry with the earliest expiry.
            evict_key = min(_seen_signatures, key=lambda k: _seen_signatures[k])
            del _seen_signatures[evict_key]

        # Retain a bit longer than the skew window so a signature timestamped
        # at the edge of the window can't be replayed just as its record
        # expires from under it.
        _seen_signatures[key] = now_monotonic + settings.event_webhook_max_clock_skew_seconds + 5.0
        return True


class AiSystemChangedEvent(BaseModel):
    ai_system_id: str
    changed_field: str | None = None


def _parse_signature_header(signature_header: str) -> tuple[int, str] | None:
    """Parses `t=<epoch>,v1=sha256=<hex>` -> (epoch, hex), or None if the
    header is missing/malformed in any way."""
    parts = signature_header.split(",")
    if len(parts) != 2:
        return None

    t_part, v1_part = parts
    if not t_part.startswith("t="):
        return None
    v1_prefix = f"v1={SIGNATURE_PREFIX}"
    if not v1_part.startswith(v1_prefix):
        return None

    try:
        t = int(t_part[len("t=") :])
    except ValueError:
        return None

    provided_hex = v1_part[len(v1_prefix) :]
    if not provided_hex:
        return None

    return t, provided_hex


def verify_signature(raw_body: bytes, signature_header: str | None) -> bool:
    """Verifies an inbound event notification's signature header.

    Convention: the caller (core's outbox dispatcher) sends
        X-P2-Signature: t=<unix_epoch_seconds>,v1=sha256=<hex>
    where <hex> = HMAC-SHA256(shared_secret, f"{t}.".encode() + raw_body).

    Returns True iff all of the following hold:
      1. The header is present and well-formed (parses into t + hex).
      2. |time.time() - t| is within settings.event_webhook_max_clock_skew_seconds.
      3. The HMAC recomputed over f"{t}.".encode() + raw_body matches `hex`
         (constant-time compare).
      4. This exact (t, hex) pair has not already been accepted before (see
         `_is_first_use` -- the actual replay guard, distinct from #2's
         freshness check).

    This is intentionally a small, pure(ish), reusable function (rather than
    inlined in the route handler) since a security-sensitive verification
    deserves to be independently unit-testable and not duplicated if a
    second webhook route is ever added.
    """
    if not signature_header:
        return False

    parsed = _parse_signature_header(signature_header)
    if parsed is None:
        return False
    t, provided_hex = parsed

    # `now` always comes from time.time() at verification time -- never
    # caller-supplied, or a replay attacker could just claim a fresh `now`.
    now = time.time()
    if abs(now - t) > settings.event_webhook_max_clock_skew_seconds:
        return False

    expected_hex = hmac.new(
        settings.event_listener_shared_secret.encode("utf-8"),
        f"{t}.".encode() + raw_body,
        hashlib.sha256,
    ).hexdigest()

    # hmac.compare_digest is constant-time -- avoids leaking match-length via
    # timing side channels.
    if not hmac.compare_digest(provided_hex, expected_hex):
        return False

    return _is_first_use(t, provided_hex)


def _client_ip_allowed(request: Request) -> bool:
    """Opt-in IP allowlist check -- see module docstring. Empty allowlist
    (the default) means this check is a no-op (always allowed); HMAC
    verification is the real gate in that case."""
    allowlist_raw = settings.event_listener_ip_allowlist
    if not allowlist_raw.strip():
        return True

    allowlist = {ip.strip() for ip in allowlist_raw.split(",") if ip.strip()}
    client_host = request.client.host if request.client else None
    return client_host in allowlist


def process_ai_system_changed(ai_system_id: str, changed_field: str | None) -> None:
    """Background job body: immediate event-triggered re-derivation for one
    ai_system. Runs via FastAPI BackgroundTasks so the HTTP response (202)
    isn't held open while the traversal/ingest push completes -- this is
    intentionally NOT a separate task queue; FastAPI's built-in
    BackgroundTasks is sufficient at this codebase's scale.

    Guarded by concurrency.try_acquire_ai_system_processing so this can never
    race the safety-net poll (scheduler._run_safety_net_poll) doing the same
    work for the same ai_system_id at the same time -- see concurrency.py's
    module docstring for the design rationale.
    """
    with try_acquire_ai_system_processing(ai_system_id) as acquired:
        if not acquired:
            log_event(
                logger,
                logging.WARNING,
                "event_derivation.skipped_in_flight",
                ai_system_id=ai_system_id,
                trigger_reason="event",
                changed_field=changed_field,
            )
            return

        with timed_stage(
            logger,
            "event_derivation",
            ai_system_id=ai_system_id,
            trigger_reason="event",
            changed_field=changed_field,
        ):
            target_node_id = schema.node_id(schema.NODE_AI_SYSTEM, ai_system_id)

            # TODO(integration): a targeted incremental pull (e.g. changed_since
            # or a scoped fetch for just this ai_system's neighborhood) would be
            # more efficient than a full re-pull on every single event; full
            # pull is correct but not optimally efficient, and is acceptable
            # for now.
            graph = fetch_and_build_graph(changed_since=None)

            # Push the freshly-built graph's structure BEFORE deriving/pushing
            # this ai_system's own obligations -- core's node/edge tables
            # should already reflect the graph a derivation was computed
            # against by the time that derivation lands (see
            # core-side-patch/ASSUMPTIONS.md item 22). A failure here is
            # logged and swallowed, not fatal to this event's derivation --
            # a stale graph-structure snapshot in core is a visibility/
            # staleness problem for Features 2/3/4/6, not a correctness
            # problem for THIS ai_system's own re-derivation (which core
            # cross-checks independently against whatever it already has).
            try:
                push_graph_structure(serialize_graph_structure(graph))
                metrics.INGEST_PUSH_TOTAL.labels(push_kind="graph_structure", outcome="success").inc()
            except Exception:
                metrics.INGEST_PUSH_TOTAL.labels(push_kind="graph_structure", outcome="failure").inc()
                log_event(
                    logger,
                    logging.ERROR,
                    "event_derivation.graph_structure_push_failed",
                    exc_info=True,
                    ai_system_id=ai_system_id,
                    trigger_reason="event",
                )

            metrics.TRAVERSAL_TOTAL.labels(trigger_reason="event").inc()
            with metrics.TRAVERSAL_DURATION_SECONDS.labels(trigger_reason="event").time():
                derivation = derive_obligations(graph, target_node_id)

            try:
                response = push_derivation(derivation, trigger_reason="event")
                metrics.INGEST_PUSH_TOTAL.labels(push_kind="derivation", outcome="success").inc()
                metrics.record_validation_status(response.get("validation_status"))
            except Exception:
                metrics.INGEST_PUSH_TOTAL.labels(push_kind="derivation", outcome="failure").inc()
                log_event(
                    logger,
                    logging.ERROR,
                    "event_derivation.push_derivation_failed",
                    exc_info=True,
                    ai_system_id=ai_system_id,
                    trigger_reason="event",
                )


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    # Safety-net reconciliation poll -- NOT the primary trigger path, NOT
    # real-time. See scheduler.py docstring / PATENT.md CHANGE LOG.
    start_scheduler()
    try:
        yield
    finally:
        stop_scheduler()


app = FastAPI(title="P2 Satellite Event Listener", lifespan=_lifespan)


@app.get("/metrics")
def metrics_endpoint() -> Response:
    """Prometheus scrape target -- see metrics.py's module docstring for
    what's exposed (traversal count/duration, validation-mismatch counter,
    replay-cache size gauge, ingest push success/failure counters) and why
    this lives on the satellite rather than only on core's side."""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/events/ai-system-changed", status_code=status.HTTP_202_ACCEPTED)
async def ai_system_changed(
    request: Request,
    background_tasks: BackgroundTasks,
    x_p2_signature: str | None = Header(default=None),
) -> dict[str, str]:
    raw_body = await request.body()

    if not _client_ip_allowed(request):
        log_event(
            logger,
            logging.WARNING,
            "webhook.rejected",
            reason="ip_not_allowlisted",
            client_ip=(request.client.host if request.client else None),
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="client ip not allowlisted")

    if not verify_signature(raw_body, x_p2_signature):
        log_event(logger, logging.WARNING, "webhook.rejected", reason="invalid_signature_or_replay")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid or missing signature")

    # model_validate_json raises pydantic.ValidationError (not HTTPException) if
    # the body is not valid JSON or doesn't match AiSystemChangedEvent's shape.
    # Without this try/except the error propagates as an unhandled 500 -- a
    # caller who presents a valid HMAC signature over malformed JSON would crash
    # the request handler. Catch and return 422 instead ("Unprocessable Content"
    # is the correct HTTP status for a syntactically-valid request whose body
    # fails schema validation). The signature was valid (attacker knew the secret
    # but sent garbage JSON), so we don't re-raise as 401 -- that would confuse
    # the caller about whether the signature or the body was the problem.
    # See stress test ST-4 (test_4_adversarial_payloads_and_replay_cache_bounded).
    try:
        event = AiSystemChangedEvent.model_validate_json(raw_body)
    except Exception as exc:
        log_event(
            logger,
            logging.WARNING,
            "webhook.rejected",
            reason="invalid_body",
            error=str(exc)[:200],  # cap length; don't log raw attacker-supplied content
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="request body could not be parsed as a valid event",
        ) from exc

    log_event(
        logger,
        logging.INFO,
        "webhook.received",
        ai_system_id=event.ai_system_id,
        changed_field=event.changed_field,
    )

    background_tasks.add_task(process_ai_system_changed, event.ai_system_id, event.changed_field)

    return {"status": "accepted", "ai_system_id": event.ai_system_id}
