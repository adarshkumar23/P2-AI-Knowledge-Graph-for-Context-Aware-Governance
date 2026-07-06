# Test coverage

Generated with `pytest-cov` (MIT). Run:

```
pytest --cov=src/p2_satellite --cov=core-side-patch --cov-report=term-missing --cov-report=html:htmlcov
open htmlcov/index.html   # line-by-line HTML report
```

## Current numbers

**Overall: 98% line coverage** (2,651 statements, 51 missed), 251 tests, zero
failures. No file is below 70% -- the lowest are `src/p2_satellite/embeddings.py`
at 85% and `core-side-patch/models.py`/`core-side-patch/reference_traversal_cte.py`
at 89% (as of the last full run before this file's own polish pass; models.py
was subsequently brought to 99% -- see "What was added" below).

This number is not a target to defend -- it's a byproduct of the fact that
almost every module in this repo already has dedicated unit tests written
alongside the code that introduced it (see `tests/unit/`, `tests/stress/`,
and `core-side-patch/tests/`). The goal of this pass was to make sure that
was actually true, not to pad the percentage.

## What was added (this pass)

`core-side-patch/models.py::upsert_graph_structure` (new in this pass, closes
`core-side-patch/ASSUMPTIONS.md` item 22) had several real, untested branches
after the router-level tests (`test_core_patch_graph_structure_ingest.py`)
were written -- those only ever exercised "everything created" and "one edge
deactivated." `test_core_patch_models_upsert_graph_structure.py` was added to
cover the branches that were genuinely missing, not to inflate a number:

- a node's `properties` dict actually changing between two pushes
- an archived node being revived by a fresh push
- an edge's `weight` actually changing
- an edge's `properties` actually changing
- an edge referencing a node from a **prior** push rather than the current
  one (the `_resolve_node_id` fallback path via `get_node_by_natural_key`)
- `get_node_by_natural_key` returning `None` for an unknown node, and being
  org-scoped

Each of these is a distinct code path where a bug would mean a "changed"
structure push either silently duplicates a row or fails to persist the
change -- exactly the kind of untested logic worth closing, as opposed to
adding assertions to already-covered lines.

## What was deliberately NOT chased

- `core-side-patch/models.py:181` (inside `load_active_catalog`): a
  `node_type is None or node_key is None: continue` guard against columns
  that are `nullable=False` in the schema. The comment right above it says
  why it exists (satisfying the type checker's honest view of what a
  `Column` can statically type as, not a runtime possibility) -- forcing a
  `None` into a NOT NULL column just to cover this line would be padding,
  not a real test.
- The remaining low-differential gaps in `src/p2_satellite/embeddings.py`
  (85%, mostly the lazy sentence-transformers model-loading branch, which
  every existing test deliberately avoids exercising for real so the suite
  never needs network access to download model weights),
  `observability.py`, `schema.py`'s `raise ValueError` branches for invalid
  node/edge types, and `scheduler.py`'s APScheduler wiring are all
  pre-existing (from before this pass) defensive/error-path lines already
  reviewed in earlier hardening passes -- see `MERGE_CHECKLIST.md` and
  `STRESS_TEST_RESULTS.md`. None of them are new code from this pass, and
  none are below the 70% file-level threshold this pass used as its bar for
  intervention.
