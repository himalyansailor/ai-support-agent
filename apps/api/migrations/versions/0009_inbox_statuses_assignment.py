"""inbox statuses and assignment

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-25 14:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0009'
down_revision = '0008'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint('ck_conversations_status', 'conversations', type_='check')
    op.execute("UPDATE conversations SET status = 'open' WHERE status = 'pending_human'")
    op.create_check_constraint(
        'ck_conversations_status', 'conversations', "status IN ('bot', 'open', 'pending', 'resolved', 'closed')"
    )
    op.add_column('conversations', sa.Column('assignee_membership_id', sa.UUID(), nullable=True))
    op.add_column('conversations', sa.Column('priority', sa.String(length=8), server_default='normal', nullable=False))
    op.add_column('conversations', sa.Column('first_response_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('conversations', sa.Column('first_response_due_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('conversations', sa.Column('resolution_due_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('conversations', sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('conversations', sa.Column('sla_breached', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False))
    op.create_foreign_key(
        'fk_conversations_assignee_membership_id_memberships', 'conversations', 'memberships',
        ['assignee_membership_id'], ['id'], ondelete='SET NULL',
    )
    op.create_check_constraint(
        'ck_conversations_priority', 'conversations', "priority IN ('low', 'normal', 'high', 'urgent')"
    )
    op.create_index('ix_conversations_assignee_membership_id', 'conversations', ['assignee_membership_id'], unique=False)
    op.create_index(
        'ix_conversations_org_status_assignee_last_message', 'conversations',
        ['organisation_id', 'status', 'assignee_membership_id', 'last_message_at'], unique=False,
    )


def downgrade() -> None:
    op.drop_index('ix_conversations_org_status_assignee_last_message', table_name='conversations')
    op.drop_index('ix_conversations_assignee_membership_id', table_name='conversations')
    op.drop_constraint('ck_conversations_priority', 'conversations', type_='check')
    op.drop_constraint('fk_conversations_assignee_membership_id_memberships', 'conversations', type_='foreignkey')
    for column in ('sla_breached', 'resolved_at', 'resolution_due_at', 'first_response_due_at', 'first_response_at', 'priority', 'assignee_membership_id'):
        op.drop_column('conversations', column)
    op.drop_constraint('ck_conversations_status', 'conversations', type_='check')
    op.execute("UPDATE conversations SET status = 'pending_human' WHERE status IN ('open', 'pending')")
    op.create_check_constraint(
        'ck_conversations_status', 'conversations', "status IN ('bot', 'pending_human', 'resolved', 'closed')"
    )
