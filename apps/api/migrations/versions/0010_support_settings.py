"""support settings

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-25 14:41:47.046652
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0010'
down_revision = '0009'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('support_settings',
    sa.Column('organisation_id', sa.UUID(), nullable=False),
    sa.Column('timezone', sa.String(length=64), server_default='UTC', nullable=False),
    sa.Column('weekly_hours', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('holidays', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('sla_targets', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('last_assigned_membership_id', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text("timezone('utc', now())"), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text("timezone('utc', now())"), nullable=False),
    sa.ForeignKeyConstraint(['last_assigned_membership_id'], ['memberships.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['organisation_id'], ['organisations.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('organisation_id')
    )


def downgrade() -> None:
    op.drop_table('support_settings')
