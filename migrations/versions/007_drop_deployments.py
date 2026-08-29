"""drop deployment tracking

Removes the deployment event type and the incidents.deployment_event_id
correlation column. Engram no longer ingests GitHub push webhooks; the
pipeline is telemetry-only (errors + traces).

Revision ID: 007
Revises: 006
Create Date: 2026-08-28
"""
from alembic import op

revision = "007"
down_revision = "006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Drop the incident -> deployment correlation link.
    op.execute("ALTER TABLE incidents DROP COLUMN IF EXISTS deployment_event_id")

    # Purge any deployment rows before narrowing the enum.
    op.execute("DELETE FROM events WHERE event_type = 'deployment'")

    # Postgres can't remove a value from an enum in place — swap the type.
    op.execute("ALTER TYPE eventtype RENAME TO eventtype_old")
    op.execute("CREATE TYPE eventtype AS ENUM ('error', 'trace')")
    op.execute(
        "ALTER TABLE events ALTER COLUMN event_type TYPE eventtype "
        "USING event_type::text::eventtype"
    )
    op.execute("DROP TYPE eventtype_old")


def downgrade() -> None:
    op.execute("ALTER TYPE eventtype RENAME TO eventtype_old")
    op.execute("CREATE TYPE eventtype AS ENUM ('error', 'trace', 'deployment')")
    op.execute(
        "ALTER TABLE events ALTER COLUMN event_type TYPE eventtype "
        "USING event_type::text::eventtype"
    )
    op.execute("DROP TYPE eventtype_old")

    op.execute(
        "ALTER TABLE incidents ADD COLUMN IF NOT EXISTS deployment_event_id "
        "UUID REFERENCES events(id)"
    )
