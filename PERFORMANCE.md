# PERFORMANCE.md — graph build + traversal at scale

Measured, not assumed, per the production-hardening pass's requirement.
Reproduce with:

```
pip install -r requirements.txt
python3 scripts/benchmark_scale.py 1000 10000
```

## Methodology

Synthetic `ai_system` inventories of size N (1,000 / 10,000), built the same
way as `tests/benchmark/fixtures.py`'s EU-India case in shape (jurisdiction,
data-category, and risk-tier cycling across a small set of realistic values),
against a **fixed-size** regulations/jurisdictions catalog (3 regulations,
~11 obligations, ~7 control types — the same catalog `tests/benchmark/`
uses). This mirrors production shape: the number of AI systems grows over
time; the regulatory catalog itself does not scale with it.

For each N: time `graph_builder.build_graph()` once, then time
`traversal.derive_obligations()` once per ai_system (i.e. N traversal calls),
using the unmodified, already-hardened production code
(`src/p2_satellite/graph_builder.py`, `src/p2_satellite/traversal.py`) —
no shortcuts or a separate "fast path" written just for this benchmark.

## Results (reference hardware: this environment's container, single run)

| N      | Graph size            | `build_graph()` | `derive_obligations()` × N | avg/system | combined |
|--------|------------------------|------------------|------------------------------|------------|----------|
| 1,000  | 1,031 nodes / 4,027 edges  | 0.031s | 0.118s  | 0.118ms | **0.149s** |
| 10,000 | 10,031 nodes / 40,027 edges | 0.131s | 1.438s  | 0.144ms | **1.569s** |

**Requirement check:** "if it's not sub-second at 1,000 systems, profile and
fix before calling this done" — combined time at 1,000 systems is **0.149s**,
well under one second; no profiling/fixing was needed. At 10,000 systems the
combined time (1.57s) is still small in absolute terms, and per-system
traversal cost stays roughly flat (0.118ms → 0.144ms/system), indicating
near-linear scaling rather than the quadratic blowup that would come from,
e.g., an accidental O(n²) construction in `build_graph()`.

## Why this scales the way it does

- `build_graph()` is a single linear pass over the three export payloads,
  adding one node/edge per relationship — no nested per-ai_system scan over
  the regulatory catalog.
- `derive_obligations()`'s traversal cost is bounded by the REACHABLE
  subgraph from one `ai_system` node (a handful of jurisdictions/data
  categories → a small, fixed regulatory catalog → obligations/controls),
  not by the total graph size — so per-system cost stays roughly constant as
  N grows, since each ai_system's own local neighborhood doesn't get bigger
  just because there are more OTHER ai_systems in the graph.
- The one dimension that does scale with N is the number of `derive_obligations()`
  calls itself (once per ai_system) — which is why total traversal time scales
  ~linearly with N while per-call cost stays flat.

## Where this could stop scaling

- **A single process's memory** holding the full NetworkX graph — at some N
  (likely far beyond 10,000 ai_systems, since regulatory-graph fan-out per
  system is small) this becomes the real limit, not traversal CPU time. Not
  measured here; if the satellite's target deployment scale is known to
  approach this, a follow-up memory-profiling pass is warranted.
- **The ingest batch endpoint's rate limit** (`core-side-patch/rate_limiter.py`,
  default 100/60s per scoped key, charging the FULL batch size in one atomic
  check — see `require_ingest_rate_limit_n`) means an UNCHUNKED single batch
  of 10,000 derivations would instantly exceed the limit. This is why
  `scheduler.py` does NOT send one giant batch: it chunks a sweep into groups
  of at most `settings.ingest_batch_chunk_size` (default 50) and paces
  `settings.ingest_batch_pace_seconds` (default 30s) between chunks — a
  10,000-system sweep becomes 200 chunked calls spread over ~100 minutes,
  comfortably inside `SAFETY_NET_POLL_HOURS` (default 2h) and never tripping
  core's flood protection. These two satellite-side values and core's real
  rate limit are NOT independently tunable — they must be coordinated before
  go-live at large fleet sizes (see `MERGE_CHECKLIST.md` and
  `core-side-patch/ASSUMPTIONS.md` item 16).
- This benchmark does not exercise the live HTTP export/ingest round-trip at
  scale (network latency, core-side DB write latency for 10,000 rows) — only
  the satellite's own in-process compute. See `tests/integration/test_end_to_end_dry_run.py`
  for the (small-scale) live-HTTP correctness proof; a real load test against
  a live/staging core deployment is a separate, not-yet-done exercise.
