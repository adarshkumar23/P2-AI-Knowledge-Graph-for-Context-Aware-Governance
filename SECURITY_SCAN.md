# Security scan results (pip-audit + bandit)

Part of the open-source-tooling polish pass. Both tools run against
`src/p2_satellite/` and `core-side-patch/` (excluding tests); pip-audit runs
against `requirements.txt`. Add both to CI (`.github/workflows/ci.yml`) so
this stays true, not just true today.

## pip-audit

```
pip-audit -r requirements.txt
```

**Before this pass:** 13 known vulnerabilities across 4 packages
(`pytest`, `python-dotenv`, `starlette` [transitive via `fastapi`],
`transformers` [transitive via `sentence-transformers`]).

**After this pass: 0 known vulnerabilities.** Fixed by bumping:

| Package | Before | After | Notes |
|---|---|---|---|
| `python-dotenv` | 1.0.1 | 1.2.2 | Direct dependency, small API surface (`load_dotenv()` only) -- verified no behavior change against this repo's usage. |
| `pytest` | 8.2.2 | 9.1.1 | Dev/test-only dependency; full suite re-run and green after the bump (also bumped `pytest-cov` to a compatible release). |
| `fastapi` | 0.111.0 | 0.139.0 | Pulled in to get a compatible, patched `starlette`. Full suite green after the bump. |
| `pydantic` | 2.7.4 | 2.13.4 | Required by `fastapi==0.139.0`'s own dependency constraint; both are pydantic v2, no code changes needed. |
| `starlette` | 0.37.2 (transitive) | 1.3.1 | See "starlette 1.x note" below -- this was NOT the version pip resolved to automatically inside an already-populated environment; had to be forced explicitly and independently re-verified. |
| `sentence-transformers` | 2.7.0 | 5.6.0 | See "ML dependency note" below. |
| `transformers` | 4.57.6 (transitive) | 5.13.0 | Now an explicit direct pin (was purely transitive before) so this fix doesn't silently regress on a future `pip install` that re-resolves loosely. |

### starlette 1.x note

`fastapi==0.139.0` declares a dependency range that *permits* `starlette`
1.x, but `pip install --upgrade fastapi` inside an environment that
already had `starlette==0.37.2` installed did **not** pick up the newer
major version on its own (pip's default upgrade strategy doesn't
eagerly upgrade already-satisfied transitive dependencies) -- `pip-audit`
resolving `requirements.txt` from a clean slate is what actually
surfaced this; the two tools disagreed with each other on this
environment until `starlette` was pinned explicitly and forced. **This is
exactly the kind of drift `pip-tools`' compiled, hash-pinned
`requirements.txt` (see the dependency-pinning section of this pass) is
meant to prevent** -- a bare `pip install --upgrade <one package>` is not
a reliable way to actually land on a patched transitive dependency.

Before forcing the bump, the five specific `starlette` CVEs/advisories
were read in full (`pip-audit --desc`) and checked against this
codebase's actual usage (`grep` for `request.url`, `StaticFiles`,
`HTTPEndpoint`, `.form()` -- none found anywhere in
`src/p2_satellite/` or `core-side-patch/`), confirming none of the
vulnerable code paths were reachable even before the fix. The version
bump was still applied (defense in depth, and "0 known vulnerabilities"
is a much easier invariant to keep true over time than "N vulnerabilities,
all individually reviewed as inapplicable"), and the full test suite was
re-run and green after forcing `starlette==1.3.1` specifically (a major
version bump warranted independent verification beyond "pip's resolver
said it's compatible").

### ML dependency note (sentence-transformers / transformers)

`sentence-transformers` 2.7.0 -> 5.6.0 and `transformers` 4.57.6 -> 5.13.0
are both major-version bumps. `src/p2_satellite/embeddings.py`'s actual
usage is narrow (`SentenceTransformer(model_name)` and
`model.encode(texts)`, both called positionally) -- confirmed compatible
via `inspect.signature()` against the installed 5.x classes, and the full
test suite (which always exercises this module through an injected stub
`encode_fn`, never the real model -- see `embeddings.py`'s own docstring on
why: no guaranteed network access in this sandbox to download real model
weights) stays green. **What this pass could NOT verify**: real embedding
generation end-to-end against the actual `all-MiniLM-L6-v2` model weights,
since that requires network access this environment doesn't reliably have.
A human merging this should run the satellite once against a real network
connection and confirm `embeddings.embed_node_texts()` still produces
384-dim vectors as expected before treating this bump as fully verified in
production, not just at the API-signature level.

## bandit

```
bandit -r src/p2_satellite core-side-patch -x "*/tests/*,*/__pycache__/*"
```

**One finding, reviewed and accepted (not a code change):**

```
B104: Possible binding to all interfaces (src/p2_satellite/config.py:69)
event_listener_host=_env("EVENT_LISTENER_HOST", "0.0.0.0")
```

`0.0.0.0` is the correct default for a service meant to receive webhook
callbacks from core, especially when containerized (binding to
`127.0.0.1` inside a container would make the service unreachable from
outside that container's network namespace, breaking typical Docker/
Kubernetes deployments entirely). The bind host is already externally
configurable via `EVENT_LISTENER_HOST` for any deployment that wants to
restrict it further (e.g. behind a reverse proxy on a specific internal
interface) -- no code change needed, network exposure is controlled at the
deployment/firewall layer, which is the correct layer for this concern.

Zero other findings -- no hardcoded secrets, no unsafe deserialization, no
SQL string concatenation (all core-side-patch DB access goes through
SQLAlchemy's query builder / ORM, never raw string-formatted SQL), no
`eval`/`exec`/`pickle` usage anywhere in either directory.
