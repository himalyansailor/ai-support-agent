"""saved replies

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-25 16:41:47.585119
"""
from alembic import op
import sqlalchemy as sa


revision = '0013'
down_revision = '0012'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('saved_replies',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('title', sa.String(length=100), nullable=False),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('shortcut', sa.String(length=30), nullable=True),
    sa.Column('created_by_membership_id', sa.UUID(), nullable=True),
    sa.Column('organisation_id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text("timezone('utc', now())"), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text("timezone('utc', now())"), nullable=False),
    sa.ForeignKeyConstraint(['created_by_membership_id'], ['memberships.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['organisation_id'], ['organisations.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_saved_replies_created_by_membership_id', 'saved_replies', ['created_by_membership_id'], unique=False)
    op.create_index(op.f('ix_saved_replies_organisation_id'), 'saved_replies', ['organisation_id'], unique=False)
    op.create_index('uq_saved_replies_organisation_id_shortcut', 'saved_replies', ['organisation_id', 'shortcut'], unique=True, postgresql_where=sa.text('shortcut IS NOT NULL'))


def downgrade() -> None:
    op.drop_index('uq_saved_replies_organisation_id_shortcut', table_name='saved_replies', postgresql_where=sa.text('shortcut IS NOT NULL'))
    op.drop_index(op.f('ix_saved_replies_organisation_id'), table_name='saved_replies')
    op.drop_index('ix_saved_replies_created_by_membership_id', table_name='saved_replies')
    op.drop_table('saved_replies')
