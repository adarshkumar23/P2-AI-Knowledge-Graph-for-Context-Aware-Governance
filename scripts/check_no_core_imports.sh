#!/usr/bin/env bash
# check_no_core_imports.sh -- Workstream F guard.
#
# Enforces the P2 satellite's "agent-push / inbound-only" architectural rule
# (see PATENT.md / CLAUDE_CODE_GOAL_PROMPT.md, constraint #1): the satellite
# must NEVER import the core backend package (`app.*`). If any generated or
# hand-written code under the target directory imports from `app.*`, that is
# a bug -- this script catches it so CI fails loudly instead of silently
# shipping a boundary violation.
#
# Usage:
#   scripts/check_no_core_imports.sh [TARGET_DIR]
#
# TARGET_DIR defaults to src/p2_satellite (relative to the current working
# directory). Passing an explicit directory lets tests point this script at
# a throwaway fixture tree to prove it actually detects violations, without
# needing to modify the real satellite source.
#
# Deliberately grep-based (no AST parsing) per the goal prompt: "a simple
# grep-based test is fine." Matches, at the start of a logical import line
# (optionally indented):
#   import app.<anything>
#   from app.<anything> import ...
#   from app import ...
#
# Exit code 0  -> no violations found (prints a short "OK" line).
# Exit code 1  -> target directory missing, or one or more violations found
#                 (prints every offending file:line to stderr).

set -euo pipefail

TARGET_DIR="${1:-src/p2_satellite}"

if [ ! -d "$TARGET_DIR" ]; then
  echo "check_no_core_imports: target directory not found: $TARGET_DIR" >&2
  exit 1
fi

# Anchored to the start of the line (allowing leading whitespace, so imports
# nested inside a function/conditional are still caught).
PATTERN='^[[:space:]]*(import[[:space:]]+app\.|from[[:space:]]+app\.|from[[:space:]]+app[[:space:]]+import)'

MATCHES="$(grep -rnE "$PATTERN" --include='*.py' "$TARGET_DIR" || true)"

if [ -n "$MATCHES" ]; then
  echo "FAIL: forbidden import(s) of the core backend package ('app.*') found under $TARGET_DIR:" >&2
  echo "$MATCHES" >&2
  echo >&2
  echo "The P2 satellite must never import app.* -- it only talks to core over" >&2
  echo "the documented HTTP export/ingest surface (agent-push / inbound-only)." >&2
  echo "See README.md and PATENT.md 'Satellite Architecture'." >&2
  exit 1
fi

echo "OK: no forbidden 'app.*' imports found under $TARGET_DIR"
exit 0
