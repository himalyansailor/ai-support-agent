"""email_channel

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-26 12:46:16.663039
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0017'
down_revision = '0016'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('email_mailboxes',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('address', sa.String(length=320), nullable=False),
    sa.Column('display_name', sa.String(length=100), server_default='', nullable=False),
    sa.Column('imap_host', sa.String(length=253), nullable=False),
    sa.Column('imap_port', sa.Integer(), server_default=sa.text('993'), nullable=False),
    sa.Column('imap_security', sa.String(length=16), server_default='ssl', nullable=False),
    sa.Column('imap_username', sa.String(length=320), nullable=False),
    sa.Column('imap_password_encrypted', sa.Text(), nullable=False),
    sa.Column('imap_folder', sa.String(length=200), server_default='INBOX', nullable=False),
    sa.Column('smtp_host', sa.String(length=253), nullable=False),
    sa.Column('smtp_port', sa.Integer(), server_default=sa.text('587'), nullable=False),
    sa.Column('smtp_security', sa.String(length=16), server_default='starttls', nullable=False),
    sa.Column('smtp_username', sa.String(length=320), nullable=False),
    sa.Column('smtp_password_encrypted', sa.Text(), nullable=False),
    sa.Column('enabled', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('status', sa.String(length=16), server_default='active', nullable=False),
    sa.Column('last_error_type', sa.String(length=64), nullable=True),
    sa.Column('last_error_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_sync_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('connected_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('uidvalidity', sa.BigInteger(), nullable=True),
    sa.Column('last_uid', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
    sa.Column('sync_lease_until', sa.DateTime(timezone=True), nullable=True),
    sa.Column('organisation_id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text("timezone('utc', now())"), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text("timezone('utc', now())"), nullable=False),
    sa.CheckConstraint("imap_security IN ('ssl', 'starttls', 'none')", name='ck_email_mailboxes_imap_security'),
    sa.CheckConstraint("smtp_security IN ('ssl', 'starttls', 'none')", name='ck_email_mailboxes_smtp_security'),
    sa.CheckConstraint("status IN ('active', 'auth_failed', 'error')", name='ck_email_mailboxes_status'),
    sa.ForeignKeyConstraint(['organisation_id'], ['organisations.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('organisation_id', name='uq_email_mailboxes_organisation_id')
    )
    op.create_index(op.f('ix_email_mailboxes_organisation_id'), 'email_mailboxes', ['organisation_id'], unique=False)
    op.create_table('email_messages',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('message_id', sa.UUID(), nullable=False),
    sa.Column('conversation_id', sa.UUID(), nullable=False),
    sa.Column('mailbox_id', sa.UUID(), nullable=True),
    sa.Column('direction', sa.String(length=8), nullable=False),
    sa.Column('rfc_message_id', sa.String(length=998), nullable=False),
    sa.Column('in_reply_to', sa.String(length=998), nullable=True),
    sa.Column('references', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('subject', sa.String(length=998), nullable=True),
    sa.Column('from_address', sa.String(length=320), nullable=False),
    sa.Column('from_name', sa.String(length=255), nullable=True),
    sa.Column('to_addresses', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('cc_addresses', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('reply_to', sa.String(length=320), nullable=True),
    sa.Column('html', sa.Text(), nullable=True),
    sa.Column('has_remote_images', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('skipped_attachments', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('imap_uid', sa.BigInteger(), nullable=True),
    sa.Column('delivery_status', sa.String(length=16), nullable=False),
    sa.Column('attempts', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('next_attempt_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('sending_until', sa.DateTime(timezone=True), nullable=True),
    sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('error_type', sa.String(length=64), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('organisation_id', sa.UUID(), nullable=False),
    sa.CheckConstraint("delivery_status IN ('received', 'queued', 'sending', 'sent', 'retry', 'failed')", name='ck_email_messages_delivery_status'),
    sa.CheckConstraint("direction IN ('inbound', 'outbound')", name='ck_email_messages_direction'),
    sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['mailbox_id'], ['email_mailboxes.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['message_id'], ['messages.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['organisation_id'], ['organisations.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('message_id', name='uq_email_messages_message_id')
    )
    op.create_index('ix_email_messages_conversation_id_created_at', 'email_messages', ['conversation_id', 'created_at'], unique=False)
    op.create_index('ix_email_messages_mailbox_id_from_address_created_at', 'email_messages', ['mailbox_id', 'from_address', 'created_at'], unique=False)
    op.create_index(op.f('ix_email_messages_organisation_id'), 'email_messages', ['organisation_id'], unique=False)
    op.create_index('uq_email_messages_organisation_id_rfc_message_id', 'email_messages', ['organisation_id', 'rfc_message_id'], unique=True)
    op.create_table('email_attachments',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('email_message_id', sa.UUID(), nullable=False),
    sa.Column('filename', sa.String(length=255), nullable=False),
    sa.Column('content_type', sa.String(length=255), nullable=False),
    sa.Column('size_bytes', sa.BigInteger(), nullable=False),
    sa.Column('sha256', sa.String(length=64), nullable=False),
    sa.Column('storage_key', sa.String(length=512), nullable=False),
    sa.Column('content_id', sa.String(length=998), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('organisation_id', sa.UUID(), nullable=False),
    sa.ForeignKeyConstraint(['email_message_id'], ['email_messages.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['organisation_id'], ['organisations.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_email_attachments_email_message_id', 'email_attachments', ['email_message_id'], unique=False)
    op.create_index(op.f('ix_email_attachments_organisation_id'), 'email_attachments', ['organisation_id'], unique=False)
    op.add_column('conversations', sa.Column('subject', sa.String(length=998), nullable=True))
    op.create_index('ix_customers_organisation_id_lower_email', 'customers', ['organisation_id', sa.literal_column('lower(email)')], unique=False)
    # Existing rows can only hold 'chat' (the sole channel before this phase); normalise anything else so the
    # constraint can never fail the upgrade.
    op.execute("UPDATE conversations SET channel = 'chat' WHERE channel NOT IN ('chat', 'email', 'voice')")
    op.create_check_constraint('ck_conversations_channel', 'conversations', "channel IN ('chat', 'email', 'voice')")


def downgrade() -> None:
    op.drop_constraint('ck_conversations_channel', 'conversations', type_='check')
    op.drop_index('ix_customers_organisation_id_lower_email', table_name='customers')
    op.drop_column('conversations', 'subject')
    op.drop_index(op.f('ix_email_attachments_organisation_id'), table_name='email_attachments')
    op.drop_index('ix_email_attachments_email_message_id', table_name='email_attachments')
    op.drop_table('email_attachments')
    op.drop_index('uq_email_messages_organisation_id_rfc_message_id', table_name='email_messages')
    op.drop_index(op.f('ix_email_messages_organisation_id'), table_name='email_messages')
    op.drop_index('ix_email_messages_mailbox_id_from_address_created_at', table_name='email_messages')
    op.drop_index('ix_email_messages_conversation_id_created_at', table_name='email_messages')
    op.drop_table('email_messages')
    op.drop_index(op.f('ix_email_mailboxes_organisation_id'), table_name='email_mailboxes')
    op.drop_table('email_mailboxes')
