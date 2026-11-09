"""analytics

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-25 19:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = '0016'
down_revision = '0015'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index('ix_bot_turns_organisation_id_created_at_id', 'bot_turns', ['organisation_id', 'created_at', 'id'], unique=False)
    op.create_table('conversation_events',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('conversation_id', sa.UUID(), nullable=False),
    sa.Column('kind', sa.String(length=32), nullable=False),
    sa.Column('occurred_at', sa.DateTime(timezone=True), server_default=sa.text('clock_timestamp()'), nullable=False),
    sa.Column('channel', sa.String(length=20), nullable=False),
    sa.Column('membership_id', sa.UUID(), nullable=True),
    sa.Column('data', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('organisation_id', sa.UUID(), nullable=False),
    sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['organisation_id'], ['organisations.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_conversation_events_conversation_id_kind_occurred_at', 'conversation_events', ['conversation_id', 'kind', 'occurred_at'], unique=False)
    op.create_index('ix_conversation_events_organisation_id_occurred_at', 'conversation_events', ['organisation_id', 'occurred_at'], unique=False)
    op.create_index(op.f('ix_conversation_events_organisation_id'), 'conversation_events', ['organisation_id'], unique=False)


    op.create_table('model_prices',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('provider', sa.String(length=64), nullable=False),
    sa.Column('model', sa.String(length=200), nullable=False),
    sa.Column('input_per_million_micros', sa.BigInteger(), nullable=False),
    sa.Column('output_per_million_micros', sa.BigInteger(), nullable=False),
    sa.Column('currency', sa.String(length=3), server_default='USD', nullable=False),
    sa.Column('updated_by', sa.UUID(), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['updated_by'], ['users.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('provider', 'model', name='uq_model_prices_provider_model')
    )

    op.create_table('integration_events',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('provider', sa.String(length=200), nullable=False),
    sa.Column('error_type', sa.String(length=64), nullable=False),
    sa.Column('hour', sa.DateTime(timezone=True), nullable=False),
    sa.Column('count', sa.Integer(), server_default='1', nullable=False),
    sa.Column('first_seen_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('organisation_id', sa.UUID(), nullable=False),
    sa.ForeignKeyConstraint(['organisation_id'], ['organisations.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('organisation_id', 'kind', 'provider', 'error_type', 'hour', name='uq_integration_events_bucket')
    )
    op.create_index('ix_integration_events_organisation_id_last_seen_at', 'integration_events', ['organisation_id', 'last_seen_at'], unique=False)
    op.create_index(op.f('ix_integration_events_organisation_id'), 'integration_events', ['organisation_id'], unique=False)

    op.create_table('agent_hourly_stats',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('hour', sa.DateTime(timezone=True), nullable=False),
    sa.Column('membership_id', sa.UUID(), nullable=False),
    sa.Column('assigned', sa.Integer(), nullable=False),
    sa.Column('handled', sa.Integer(), nullable=False),
    sa.Column('handle_seconds_sum', sa.BigInteger(), nullable=False),
    sa.Column('first_responses', sa.Integer(), nullable=False),
    sa.Column('first_response_seconds_sum', sa.BigInteger(), nullable=False),
    sa.Column('csat_count', sa.Integer(), nullable=False),
    sa.Column('csat_sum', sa.Integer(), nullable=False),
    sa.Column('organisation_id', sa.UUID(), nullable=False),
    sa.ForeignKeyConstraint(['organisation_id'], ['organisations.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('organisation_id', 'hour', 'membership_id', name='uq_agent_hourly_stats_bucket')
    )
    op.create_index(op.f('ix_agent_hourly_stats_organisation_id'), 'agent_hourly_stats', ['organisation_id'], unique=False)
    op.create_table('org_hourly_stats',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('hour', sa.DateTime(timezone=True), nullable=False),
    sa.Column('channel', sa.String(length=20), nullable=False),
    sa.Column('conversations_started', sa.Integer(), nullable=False),
    sa.Column('entered_team', sa.Integer(), nullable=False),
    sa.Column('resolved', sa.Integer(), nullable=False),
    sa.Column('ai_resolved', sa.Integer(), nullable=False),
    sa.Column('first_response_count', sa.Integer(), nullable=False),
    sa.Column('first_response_seconds_sum', sa.BigInteger(), nullable=False),
    sa.Column('resolution_count', sa.Integer(), nullable=False),
    sa.Column('resolution_seconds_sum', sa.BigInteger(), nullable=False),
    sa.Column('csat_count', sa.Integer(), nullable=False),
    sa.Column('csat_sum', sa.Integer(), nullable=False),
    sa.Column('csat_positive', sa.Integer(), nullable=False),
    sa.Column('sla_breaches', sa.Integer(), nullable=False),
    sa.Column('messages_customer', sa.Integer(), nullable=False),
    sa.Column('messages_bot', sa.Integer(), nullable=False),
    sa.Column('messages_agent', sa.Integer(), nullable=False),
    sa.Column('bot_turns', sa.Integer(), nullable=False),
    sa.Column('knowledge_gaps', sa.Integer(), nullable=False),
    sa.Column('input_tokens', sa.BigInteger(), nullable=False),
    sa.Column('output_tokens', sa.BigInteger(), nullable=False),
    sa.Column('cost_micros', sa.BigInteger(), nullable=False),
    sa.Column('cost_priced_turns', sa.Integer(), nullable=False),
    sa.Column('cost_unpriced_turns', sa.Integer(), nullable=False),
    sa.Column('organisation_id', sa.UUID(), nullable=False),
    sa.ForeignKeyConstraint(['organisation_id'], ['organisations.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('organisation_id', 'hour', 'channel', name='uq_org_hourly_stats_bucket')
    )
    op.create_index(op.f('ix_org_hourly_stats_organisation_id'), 'org_hourly_stats', ['organisation_id'], unique=False)
    op.create_table('reason_hourly_stats',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('hour', sa.DateTime(timezone=True), nullable=False),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('key', sa.String(length=200), nullable=False),
    sa.Column('label', sa.String(length=200), nullable=False),
    sa.Column('count', sa.Integer(), nullable=False),
    sa.Column('organisation_id', sa.UUID(), nullable=False),
    sa.ForeignKeyConstraint(['organisation_id'], ['organisations.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('organisation_id', 'hour', 'kind', 'key', name='uq_reason_hourly_stats_bucket')
    )
    op.create_index(op.f('ix_reason_hourly_stats_organisation_id'), 'reason_hourly_stats', ['organisation_id'], unique=False)
    op.create_table('rollup_dirty_hours',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('hour', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('organisation_id', sa.UUID(), nullable=False),
    sa.ForeignKeyConstraint(['organisation_id'], ['organisations.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_rollup_dirty_hours_organisation_id'), 'rollup_dirty_hours', ['organisation_id'], unique=False)
    op.create_index('ix_rollup_dirty_hours_organisation_id_hour', 'rollup_dirty_hours', ['organisation_id', 'hour'], unique=False)
    op.create_table('rollup_watermarks',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('rolled_through', sa.DateTime(timezone=True), nullable=False),
    sa.Column('organisation_id', sa.UUID(), nullable=False),
    sa.ForeignKeyConstraint(['organisation_id'], ['organisations.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('organisation_id', name='uq_rollup_watermarks_organisation_id')
    )
    op.create_index(op.f('ix_rollup_watermarks_organisation_id'), 'rollup_watermarks', ['organisation_id'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_bot_turns_organisation_id_created_at_id', table_name='bot_turns')
    op.drop_index(op.f('ix_rollup_watermarks_organisation_id'), table_name='rollup_watermarks')
    op.drop_table('rollup_watermarks')
    op.drop_index('ix_rollup_dirty_hours_organisation_id_hour', table_name='rollup_dirty_hours')
    op.drop_index(op.f('ix_rollup_dirty_hours_organisation_id'), table_name='rollup_dirty_hours')
    op.drop_table('rollup_dirty_hours')
    op.drop_index(op.f('ix_reason_hourly_stats_organisation_id'), table_name='reason_hourly_stats')
    op.drop_table('reason_hourly_stats')
    op.drop_index(op.f('ix_org_hourly_stats_organisation_id'), table_name='org_hourly_stats')
    op.drop_table('org_hourly_stats')
    op.drop_index(op.f('ix_agent_hourly_stats_organisation_id'), table_name='agent_hourly_stats')
    op.drop_table('agent_hourly_stats')
    op.drop_index(op.f('ix_integration_events_organisation_id'), table_name='integration_events')
    op.drop_index('ix_integration_events_organisation_id_last_seen_at', table_name='integration_events')
    op.drop_table('integration_events')
    op.drop_table('model_prices')
    op.drop_index(op.f('ix_conversation_events_organisation_id'), table_name='conversation_events')
    op.drop_index('ix_conversation_events_organisation_id_occurred_at', table_name='conversation_events')
    op.drop_index('ix_conversation_events_conversation_id_kind_occurred_at', table_name='conversation_events')
    op.drop_table('conversation_events')
