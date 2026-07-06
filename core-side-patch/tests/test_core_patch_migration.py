# Patch for CompliVibe core (app.complivibe.in), lands on top of migration head 0175.
# Test-harness only (not part of the patch set shipped to core).
#
# Migration file structure sanity: import the migration module directly by
# file path (its filename starts with a digit, so it can't be `import`ed with
# normal dotted syntax -- this mirrors how Alembic itself loads revision
# scripts) and verify upgrade()/downgrade() exist and touch exactly the three
# PATENT.md table names, in the right order, without needing a live Postgres.
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

MIGRATION_PATH = Path(__file__).resolve().parent.parent / "migrations" / "0176_add_governance_graph_tables.py"

EXPECTED_TABLES = [
    "governance_graph_nodes",
    "governance_graph_edges",
    "governance_graph_traversal_results",
]


def _load_migration_module():
    spec = importlib.util.spec_from_file_location("core_patch_migration_0176", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _RecordingOp:
    """Fake alembic `op` proxy that just records calls -- lets us execute the
    migration's upgrade()/downgrade() bodies without a live Alembic
    MigrationContext / real database connection."""

    def __init__(self):
        self.calls: list[tuple[str, tuple, dict]] = []

    def __getattr__(self, name):
        def _recorder(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return None

        return _recorder

    def create_table_names(self) -> list[str]:
        return [args[0] for (name, args, _kw) in self.calls if name == "create_table"]

    def drop_table_names(self) -> list[str]:
        return [args[0] for (name, args, _kw) in self.calls if name == "drop_table"]


@pytest.fixture()
def migration_module():
    return _load_migration_module()


def test_migration_has_revision_identifiers(migration_module):
    assert migration_module.revision == "0176_governance_graph"
    assert migration_module.down_revision == "0175"


def test_migration_has_upgrade_and_downgrade(migration_module):
    assert callable(migration_module.upgrade)
    assert callable(migration_module.downgrade)


def test_upgrade_creates_exactly_the_three_patent_tables(migration_module):
    fake_op = _RecordingOp()
    migration_module.op = fake_op

    migration_module.upgrade()

    created = fake_op.create_table_names()
    assert created == EXPECTED_TABLES


def test_upgrade_creates_expected_indexes(migration_module):
    fake_op = _RecordingOp()
    migration_module.op = fake_op

    migration_module.upgrade()

    index_calls = [(args[0], args[2]) for (name, args, _kw) in fake_op.calls if name == "create_index"]
    index_columns = {name: cols for name, cols in index_calls}

    assert index_columns["ix_governance_graph_nodes_org_id"] == ["org_id"]
    assert index_columns["ix_governance_graph_nodes_org_node_type"] == ["org_id", "node_type"]
    assert index_columns["ix_governance_graph_edges_org_source"] == ["org_id", "source_node_id"]
    assert index_columns["ix_governance_graph_edges_edge_type"] == ["edge_type"]
    assert index_columns["ix_governance_graph_traversal_results_org_ai_system"] == [
        "org_id",
        "ai_system_id",
    ]


def test_downgrade_drops_the_three_tables_in_reverse_dependency_order(migration_module):
    fake_op = _RecordingOp()
    migration_module.op = fake_op

    migration_module.downgrade()

    dropped = fake_op.drop_table_names()
    assert dropped == list(reversed(EXPECTED_TABLES))


def test_upgrade_enables_pgvector_extension(migration_module):
    fake_op = _RecordingOp()
    migration_module.op = fake_op

    migration_module.upgrade()

    execute_calls = [args[0] for (name, args, _kw) in fake_op.calls if name == "execute"]
    assert any("CREATE EXTENSION IF NOT EXISTS vector" in sql for sql in execute_calls)


def test_downgrade_does_not_drop_pgvector_extension(migration_module):
    fake_op = _RecordingOp()
    migration_module.op = fake_op

    migration_module.downgrade()

    execute_calls = [args[0] for (name, args, _kw) in fake_op.calls if name == "execute"]
    assert not any("DROP EXTENSION" in sql for sql in execute_calls)


def test_upgrade_creates_hnsw_index_on_embedding_column(migration_module):
    """Without an ANN index, every similarity search over
    governance_graph_nodes.embedding is a full table scan. HNSW (not IVFFlat)
    is used because IVFFlat needs representative data present at build time
    for good recall, and this table starts empty -- see the migration's
    module docstring."""
    fake_op = _RecordingOp()
    migration_module.op = fake_op

    migration_module.upgrade()

    index_creates = [(args, kwargs) for (name, args, kwargs) in fake_op.calls if name == "create_index"]
    embedding_index = next(
        ((args, kwargs) for (args, kwargs) in index_creates if args[0] == "ix_governance_graph_nodes_embedding_hnsw"),
        None,
    )
    assert embedding_index is not None, "expected an HNSW index on governance_graph_nodes.embedding"

    args, kwargs = embedding_index
    assert args[1] == "governance_graph_nodes"
    assert args[2] == ["embedding"]
    assert kwargs.get("postgresql_using") == "hnsw"
    assert kwargs.get("postgresql_ops") == {"embedding": "vector_cosine_ops"}


def test_downgrade_drops_hnsw_index_before_dropping_its_table(migration_module):
    fake_op = _RecordingOp()
    migration_module.op = fake_op

    migration_module.downgrade()

    call_names_in_order = [name for (name, _args, _kw) in fake_op.calls]
    drop_index_calls = [(args, kwargs) for (name, args, kwargs) in fake_op.calls if name == "drop_index"]
    assert any(args[0] == "ix_governance_graph_nodes_embedding_hnsw" for (args, _kw) in drop_index_calls)

    # The index must be dropped no later than its owning table.
    index_pos = call_names_in_order.index("drop_index")
    nodes_table_drop_pos = max(
        i
        for i, (name, args, _kw) in enumerate(fake_op.calls)
        if name == "drop_table" and args[0] == "governance_graph_nodes"
    )
    assert index_pos <= nodes_table_drop_pos
