"""open-source single-tenant: drop API keys, collapse to one project

Engram is now a self-hosted, single-tenant tool. There are no per-project API
keys and no admin key. This migration:
  - drops projects.api_key_hash (and its UNIQUE constraint)
  - seeds the one implicit project row every event/incident hangs off of
  - re-points any pre-existing events/incidents/digests onto it
  - removes every other project row

Revision ID: 008
Revises: 007
Create Date: 2026-08-28
"""
from alembic import op

revision = "008"
down_revision = "007"
branch_labels = None
depends_on = None

DEFAULT_PROJECT_ID = "00000000-0000-0000-0000-000000000001"


def upgrade() -> None:
    op.execute("ALTER TABLE projects DROP COLUMN IF EXISTS api_key_hash")

    # 1. Seed the single implicit project.
    op.execute(
        f"""
        INSERT INTO projects (id, name, created_at)
        VALUES ('{DEFAULT_PROJECT_ID}', 'default', NOW())
        ON CONFLICT (id) DO NOTHING
        """
    )

    # 2. Re-point everything that used to belong to some other project.
    #    (Must happen before deleting the old project rows — daily_digests
    #     has ON DELETE CASCADE.)
    for table in ("events", "incidents", "daily_digests"):
        op.execute(
            f"UPDATE {table} SET project_id = '{DEFAULT_PROJECT_ID}' "
            f"WHERE project_id <> '{DEFAULT_PROJECT_ID}'"
        )

    # 3. Drop every other project.
    op.execute(f"DELETE FROM projects WHERE id <> '{DEFAULT_PROJECT_ID}'")


def downgrade() -> None:
    # api_key_hash was NOT NULL UNIQUE; re-add it nullable since the original
    # secrets can't be reconstructed. The project collapse is not reversible.
    op.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS api_key_hash VARCHAR(64)")
