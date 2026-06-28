"""add daily_digests table

Revision ID: 005
Revises: 004
Create Date: 2026-06-14
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision     = "005"
down_revision = "004"
branch_labels = None
depends_on    = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE daily_digests (
            id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            date       DATE NOT NULL,
            summary    TEXT,
            data       JSONB,
            created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_digest_project_date UNIQUE (project_id, date)
        )
    """)
    op.execute("""
        CREATE INDEX idx_daily_digests_project_date
            ON daily_digests (project_id, date DESC)
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS daily_digests")
