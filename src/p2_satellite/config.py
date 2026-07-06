"""
Central configuration for the P2 satellite.
Loaded once, imported everywhere. No config values scattered across files.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _env(key: str, default: str | None = None, required: bool = False) -> str:
    val = os.getenv(key, default)
    if required and not val:
        raise RuntimeError(f"Missing required env var: {key}")
    if val is None:
        # Genuinely reachable if a future call site passes required=False with
        # no default -- every call in load_settings() below always supplies
        # one or the other today, but the signature previously promised `str`
        # while silently returning None in that case. Fail loudly instead.
        raise RuntimeError(f"Missing env var {key!r} and no default was provided")
    return val


@dataclass(frozen=True)
class Settings:
    core_base_url: str
    core_export_api_key: str
    core_ingest_api_key: str
    safety_net_poll_hours: float
    event_listener_host: str
    event_listener_port: int
    event_listener_shared_secret: str
    max_traversal_depth: int
    embedding_model: str
    embedding_dim: int
    methodology_version: str
    # -- production-hardening additions (webhook replay/freshness + IP allowlist) --
    event_webhook_max_clock_skew_seconds: float
    event_listener_ip_allowlist: str
    # -- production-hardening additions (safety-net batch push pacing) --
    # The safety-net poll sends ALL derivations for one sweep through
    # push_derivations_batch() rather than one HTTP call per ai_system (see
    # scheduler.py). Core's ingest rate limiter (core-side-patch/rate_limiter.py)
    # charges the FULL batch size in one atomic check against a per-scoped-key
    # window (default stopgap: 100 units / 60s) -- an unchunked burst of
    # thousands of derivations in one call would instantly exceed that limit.
    # scheduler.py chunks large sweeps into groups of at most
    # ingest_batch_chunk_size, pausing ingest_batch_pace_seconds between
    # chunks. These two values and core's rate limit are NOT independently
    # tunable -- they must be coordinated (chunk_size should stay at or below
    # core's configured limit, and the pace should keep the sustained rate
    # under core's limit/window) before go-live at large fleet sizes. See
    # MERGE_CHECKLIST.md and core-side-patch/ASSUMPTIONS.md item 16.
    ingest_batch_chunk_size: int
    ingest_batch_pace_seconds: float


def load_settings() -> Settings:
    return Settings(
        core_base_url=_env("CORE_BASE_URL", "http://localhost:8000"),
        core_export_api_key=_env("CORE_EXPORT_API_KEY", "dev-export-key"),
        core_ingest_api_key=_env("CORE_INGEST_API_KEY", "dev-ingest-key"),
        safety_net_poll_hours=float(_env("SAFETY_NET_POLL_HOURS", "2")),
        event_listener_host=_env("EVENT_LISTENER_HOST", "0.0.0.0"),
        event_listener_port=int(_env("EVENT_LISTENER_PORT", "8501")),
        event_listener_shared_secret=_env("EVENT_LISTENER_SHARED_SECRET", "dev-secret"),
        max_traversal_depth=int(_env("MAX_TRAVERSAL_DEPTH", "6")),
        embedding_model=_env("EMBEDDING_MODEL", "all-MiniLM-L6-v2"),
        embedding_dim=int(_env("EMBEDDING_DIM", "384")),
        methodology_version=_env("METHODOLOGY_VERSION", "p2-v1.0.0"),
        event_webhook_max_clock_skew_seconds=float(_env("EVENT_WEBHOOK_MAX_CLOCK_SKEW_SECONDS", "300")),
        # Comma-separated IPs; empty (default) disables the allowlist entirely
        # -- opt-in defense-in-depth alongside HMAC verification, see
        # event_listener.py for the enforcement + rationale.
        event_listener_ip_allowlist=_env("EVENT_LISTENER_IP_ALLOWLIST", ""),
        # Conservative defaults: 50 derivations/chunk, 30s pace between chunks
        # -- comfortably under core's default 100/60s stopgap rate limit even
        # accounting for clock/window-boundary imprecision. MUST be tuned in
        # lockstep with core's real rate limit before large-fleet go-live.
        ingest_batch_chunk_size=int(_env("INGEST_BATCH_CHUNK_SIZE", "50")),
        ingest_batch_pace_seconds=float(_env("INGEST_BATCH_PACE_SECONDS", "30")),
    )


settings = load_settings()
