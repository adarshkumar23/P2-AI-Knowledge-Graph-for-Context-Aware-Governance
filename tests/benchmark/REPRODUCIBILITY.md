# REPRODUCIBILITY.md — Workstream E Benchmark

How to independently reproduce the benchmark evidence in
`PATENT_TECHNICAL_EFFECT.md`, from a clean checkout, with no manual setup
beyond installing dependencies.

## Requirements

- Python 3.10+ (developed/verified on Python 3.12.1)
- No network access required. No environment variables required beyond
  `src/p2_satellite/config.py`'s built-in defaults (it loads a `.env` if
  present via `python-dotenv`, but every setting has a default and the
  benchmark never touches `httpx`/network code paths — it calls
  `graph_builder.build_graph()`, the pure function, directly with in-memory
  dicts, never `fetch_and_build_graph()`).
- No live core backend, no database, no pgvector — this benchmark exercises
  only `src/p2_satellite/graph_builder.py`'s `build_graph()` and
  `src/p2_satellite/traversal.py`'s `derive_obligations()`, both pure
  in-process functions.

## Steps

From the repository root:

```bash
pip install -r requirements.txt
pytest tests/benchmark/
```

That's it. `tests/benchmark/eu_india_biometric_case.py` is registered as a
pytest test module via `pytest.ini`'s `python_files` setting (it is
intentionally not named `test_*.py` — see the comment in `pytest.ini` —
because its filename is fixed by PATENT.md's "Required Evidence Before
Filing" section / `CLAUDE_CODE_GOAL_PROMPT.md` Workstream E).

To run the whole repository's test suite instead (all workstreams,
including this benchmark):

```bash
pytest
```

## Expected output

Running `pytest tests/benchmark/ -v` on this codebase produces (captured
verbatim on 2026-07-06, Python 3.12.1, pytest 8.2.2):

```
============================= test session starts ==============================
platform linux -- Python 3.12.1, pytest-8.2.2, pluggy-1.6.0 -- .../bin/python3
cachedir: .pytest_cache
rootdir: /workspaces/P2-AI-Knowledge-Graph-for-Context-Aware-Governance
configfile: pytest.ini
plugins: anyio-4.12.1
collecting ... collected 10 items

tests/benchmark/eu_india_biometric_case.py::test_fixture_expected_set_matches_the_catalog_it_claims_to_summarize PASSED [ 10%]
tests/benchmark/eu_india_biometric_case.py::test_naive_lookup_returns_nonempty_but_incomplete PASSED [ 20%]
tests/benchmark/eu_india_biometric_case.py::test_naive_lookup_drops_dpdp_entirely PASSED [ 30%]
tests/benchmark/eu_india_biometric_case.py::test_naive_lookup_drops_high_risk_additions PASSED [ 40%]
tests/benchmark/eu_india_biometric_case.py::test_naive_lookup_drops_both_role_specific_obligations PASSED [ 50%]
tests/benchmark/eu_india_biometric_case.py::test_graph_traversal_returns_the_complete_correct_set PASSED [ 60%]
tests/benchmark/eu_india_biometric_case.py::test_graph_traversal_spans_all_three_regulations PASSED [ 70%]
tests/benchmark/eu_india_biometric_case.py::test_graph_traversal_includes_both_controller_and_processor_obligations PASSED [ 80%]
tests/benchmark/eu_india_biometric_case.py::test_graph_traversal_methodology_and_shape PASSED [ 90%]
tests/benchmark/eu_india_biometric_case.py::test_graph_traversal_is_a_strict_superset_of_naive_lookup PASSED [100%]

============================== 10 passed in 0.33s ==============================
```

Running the full repository suite (`pytest`, no path argument) at the time
this benchmark was written produces `122 passed` (this benchmark's 10 tests
plus all of Workstreams A–D's unit/integration tests) — i.e. this
benchmark introduces zero regressions elsewhere.

## What each test/assertion proves

| Test | Proves |
|---|---|
| `test_fixture_expected_set_matches_the_catalog_it_claims_to_summarize` | The benchmark's documented "expected complete obligation set" is not a hand-typed guess — it is mechanically reconstructable from the raw regulations catalog data (a drift guard: if fixtures.py's catalog is edited without updating the summary constants, this fails first). Also confirms the controller/processor obligation role tags match what's claimed. |
| `test_naive_lookup_returns_nonempty_but_incomplete` | The naive static lookup table is not a strawman that fails loudly (returns nothing) — it fails silently, returning a plausible non-empty answer that is nonetheless a strict subset of the correct one. |
| `test_naive_lookup_drops_dpdp_entirely` | The naive table's single-jurisdiction assumption (`geographic_scope[0]`) causes it to miss India/DPDP entirely for a joint EU-India system. |
| `test_naive_lookup_drops_high_risk_additions` | The naive table has no risk-tier reasoning, so it never surfaces EU AI Act's high-risk obligations (conformity assessment, human oversight, biometric accuracy/bias testing) even for a system explicitly classified `risk_tier="high"`. |
| `test_naive_lookup_drops_both_role_specific_obligations` | The naive table has no controller/processor role dimension at all, so both role-specific obligations (one GDPR, one DPDP) are invisible to it. |
| `test_graph_traversal_returns_the_complete_correct_set` | `build_graph()` + `derive_obligations()`, run unmodified against the identical input, return exactly the 11-obligation / 7-control complete set. |
| `test_graph_traversal_spans_all_three_regulations` | The complete set genuinely spans GDPR, EU AI Act, and DPDP — not just one or two of the three regulations in play. |
| `test_graph_traversal_includes_both_controller_and_processor_obligations` | The complete set includes both the controller-tagged and processor-tagged obligations, proving the graph result correctly reflects the dual-role deployment. |
| `test_graph_traversal_methodology_and_shape` | The result carries `methodology_version` (from `src/p2_satellite/config.py`'s `settings`, read live, not hardcoded) and a well-formed `graph_path` audit trail, matching the shape PATENT.md's "Satellites Compute, Core Decides" section requires for auditability. |
| `test_graph_traversal_is_a_strict_superset_of_naive_lookup` | Graph traversal's result is a strict superset of the naive lookup's result, and the specific gap between them is exactly the set of dimensions (DPDP, role-specific obligations) the naive table structurally cannot represent — i.e., graph traversal closes precisely the gap PATENT.md predicts a lookup table cannot close. |

## Regenerating the exact outputs quoted in PATENT_TECHNICAL_EFFECT.md

```bash
python3 -c "
import json
from src.p2_satellite import schema
from src.p2_satellite.graph_builder import build_graph
from src.p2_satellite.traversal import derive_obligations
from tests.benchmark.fixtures import AI_SYSTEM_KEY, AI_SYSTEMS_EXPORT, REGULATIONS_CATALOG_EXPORT, JURISDICTIONS_EXPORT
from tests.benchmark.naive_static_lookup import naive_static_lookup

ai_system_record = [i for i in AI_SYSTEMS_EXPORT['items'] if i['id'] == AI_SYSTEM_KEY][0]
print('naive:', json.dumps(naive_static_lookup(ai_system_record), indent=2))

g = build_graph(AI_SYSTEMS_EXPORT, REGULATIONS_CATALOG_EXPORT, JURISDICTIONS_EXPORT)
node_id = schema.node_id(schema.NODE_AI_SYSTEM, AI_SYSTEM_KEY)
result = derive_obligations(g, node_id, max_traversal_depth=6)
print('traversal:', json.dumps(result, indent=2))
"
```
