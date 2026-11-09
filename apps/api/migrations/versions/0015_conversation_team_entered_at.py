"""conversation team_entered_at

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-25 18:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '0015'
down_revision = '0014'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('conversations', sa.Column('team_entered_at', sa.DateTime(timezone=True), nullable=True))
    # Conversations already with the team: the best known entry time is their creation.
    op.execute("UPDATE conversations SET team_entered_at = created_at WHERE status IN ('open', 'pending')")


def downgrade() -> None:
    op.drop_column('conversations', 'team_entered_at')
