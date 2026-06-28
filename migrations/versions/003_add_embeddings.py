"""Add pgvector embedding column

Revision ID: 003
Revises: 002
Create Date: 2026-05-22
"""

revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from alembic import op

    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.execute("""
        ALTER TABLE incidents
        ADD COLUMN IF NOT EXISTS embedding vector(1536)
    """)

    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_incidents_embedding
        ON incidents
        USING ivfflat (embedding vector_cosine_ops)
        WITH (lists = 100)
    """)


def downgrade() -> None:
    from alembic import op
    op.execute("DROP INDEX IF EXISTS idx_incidents_embedding")
    op.execute("""
        ALTER TABLE incidents
        DROP COLUMN IF EXISTS embedding
    """)