"""Initial schema

Revision ID: 001
Revises:
Create Date: 2026-05-22
"""

revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    from alembic import op

    op.execute("""
        DO $$ BEGIN
            CREATE TYPE eventtype AS ENUM ('error', 'trace', 'deployment');
        EXCEPTION WHEN duplicate_object THEN null;
        END $$;
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS projects (
            id          UUID         PRIMARY KEY,
            name        VARCHAR(255) NOT NULL,
            api_key_hash VARCHAR(64) NOT NULL UNIQUE,
            created_at  TIMESTAMP    NOT NULL
        )
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id          UUID        PRIMARY KEY,
            project_id  UUID        NOT NULL REFERENCES projects(id),
            event_type  eventtype   NOT NULL,
            environment VARCHAR(50) NOT NULL DEFAULT 'production',
            service     VARCHAR(255),
            timestamp   TIMESTAMP   NOT NULL,
            raw_data    JSONB       NOT NULL,
            created_at  TIMESTAMP   NOT NULL
        )
    """)

    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_events_project_timestamp
        ON events (project_id, timestamp)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_events_event_type
        ON events (event_type)
    """)


def downgrade() -> None:
    from alembic import op
    op.execute("DROP INDEX IF EXISTS idx_events_event_type")
    op.execute("DROP INDEX IF EXISTS idx_events_project_timestamp")
    op.execute("DROP TABLE IF EXISTS events")
    op.execute("DROP TABLE IF EXISTS projects")
    op.execute("DROP TYPE IF EXISTS eventtype")