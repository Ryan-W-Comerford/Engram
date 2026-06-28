"""Add incidents table

Revision ID: 002
Revises: 001
Create Date: 2026-05-22
"""

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from alembic import op

    op.execute("""
        DO $$ BEGIN
            CREATE TYPE severity AS ENUM ('low', 'medium', 'high', 'critical');
        EXCEPTION WHEN duplicate_object THEN null;
        END $$;
    """)

    op.execute("""
        DO $$ BEGIN
            CREATE TYPE incidentstatus AS ENUM ('open', 'investigating', 'resolved');
        EXCEPTION WHEN duplicate_object THEN null;
        END $$;
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS incidents (
            id                   UUID          PRIMARY KEY,
            project_id           UUID          NOT NULL REFERENCES projects(id),
            detected_at          TIMESTAMP     NOT NULL,
            current_errors       VARCHAR(20)   NOT NULL,
            baseline_avg         VARCHAR(20)   NOT NULL,
            spike_ratio          VARCHAR(20)   NOT NULL,
            title                VARCHAR(500),
            severity             severity,
            ai_summary           TEXT,
            affected_endpoints   JSONB,
            recommended_actions  JSONB,
            deployment_event_id  UUID          REFERENCES events(id),
            status               incidentstatus NOT NULL DEFAULT 'open',
            ai_analyzed          VARCHAR(5)    NOT NULL DEFAULT 'false',
            created_at           TIMESTAMP     NOT NULL
        )
    """)

    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_incidents_project_detected
        ON incidents (project_id, detected_at)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_incidents_status
        ON incidents (status)
    """)


def downgrade() -> None:
    from alembic import op
    op.execute("DROP INDEX IF EXISTS idx_incidents_status")
    op.execute("DROP INDEX IF EXISTS idx_incidents_project_detected")
    op.execute("DROP TABLE IF EXISTS incidents")
    op.execute("DROP TYPE IF EXISTS incidentstatus")
    op.execute("DROP TYPE IF EXISTS severity")