"""voice_transfers

Speech-to-speech voice, live transfer to a person and callback calls (Phase 7b): Realtime and transfer settings,
the call transfer table, outbound calls beside the inbound one, and an agent's call preference.

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-28 10:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0020'
down_revision = '0019'
branch_labels = None
depends_on = None

_END_REASONS_7A = "'caller_hangup', 'handoff_callback', 'max_duration', 'silence', 'voice_unavailable', 'capacity', 'error'"
_END_REASONS_7B = _END_REASONS_7A + ", 'transferred', 'transfer_no_answer', 'callback'"


def upgrade() -> None:
    op.add_column('voice_settings', sa.Column('realtime_provider', sa.String(length=32), nullable=True))
    op.add_column('voice_settings', sa.Column('realtime_model', sa.String(length=64), nullable=True))
    op.add_column('voice_settings', sa.Column('realtime_voice', sa.String(length=32), nullable=True))
    op.add_column('voice_settings', sa.Column('realtime_credential_id', sa.UUID(), nullable=True))
    op.create_foreign_key('fk_voice_settings_realtime_credential_id', 'voice_settings', 'provider_credentials', ['realtime_credential_id'], ['id'], ondelete='SET NULL')
    op.add_column('voice_settings', sa.Column('transfer_browser_enabled', sa.Boolean(), server_default=sa.text('true'), nullable=False))
    op.add_column('voice_settings', sa.Column('transfer_phone_enabled', sa.Boolean(), server_default=sa.text('false'), nullable=False))

    op.add_column('memberships', sa.Column('call_preference', sa.String(length=16), server_default='browser', nullable=False))
    op.create_check_constraint('ck_memberships_call_preference', 'memberships', "call_preference IN ('browser', 'phone')")

    op.drop_constraint('ck_calls_direction', 'calls', type_='check')
    op.create_check_constraint('ck_calls_direction', 'calls', "direction IN ('inbound', 'outbound')")
    op.drop_constraint('ck_calls_source', 'calls', type_='check')
    op.create_check_constraint('ck_calls_source', 'calls', "source IN ('phone', 'browser', 'callback')")
    op.drop_constraint('ck_calls_end_reason', 'calls', type_='check')
    op.create_check_constraint('ck_calls_end_reason', 'calls', f"end_reason IS NULL OR end_reason IN ({_END_REASONS_7B})")
    op.add_column('calls', sa.Column('mode', sa.String(length=24), nullable=True))
    op.create_check_constraint('ck_calls_mode', 'calls', "mode IS NULL OR mode IN ('chained', 'speech_to_speech')")
    op.add_column('calls', sa.Column('agent_membership_id', sa.UUID(), nullable=True))
    op.create_foreign_key('fk_calls_agent_membership_id', 'calls', 'memberships', ['agent_membership_id'], ['id'], ondelete='SET NULL')
    op.alter_column('calls', 'provider_call_sid', existing_type=sa.String(length=34), nullable=True)
    op.drop_constraint('uq_calls_conversation_id', 'calls', type_='unique')
    op.create_index('uq_calls_inbound_conversation_id', 'calls', ['conversation_id'], unique=True, postgresql_where=sa.text("direction = 'inbound'"))
    op.create_index('ix_calls_conversation_id', 'calls', ['conversation_id'], unique=False)

    op.create_table('call_transfers',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('call_id', sa.UUID(), nullable=False),
    sa.Column('conversation_id', sa.UUID(), nullable=False),
    sa.Column('status', sa.String(length=16), server_default='ringing', nullable=False),
    sa.Column('attempt', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('current_membership_id', sa.UUID(), nullable=True),
    sa.Column('current_target', sa.String(length=16), nullable=True),
    sa.Column('current_leg_sid', sa.String(length=34), nullable=True),
    sa.Column('tried_membership_ids', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('connected_membership_id', sa.UUID(), nullable=True),
    sa.Column('deadline_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('connected_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('agent_joined_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('ended_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('organisation_id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text("timezone('utc', now())"), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text("timezone('utc', now())"), nullable=False),
    sa.CheckConstraint("current_target IS NULL OR current_target IN ('browser', 'phone')", name='ck_call_transfers_current_target'),
    sa.CheckConstraint("status IN ('ringing', 'connected', 'no_answer', 'abandoned', 'failed')", name='ck_call_transfers_status'),
    sa.ForeignKeyConstraint(['call_id'], ['calls.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['connected_membership_id'], ['memberships.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['current_membership_id'], ['memberships.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['organisation_id'], ['organisations.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('call_id', name='uq_call_transfers_call_id')
    )
    op.create_index('ix_call_transfers_organisation_id_status', 'call_transfers', ['organisation_id', 'status'], unique=False)
    op.create_index(op.f('ix_call_transfers_organisation_id'), 'call_transfers', ['organisation_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_call_transfers_organisation_id'), table_name='call_transfers')
    op.drop_index('ix_call_transfers_organisation_id_status', table_name='call_transfers')
    op.drop_table('call_transfers')

    # Callback calls (and the rows that only 7b can hold) cannot exist under the 7a checks.
    op.execute("DELETE FROM calls WHERE direction = 'outbound' OR source = 'callback' OR provider_call_sid IS NULL")
    op.execute("UPDATE calls SET end_reason = 'handoff_callback' WHERE end_reason IN ('transferred', 'transfer_no_answer', 'callback')")
    op.drop_index('ix_calls_conversation_id', table_name='calls')
    op.drop_index('uq_calls_inbound_conversation_id', table_name='calls', postgresql_where=sa.text("direction = 'inbound'"))
    op.create_unique_constraint('uq_calls_conversation_id', 'calls', ['conversation_id'])
    op.alter_column('calls', 'provider_call_sid', existing_type=sa.String(length=34), nullable=False)
    op.drop_constraint('fk_calls_agent_membership_id', 'calls', type_='foreignkey')
    op.drop_column('calls', 'agent_membership_id')
    op.drop_constraint('ck_calls_mode', 'calls', type_='check')
    op.drop_column('calls', 'mode')
    op.drop_constraint('ck_calls_end_reason', 'calls', type_='check')
    op.create_check_constraint('ck_calls_end_reason', 'calls', f"end_reason IS NULL OR end_reason IN ({_END_REASONS_7A})")
    op.drop_constraint('ck_calls_source', 'calls', type_='check')
    op.create_check_constraint('ck_calls_source', 'calls', "source IN ('phone', 'browser')")
    op.drop_constraint('ck_calls_direction', 'calls', type_='check')
    op.create_check_constraint('ck_calls_direction', 'calls', "direction IN ('inbound')")

    op.drop_constraint('ck_memberships_call_preference', 'memberships', type_='check')
    op.drop_column('memberships', 'call_preference')

    op.drop_column('voice_settings', 'transfer_phone_enabled')
    op.drop_column('voice_settings', 'transfer_browser_enabled')
    op.drop_constraint('fk_voice_settings_realtime_credential_id', 'voice_settings', type_='foreignkey')
    op.drop_column('voice_settings', 'realtime_credential_id')
    op.drop_column('voice_settings', 'realtime_voice')
    op.drop_column('voice_settings', 'realtime_model')
    op.drop_column('voice_settings', 'realtime_provider')
