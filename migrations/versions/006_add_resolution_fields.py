"""add resolution fields to incidents

Revision ID: 006
Revises: 005
Create Date: 2026-06-28
"""
from alembic import op
import sqlalchemy as sa

revision = '006'
down_revision = '005'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('incidents', sa.Column('resolved_at', sa.DateTime(), nullable=True))
    op.add_column('incidents', sa.Column('resolution_note', sa.Text(), nullable=True))


def downgrade():
    op.drop_column('incidents', 'resolution_note')
    op.drop_column('incidents', 'resolved_at')
