"""Fix incident column types and upgrade vector index

- current_errors: VARCHAR(20) → INTEGER
- baseline_avg:   VARCHAR(20) → DOUBLE PRECISION
- spike_ratio:    VARCHAR(20) → DOUBLE PRECISION
- ai_analyzed:    VARCHAR(5)  → BOOLEAN  ('true'/'false' cast automatically)
- Replace IVFFlat index with HNSW (no pre-training needed, better recall at scale)

Revision ID: 004
Revises: 003
Create Date: 2026-06-14
"""

revision = "004"
down_revision = "003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from alembic import op

    # Drop the old string default before changing type — Postgres can't auto-cast
    # a string default ('false') to boolean in the same statement as the type change.
    op.execute("ALTER TABLE incidents ALTER COLUMN ai_analyzed DROP DEFAULT")

    op.execute("""
        ALTER TABLE incidents
            ALTER COLUMN current_errors TYPE integer
                USING current_errors::integer,
            ALTER COLUMN baseline_avg TYPE double precision
                USING baseline_avg::double precision,
            ALTER COLUMN spike_ratio TYPE double precision
                USING spike_ratio::double precision,
            ALTER COLUMN ai_analyzed TYPE boolean
                USING (ai_analyzed = 'true')
    """)

    op.execute("ALTER TABLE incidents ALTER COLUMN ai_analyzed SET DEFAULT false")

    # HNSW gives better recall than IVFFlat and requires no tuning as row count grows.
    # m=16, ef_construction=64 are well-tested defaults for 1536-dim embeddings.
    op.execute("DROP INDEX IF EXISTS idx_incidents_embedding")
    op.execute("""
        CREATE INDEX idx_incidents_embedding
        ON incidents
        USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 64)
    """)


def downgrade() -> None:
    from alembic import op

    op.execute("""
        ALTER TABLE incidents
            ALTER COLUMN current_errors TYPE varchar(20)
                USING current_errors::text,
            ALTER COLUMN baseline_avg TYPE varchar(20)
                USING baseline_avg::text,
            ALTER COLUMN spike_ratio TYPE varchar(20)
                USING spike_ratio::text,
            ALTER COLUMN ai_analyzed TYPE varchar(5)
                USING CASE WHEN ai_analyzed THEN 'true' ELSE 'false' END,
            ALTER COLUMN ai_analyzed SET DEFAULT 'false'
    """)

    op.execute("DROP INDEX IF EXISTS idx_incidents_embedding")
    op.execute("""
        CREATE INDEX idx_incidents_embedding
        ON incidents
        USING ivfflat (embedding vector_cosine_ops)
        WITH (lists = 100)
    """)
