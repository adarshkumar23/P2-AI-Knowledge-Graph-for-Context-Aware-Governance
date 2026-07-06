# Patch for CompliVibe core (app.complivibe.in), lands on top of migration head 0175.
# Test-harness only (not part of the patch set shipped to core).
from __future__ import annotations

import permissions


def test_permission_constants_are_scoped_keys_not_normal_permissions():
    assert permissions.PATENT_EXPORT_P2_READ == "patent_export:p2:read"
    assert permissions.PATENT_INGEST_P2_WRITE == "patent_ingest:p2:write"


def test_scoped_permission_set_contains_both():
    assert {
        "patent_export:p2:read",
        "patent_ingest:p2:write",
    } == permissions.SCOPED_API_KEY_PERMISSIONS
