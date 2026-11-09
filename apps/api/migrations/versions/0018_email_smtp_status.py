"""email_smtp_status

The SMTP side of a mailbox gets its own health (smtp_status, smtp_error_type, smtp_last_error_at), so a failing
SMTP login no longer marks the whole mailbox auth_failed (which slowed IMAP sync down). Also: a partial index for
the outbound sweep, and the loop guard's index on lower(from_address).

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-26 22:30:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = '0018'
down_revision = '0017'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('email_mailboxes', sa.Column('smtp_status', sa.String(length=16), server_default='active', nullable=False))
    op.add_column('email_mailboxes', sa.Column('smtp_error_type', sa.String(length=64), nullable=True))
    op.add_column('email_mailboxes', sa.Column('smtp_last_error_at', sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint(
        'ck_email_mailboxes_smtp_status', 'email_mailboxes', "smtp_status IN ('active', 'auth_failed', 'error')"
    )
    op.create_index(
        'ix_email_messages_outbound_pending',
        'email_messages',
        ['delivery_status', 'sending_until'],
        unique=False,
        postgresql_where=sa.text("direction = 'outbound' AND delivery_status IN ('queued', 'retry', 'sending')"),
    )
    op.drop_index('ix_email_messages_mailbox_id_from_address_created_at', table_name='email_messages')
    op.create_index(
        'ix_email_messages_mailbox_id_lower_from_address_created_at',
        'email_messages',
        ['mailbox_id', sa.literal_column('lower(from_address)'), 'created_at'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index('ix_email_messages_mailbox_id_lower_from_address_created_at', table_name='email_messages')
    op.create_index(
        'ix_email_messages_mailbox_id_from_address_created_at',
        'email_messages',
        ['mailbox_id', 'from_address', 'created_at'],
        unique=False,
    )
    op.drop_index('ix_email_messages_outbound_pending', table_name='email_messages')
    op.drop_constraint('ck_email_mailboxes_smtp_status', 'email_mailboxes', type_='check')
    op.drop_column('email_mailboxes', 'smtp_last_error_at')
    op.drop_column('email_mailboxes', 'smtp_error_type')
    op.drop_column('email_mailboxes', 'smtp_status')
