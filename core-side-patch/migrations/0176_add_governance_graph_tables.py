"""Add governance_graph_nodes / governance_graph_edges / governance_graph_traversal_results

Patch for CompliVibe core (app.complivibe.in), lands on top of migration head 0175.
Field names / existing-pattern assumptions below are best-effort from PATENT.md and
must be verified against the real ai_system model / AuditService / permission system
before merging -- see core-side-patch/ASSUMPTIONS.md.

Revision ID: 0176_governance_graph
Revises: 0175
Create Date: 2026-07-06

ASSUMPTIONS (see core-side-patch/ASSUMPTIONS.md for the full list):
  - down_revision="0175" assumes the real head revision id is literally the
    string "0175"; core's actual Alembic revision ids are probably longer
    hashes and 0175 is just the migration's sequence number/description from
    CLAUDE_CODE_GOAL_PROMPT.md ("migration head 0175") -- a human MUST replace
    this with the real down_revision id string before this migration will
    apply cleanly.
  - Uses sa.BigInteger autoincrement primary keys for all three new tables;
    we could not verify whether core's convention is integer or UUID PKs.
  - Adds `CREATE EXTENSION IF NOT EXISTS vector` in upgrade() (required by the
    embedding Vector(384) column) and deliberately does NOT drop the extension
    in downgrade(), in case other tables/patches already depend on it.
  - Adds a UNIQUE(org_id, node_type, node_key) constraint on
    governance_graph_nodes beyond PATENT.md's literal column list, so re-
    ingesting the same export can't duplicate nodes -- a judgment call, not
    a literal spec requirement; flag for review.
  - Adds an HNSW approximate-nearest-neighbor index on
    governance_graph_nodes.embedding (postgresql_ops vector_cosine_ops).
    HNSW (not IVFFlat) was chosen because IVFFlat requires a representative
    sample of data to already be present at index-build time for good recall
    (its clustering step is trained on whatever rows exist when the index is
    built) -- this table starts empty at migration time and grows
    incrementally as nodes are ingested, which is exactly the case HNSW
    handles better (no training/build-time data requirement, degrades more
    gracefully as data accumulates). Requires pgvector >= 0.5.0 for HNSW
    support -- if core's Postgres has an older pgvector build, this index
    creation will fail and must be swapped for IVFFlat (with a subsequent
    REINDEX once real data exists) or deferred to a follow-up migration.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

# --- Alembic identifiers ----------------------------------------------------
revision = "0176_governance_graph"
down_revision = "0175"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Required for the embedding Vector(384) column below.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "governance_graph_nodes",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("org_id", sa.BigInteger(), nullable=False),
        sa.Column("node_type", sa.String(length=64), nullable=False),
        sa.Column("node_key", sa.String(length=255), nullable=False),
        sa.Column("properties", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"),
        sa.Column("embedding", Vector(384), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("archived", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_governance_graph_nodes_org_id", "governance_graph_nodes", ["org_id"])
    op.create_index("ix_governance_graph_nodes_org_node_type", "governance_graph_nodes", ["org_id", "node_type"])
    op.create_unique_constraint(
        "uq_governance_graph_nodes_org_type_key",
        "governance_graph_nodes",
        ["org_id", "node_type", "node_key"],
    )
    # Approximate-nearest-neighbor index on the embedding column -- without
    # this, every semantic node-similarity search is a full table scan (see
    # module docstring for why HNSW over IVFFlat here). cosine distance
    # (vector_cosine_ops) matches sentence-transformers' normalized embeddings
    # (src/p2_satellite/config.py EMBEDDING_DIM=384, all-MiniLM-L6-v2).
    op.create_index(
        "ix_governance_graph_nodes_embedding_hnsw",
        "governance_graph_nodes",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_with={"m": 16, "ef_construction": 64},
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )

    op.create_table(
        "governance_graph_edges",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("org_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "source_node_id",
            sa.BigInteger(),
            sa.ForeignKey("governance_graph_nodes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "target_node_id",
            sa.BigInteger(),
            sa.ForeignKey("governance_graph_nodes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("edge_type", sa.String(length=64), nullable=False),
        sa.Column("weight", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column("properties", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.create_index("ix_governance_graph_edges_org_id", "governance_graph_edges", ["org_id"])
    op.create_index("ix_governance_graph_edges_org_source", "governance_graph_edges", ["org_id", "source_node_id"])
    op.create_index("ix_governance_graph_edges_edge_type", "governance_graph_edges", ["edge_type"])

    op.create_table(
        "governance_graph_traversal_results",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("org_id", sa.BigInteger(), nullable=False),
        sa.Column("ai_system_id", sa.String(length=64), nullable=False),
        sa.Column(
            "traversal_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("input_context", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"),
        sa.Column(
            "derived_obligations",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
        sa.Column(
            "derived_controls",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
        sa.Column("graph_path", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("methodology_version", sa.String(length=32), nullable=False),
        sa.Column("trigger_reason", sa.String(length=16), nullable=False),
        sa.Column("validation_status", sa.String(length=32), nullable=False),
    )
    op.create_index("ix_governance_graph_traversal_results_org_id", "governance_graph_traversal_results", ["org_id"])
    op.create_index(
        "ix_governance_graph_traversal_results_org_ai_system",
        "governance_graph_traversal_results",
        ["org_id", "ai_system_id"],
    )


def downgrade() -> None:
    op.drop_table("governance_graph_traversal_results")
    op.drop_table("governance_graph_edges")
    # Dropping the table would drop this index implicitly too, but we drop it
    # explicitly first for symmetry with upgrade() and so this stays correct
    # if the index is ever split into its own follow-up migration.
    op.drop_index("ix_governance_graph_nodes_embedding_hnsw", table_name="governance_graph_nodes")
    op.drop_table("governance_graph_nodes")
    # Deliberately NOT dropping the `vector` extension -- other tables/patches
    # may depend on it already existing. See ASSUMPTIONS.md.
