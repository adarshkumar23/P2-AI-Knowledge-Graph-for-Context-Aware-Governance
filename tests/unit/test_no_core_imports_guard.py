"""
Unit tests for scripts/check_no_core_imports.sh (Workstream F).

This is the "CI fails if violated" mechanism the goal prompt asks for
(CLAUDE_CODE_GOAL_PROMPT.md Workstream F: "A lint/test guard ... that fails
CI if any file in src/p2_satellite/ imports from the core backend package"),
expressed as a pytest test so it runs under the same `pytest` invocation as
every other workstream's tests instead of requiring a separate CI step.

Two things are covered:
  1. The guard actually passes against the real src/p2_satellite/ tree today
     (proves the satellite is currently clean, not just that the script
     exists).
  2. The guard actually detects a violation when one exists (proves the
     guard has teeth -- a script that always exits 0 would trivially "pass"
     test 1 without protecting anything).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "check_no_core_imports.sh"
SATELLITE_DIR = REPO_ROOT / "src" / "p2_satellite"


def _run_guard(target_dir: Path) -> subprocess.CompletedProcess:
    if not SCRIPT_PATH.exists():
        pytest.fail(f"guard script not found at {SCRIPT_PATH}")
    return subprocess.run(
        ["bash", str(SCRIPT_PATH), str(target_dir)],
        capture_output=True,
        text=True,
    )


def test_guard_passes_against_real_satellite_tree():
    """The real src/p2_satellite/ tree must currently be clean of any
    `app.*` import -- this is the satellite's core architectural boundary
    (agent-push / inbound-only, PATENT.md constraint #1)."""
    result = _run_guard(SATELLITE_DIR)

    assert result.returncode == 0, (
        f"check_no_core_imports.sh unexpectedly failed against "
        f"{SATELLITE_DIR}:\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "OK" in result.stdout


def test_guard_detects_a_deliberately_bad_import(tmp_path):
    """Prove the guard has teeth: build a tiny throwaway package containing
    one file with a forbidden `from app.models import Foo` import, and
    confirm the script exits non-zero and names the offending file."""
    bad_file = tmp_path / "bad_module.py"
    bad_file.write_text("from app.models import Foo\n" "\n" "def use_it():\n" "    return Foo()\n")

    # A clean sibling file should not, by itself, cause a false positive.
    clean_file = tmp_path / "clean_module.py"
    clean_file.write_text("import json\n\ndef ok():\n    return json.dumps({})\n")

    result = _run_guard(tmp_path)

    assert result.returncode != 0, (
        f"expected the guard to fail against a directory containing a "
        f"forbidden import, but it exited 0:\nstdout={result.stdout}"
    )
    assert "bad_module.py" in result.stderr
    assert "FAIL" in result.stderr


if __name__ == "__main__":
    sys.exit(pytest.main([__file__]))
