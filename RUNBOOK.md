# RUNBOOK — P2 Satellite (AI Knowledge Graph for Context-Aware Governance)

For an on-call engineer with no other context. Each section: what you'd
observe, what it means, what to actually do. See `README.md`'s "Failure
modes" table for the one-line version of each of these; this doc is the
longer, do-this-now version.

---

## Validation mismatch rate is spiking

**What you'd see:** repeated `governance_graph.obligation_derivation_mismatch`
WARNING log lines from core (`core-side-patch/routers/patent_ingest_p2.py`),
or `core-side-patch/mismatch_metrics.py`'s `MismatchMetrics.mismatch_rate()`
trending up.

**What it means:** core's independent re-derivation (its own reference CTE)
disagrees with what the satellite submitted for one or more `ai_system`s.
Per PATENT.md's "Satellites Compute, Core Decides" contract, core has
already refused to silently trust the satellite here — no bad data has been
written to `ai_system_obligation_links`. This is the system catching itself,
not a "clean this up before anyone notices" situation.

**What to do:**

1. **Do NOT disable core's re-validation step to unblock ingestion.** That
   would reintroduce the exact class of bug PATENT.md calls out (the "P4
   satellite rebuild" boundary violation). Never do this, regardless of how
   urgent it feels.
2. Pause the satellite's ingest push instead (satellite-side config/feature
   flag — the satellite is agent-push, so this is the satellite's call, not
   core's) while flagged rows accumulate in `governance_graph_traversal_results`
   for review. If there's no flag wired yet, stopping the satellite process
   (or its scheduler) achieves the same thing.
3. Pull a sample of `governance_graph_traversal_results` rows with
   `validation_status="flagged_mismatch"` and compare `derived_obligations`
   (satellite's submission) against `reference_derived_obligations` (core's
   own re-derivation, returned in the ingest response and — if you added
   logging for it — in the WARNING log's context).
4. Most likely causes, roughly in order of likelihood:
   - **`MAX_TRAVERSAL_DEPTH` mismatch** between the satellite's
     `src/p2_satellite/config.py` setting and whatever core's
     `_resolve_max_traversal_depth()` (currently hardcoded to `6`, see
     `core-side-patch/ASSUMPTIONS.md` item 11) is actually using in
     production. Check both values match.
   - **Stale/out-of-sync graph** — the satellite derived against an export
     snapshot that's since changed on core's side before the push landed.
     Check the timestamps: how far apart is `traversal_at` on the
     `governance_graph_traversal_results` row vs. the most recent relevant
     `governance_graph_change_events` row for that `ai_system_id`.
   - **The untested Postgres-literal-CTE path disagreeing with the
     satellite's NetworkX traversal** in some way the SQLite fallback path
     used in this repo's own tests never surfaced — see
     `core-side-patch/ASSUMPTIONS.md` item 12 (no live Postgres integration
     test exists yet). If the mismatch pattern looks structural (not
     timing-related, affects many/all systems), suspect this first.
   - Real bug in either `src/p2_satellite/traversal.py` or
     `core-side-patch/reference_traversal_cte.py` diverging from the shared
     reference (`tests/fixtures/reference_cte.py`) — if so, this is a
     genuine regression; re-run `pytest tests/unit/test_traversal.py` and
     `pytest core-side-patch/tests/test_core_patch_reference_traversal_cte.py`
     first to check whether the cross-validation tests still pass on the
     current code (if they don't, you've found it).
5. Once root-caused and fixed, re-enable the satellite's ingest push and
   watch the mismatch rate return to baseline before considering it resolved.

---

## The scheduler is falling behind (safety-net poll not completing within `SAFETY_NET_POLL_HOURS`)

**What you'd see:** `safety_net_poll.summary` log events (from
`src/p2_satellite/scheduler.py`) arriving further apart than
`SAFETY_NET_POLL_HOURS`, or APScheduler logging missed-job warnings, or the
poll simply taking so long it's still running when the next interval fires
(APScheduler's `replace_existing=True` + default `max_instances` behavior —
check whether a second run gets skipped or queued; if you haven't configured
`max_instances` explicitly, only one instance of the job runs at a time by
default, so a slow run delays the next one rather than overlapping it).

**What it means:** almost always one of:

- **Fleet size has grown past what the current chunk/pace settings assume.**
  `scheduler.py` chunks a sweep into groups of `INGEST_BATCH_CHUNK_SIZE`
  (default 50), pausing `INGEST_BATCH_PACE_SECONDS` (default 30s) between
  chunks specifically so it never bursts past core's ingest rate limit (see
  `PERFORMANCE.md`). At 10,000 systems with the defaults, that's ~200 chunks
  × 30s ≈ 100 minutes just in pacing sleeps, on top of actual compute/HTTP
  time — comfortably inside a 2-hour `SAFETY_NET_POLL_HOURS`, but if the
  fleet has grown to (say) 50,000 systems, the same defaults now take ~500
  minutes and will blow past a 2-hour window.
- **Core's export or ingest endpoints have gotten slower** (DB load,
  network). Check `graph_build.*` and `ingest_push*.*` structured log
  durations (`duration_ms` field, see "Observability" in README.md) for a
  step-change vs. historical baseline.
- **An actual code-level performance regression.** Run
  `python3 scripts/benchmark_scale.py 1000 10000` and compare against the
  numbers in `PERFORMANCE.md` — if `build_graph()`/`derive_obligations()`
  are dramatically slower than documented there, that's your answer, not a
  scale/config issue.

**What to do:**

1. Check current fleet size (`ai_system` count in the graph) against the
   defaults' assumptions above. If the fleet has grown significantly,
   `INGEST_BATCH_CHUNK_SIZE` and/or `INGEST_BATCH_PACE_SECONDS` likely need
   retuning **in coordination with core's real ingest rate limit** (see
   `core-side-patch/rate_limiter.py` and `MERGE_CHECKLIST.md`) — raising the
   satellite's chunk size without raising core's limit just moves the
   bottleneck to a wall of `429`s instead.
2. If it's a one-off slow run (not a trend), it's very likely fine —
   `SAFETY_NET_POLL_HOURS` exists precisely because event-triggered
   derivation is the primary path; a late safety-net sweep is a delayed
   backstop, not a missed primary signal, unless it's ALSO true that
   event-triggered derivations are failing (check `event-triggered
   derivation starting`/`.failed` logs from `event_listener.py` too).
3. If it's a trend, re-run the benchmark script, compare to `PERFORMANCE.md`,
   and follow the profiling guidance there if a real regression is found.

---

## Manually re-triggering a single system's derivation

Two ways, depending on what you're trying to test/fix:

**Simulate the event-triggered path** (fastest, exercises the real webhook
contract): POST a correctly HMAC-signed payload to the satellite's
`/events/ai-system-changed` endpoint yourself. You need
`EVENT_LISTENER_SHARED_SECRET` and the exact signing scheme from
`src/p2_satellite/event_listener.py`'s `verify_signature()` docstring
(`X-P2-Signature: t=<unix_epoch_seconds>,v1=sha256=<hex hmac of f"{t}." +
raw body>`) — a one-off Python snippet using `hmac`/`hashlib` from the
stdlib is the fastest way to construct this by hand; don't hand-craft the
hex digest manually.

```python
import hashlib, hmac, json, time
secret = "..."  # EVENT_LISTENER_SHARED_SECRET
body = json.dumps({"ai_system_id": "the-system-id", "changed_field": "risk_tier"}).encode()
t = int(time.time())
sig = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
headers = {"X-P2-Signature": f"t={t},v1=sha256={sig}"}
# POST body/headers to http://<satellite host>:<EVENT_LISTENER_PORT>/events/ai-system-changed
```

**Call the traversal/push path directly** (no HTTP, useful for debugging in
a shell/REPL on the satellite host): import and call the same functions
`event_listener.process_ai_system_changed()` uses —

```python
from src.p2_satellite import schema
from src.p2_satellite.graph_builder import fetch_and_build_graph
from src.p2_satellite.traversal import derive_obligations
from src.p2_satellite.ingest_client import push_derivation

graph = fetch_and_build_graph()
node_id = schema.node_id(schema.NODE_AI_SYSTEM, "the-system-id")
derivation = derive_obligations(graph, node_id)
push_derivation(derivation, trigger_reason="event")
```

Either way, watch core's ingest response (`validation_status`) and, if it
comes back `flagged_mismatch`, follow the "Validation mismatch rate is
spiking" section above for that one system.

---

## Webhook requests are being rejected (401/403) unexpectedly

- **401, "invalid or missing signature"** — check the shared secret matches
  on both sides (`EVENT_LISTENER_SHARED_SECRET`), and that whatever is
  sending the webhook is using the CURRENT signing scheme
  (`t=<ts>,v1=sha256=<hex over "{t}." + body>`) — the older
  `sha256=<hex over body alone>` scheme (pre-hardening-pass) is no longer
  accepted.
- **401, stale timestamp** — clock skew between core and the satellite host
  exceeding `EVENT_WEBHOOK_MAX_CLOCK_SKEW_SECONDS` (default 300s). Check NTP
  sync on both hosts before widening this window.
- **401, replay rejected** — the exact same signed payload was already
  processed once. This is almost certainly a retry-on-timeout on the
  sender's side that doesn't realize the first attempt actually succeeded
  (network partition after the satellite responded 202, before the sender
  saw it) — check whether the corresponding derivation already landed
  (`governance_graph_traversal_results`) despite the "failed" appearance on
  the sender's side. If so, no action needed; the safety-net poll would have
  caught this anyway.
- **403, IP not allowed** — `EVENT_LISTENER_IP_ALLOWLIST` is set and the
  request's source IP isn't on it. Either the allowlist is stale (core's
  egress IPs changed) or something unexpected is hitting this endpoint —
  treat the latter as a possible security event, not just a config typo,
  until confirmed otherwise.

---

## Ingest requests are getting 429'd

Core's per-scoped-key rate limit (`core-side-patch/rate_limiter.py`,
stopgap default 100 derivations/60s) has been exceeded. If this is the
safety-net poll's batch push tripping it, `scheduler.py`'s
`INGEST_BATCH_CHUNK_SIZE`/`INGEST_BATCH_PACE_SECONDS` are supposed to
prevent exactly this — see the "scheduler falling behind" section above for
what to check first (they may be out of sync with core's actual configured
limit, which only core's team can confirm — see
`core-side-patch/ASSUMPTIONS.md` item 16). If it's NOT the scheduler (e.g.
sustained 429s outside of poll windows), treat it as a possible
compromised-or-buggy-satellite-instance event per the rate limiter's
original design intent, not just a tuning problem.
