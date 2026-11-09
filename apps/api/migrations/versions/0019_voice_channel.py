"""voice_channel

The voice channel (Phase 7a): the organisation's Twilio account, its phone numbers, voice settings, calls and
call recordings; a phone number on customers; the callback request on conversations.

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-27 15:30:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = '0019'
down_revision = '0018'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('telephony_accounts',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('account_sid', sa.String(length=34), nullable=False),
    sa.Column('auth_token_encrypted', sa.Text(), nullable=False),
    sa.Column('auth_token_last4', sa.String(length=4), nullable=False),
    sa.Column('api_key_sid', sa.String(length=34), nullable=True),
    sa.Column('api_key_secret_encrypted', sa.Text(), nullable=True),
    sa.Column('twiml_app_sid', sa.String(length=34), nullable=True),
    sa.Column('status', sa.String(length=16), server_default='active', nullable=False),
    sa.Column('last_error_type', sa.String(length=64), nullable=True),
    sa.Column('last_error_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('organisation_id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text("timezone('utc', now())"), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text("timezone('utc', now())"), nullable=False),
    sa.CheckConstraint("kind IN ('platform', 'byo')", name='ck_telephony_accounts_kind'),
    sa.CheckConstraint("status IN ('active', 'auth_failed', 'error')", name='ck_telephony_accounts_status'),
    sa.ForeignKeyConstraint(['organisation_id'], ['organisations.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('account_sid'),
    sa.UniqueConstraint('organisation_id', name='uq_telephony_accounts_organisation_id')
    )
    op.create_index(op.f('ix_telephony_accounts_organisation_id'), 'telephony_accounts', ['organisation_id'], unique=False)
    op.create_table('phone_numbers',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('telephony_account_id', sa.UUID(), nullable=False),
    sa.Column('e164', sa.String(length=16), nullable=False),
    sa.Column('provider_sid', sa.String(length=34), nullable=False),
    sa.Column('ownership', sa.String(length=16), nullable=False),
    sa.Column('label', sa.String(length=100), nullable=True),
    sa.Column('widget_id', sa.UUID(), nullable=True),
    sa.Column('enabled', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('webhook_url', sa.String(length=2048), nullable=True),
    sa.Column('organisation_id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text("timezone('utc', now())"), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text("timezone('utc', now())"), nullable=False),
    sa.CheckConstraint("ownership IN ('platform', 'byo')", name='ck_phone_numbers_ownership'),
    sa.ForeignKeyConstraint(['organisation_id'], ['organisations.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['telephony_account_id'], ['telephony_accounts.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['widget_id'], ['widgets.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('e164', name='uq_phone_numbers_e164')
    )
    op.create_index(op.f('ix_phone_numbers_organisation_id'), 'phone_numbers', ['organisation_id'], unique=False)
    op.create_table('voice_settings',
    sa.Column('organisation_id', sa.UUID(), nullable=False),
    sa.Column('phone_enabled', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('browser_calls_enabled', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('mode', sa.String(length=24), server_default='chained', nullable=False),
    sa.Column('stt_provider', sa.String(length=32), nullable=True),
    sa.Column('stt_model', sa.String(length=255), nullable=True),
    sa.Column('stt_credential_id', sa.UUID(), nullable=True),
    sa.Column('tts_provider', sa.String(length=32), nullable=True),
    sa.Column('tts_model', sa.String(length=255), nullable=True),
    sa.Column('tts_voice', sa.String(length=64), nullable=True),
    sa.Column('tts_credential_id', sa.UUID(), nullable=True),
    sa.Column('record_calls', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('recording_retention_days', sa.Integer(), server_default=sa.text('90'), nullable=False),
    sa.Column('max_call_minutes', sa.Integer(), server_default=sa.text('30'), nullable=False),
    sa.Column('browser_widget_id', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text("timezone('utc', now())"), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text("timezone('utc', now())"), nullable=False),
    sa.CheckConstraint("max_call_minutes BETWEEN 1 AND 120", name='ck_voice_settings_max_call_minutes'),
    sa.CheckConstraint("mode IN ('chained', 'speech_to_speech')", name='ck_voice_settings_mode'),
    sa.CheckConstraint("recording_retention_days BETWEEN 1 AND 3650", name='ck_voice_settings_recording_retention_days'),
    sa.ForeignKeyConstraint(['browser_widget_id'], ['widgets.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['organisation_id'], ['organisations.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['stt_credential_id'], ['provider_credentials.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['tts_credential_id'], ['provider_credentials.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('organisation_id')
    )
    op.create_table('calls',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('conversation_id', sa.UUID(), nullable=False),
    sa.Column('phone_number_id', sa.UUID(), nullable=True),
    sa.Column('source', sa.String(length=16), nullable=False),
    sa.Column('direction', sa.String(length=16), server_default='inbound', nullable=False),
    sa.Column('provider_call_sid', sa.String(length=34), nullable=False),
    sa.Column('caller_e164', sa.String(length=16), nullable=True),
    sa.Column('ticket_jti', sa.String(length=64), nullable=True),
    sa.Column('status', sa.String(length=16), server_default='ringing', nullable=False),
    sa.Column('end_reason', sa.String(length=24), nullable=True),
    sa.Column('stream_connected_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('stream_ended_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('provider_ended_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('started_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('answered_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('ended_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('duration_seconds', sa.Integer(), nullable=True),
    sa.Column('stt_seconds', sa.Float(), server_default=sa.text('0'), nullable=False),
    sa.Column('tts_characters', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('recording_sid', sa.String(length=34), nullable=True),
    sa.Column('finalised', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('organisation_id', sa.UUID(), nullable=False),
    sa.CheckConstraint("direction IN ('inbound')", name='ck_calls_direction'),
    sa.CheckConstraint("end_reason IS NULL OR end_reason IN ('caller_hangup', 'handoff_callback', 'max_duration', 'silence', 'voice_unavailable', 'capacity', 'error')", name='ck_calls_end_reason'),
    sa.CheckConstraint("source IN ('phone', 'browser')", name='ck_calls_source'),
    sa.CheckConstraint("status IN ('ringing', 'in_progress', 'completed', 'failed', 'busy', 'no_answer', 'canceled')", name='ck_calls_status'),
    sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['organisation_id'], ['organisations.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['phone_number_id'], ['phone_numbers.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('conversation_id', name='uq_calls_conversation_id'),
    sa.UniqueConstraint('provider_call_sid', name='uq_calls_provider_call_sid'),
    sa.UniqueConstraint('ticket_jti', name='uq_calls_ticket_jti')
    )
    op.create_index(op.f('ix_calls_organisation_id'), 'calls', ['organisation_id'], unique=False)
    op.create_table('call_recordings',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('call_id', sa.UUID(), nullable=False),
    sa.Column('storage_key', sa.String(length=512), nullable=False),
    sa.Column('content_type', sa.String(length=64), server_default='audio/mpeg', nullable=False),
    sa.Column('size_bytes', sa.Integer(), nullable=False),
    sa.Column('duration_seconds', sa.Integer(), nullable=False),
    sa.Column('delete_after', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('organisation_id', sa.UUID(), nullable=False),
    sa.ForeignKeyConstraint(['call_id'], ['calls.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['organisation_id'], ['organisations.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('call_id', name='uq_call_recordings_call_id')
    )
    op.create_index('ix_call_recordings_delete_after', 'call_recordings', ['delete_after'], unique=False)
    op.create_index(op.f('ix_call_recordings_organisation_id'), 'call_recordings', ['organisation_id'], unique=False)

    op.add_column('customers', sa.Column('phone', sa.String(length=16), nullable=True))
    op.create_index('ix_customers_organisation_id_phone', 'customers', ['organisation_id', 'phone'], unique=False)
    op.add_column('conversations', sa.Column('callback_status', sa.String(length=16), nullable=True))
    op.add_column('conversations', sa.Column('callback_phone', sa.String(length=16), nullable=True))
    op.create_check_constraint(
        'ck_conversations_callback_status',
        'conversations',
        "callback_status IS NULL OR callback_status IN ('requested', 'done')",
    )
    op.create_index(
        'ix_conversations_organisation_id_callback_status',
        'conversations',
        ['organisation_id', 'callback_status'],
        unique=False,
        postgresql_where=sa.text("callback_status = 'requested'"),
    )


def downgrade() -> None:
    op.drop_index('ix_conversations_organisation_id_callback_status', table_name='conversations', postgresql_where=sa.text("callback_status = 'requested'"))
    op.drop_constraint('ck_conversations_callback_status', 'conversations', type_='check')
    op.drop_column('conversations', 'callback_phone')
    op.drop_column('conversations', 'callback_status')
    op.drop_index('ix_customers_organisation_id_phone', table_name='customers')
    op.drop_column('customers', 'phone')
    op.drop_index(op.f('ix_call_recordings_organisation_id'), table_name='call_recordings')
    op.drop_index('ix_call_recordings_delete_after', table_name='call_recordings')
    op.drop_table('call_recordings')
    op.drop_index(op.f('ix_calls_organisation_id'), table_name='calls')
    op.drop_table('calls')
    op.drop_table('voice_settings')
    op.drop_index(op.f('ix_phone_numbers_organisation_id'), table_name='phone_numbers')
    op.drop_table('phone_numbers')
    op.drop_index(op.f('ix_telephony_accounts_organisation_id'), table_name='telephony_accounts')
    op.drop_table('telephony_accounts')
