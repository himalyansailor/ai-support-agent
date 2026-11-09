"""billing

Plans, billing accounts, the usage ledger and the provider webhook log (Phase 8), and `plan_limit` as a call end
reason. Every existing organisation gets a billing account with no plan.

Revision ID: 0021
Revises: 0020
Create Date: 2026-10-28 12:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0021'
down_revision = '0020'
branch_labels = None
depends_on = None

_END_REASONS_7B = "'caller_hangup', 'handoff_callback', 'max_duration', 'silence', 'voice_unavailable', 'capacity', 'error', 'transferred', 'transfer_no_answer', 'callback'"
_END_REASONS_8 = _END_REASONS_7B + ", 'plan_limit'"
_NOW = sa.text("timezone('utc', now())")


def upgrade() -> None:
    op.create_table('plans',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('key', sa.String(length=50), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('description', sa.Text(), server_default='', nullable=False),
    sa.Column('active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('sort_order', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('monthly_price_minor', sa.Integer(), nullable=False),
    sa.Column('trial_days', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('seats', sa.Integer(), nullable=True),
    sa.Column('messages', sa.Integer(), nullable=True),
    sa.Column('voice_minutes', sa.Integer(), nullable=True),
    sa.Column('storage_mb', sa.Integer(), nullable=True),
    sa.Column('knowledge_bases', sa.Integer(), nullable=True),
    sa.Column('phone_lines', sa.Integer(), nullable=True),
    sa.Column('messages_overage_micros', sa.BigInteger(), nullable=True),
    sa.Column('voice_minutes_overage_micros', sa.BigInteger(), nullable=True),
    sa.Column('provider', sa.String(length=32), nullable=True),
    sa.Column('provider_product_id', sa.String(length=255), nullable=True),
    sa.Column('sync_status', sa.String(length=16), server_default='pending', nullable=False),
    sa.Column('sync_error', sa.String(length=64), nullable=True),
    sa.Column('synced_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
    sa.CheckConstraint('monthly_price_minor >= 0', name='ck_plans_monthly_price_minor'),
    sa.CheckConstraint('trial_days BETWEEN 0 AND 90', name='ck_plans_trial_days'),
    sa.CheckConstraint("sync_status IN ('pending', 'synced', 'failed')", name='ck_plans_sync_status'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('key')
    )

    op.create_table('plan_provider_prices',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('plan_id', sa.UUID(), nullable=False),
    sa.Column('provider', sa.String(length=32), nullable=False),
    sa.Column('price_id', sa.String(length=255), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('amount_minor', sa.Integer(), nullable=True),
    sa.Column('included_units', sa.Integer(), nullable=True),
    sa.Column('overage_micros', sa.BigInteger(), nullable=True),
    sa.Column('active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
    sa.CheckConstraint("kind IN ('base', 'messages', 'voice_minutes')", name='ck_plan_provider_prices_kind'),
    sa.ForeignKeyConstraint(['plan_id'], ['plans.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('provider', 'price_id', name='uq_plan_provider_prices_provider_price_id')
    )
    op.create_index('ix_plan_provider_prices_plan_id', 'plan_provider_prices', ['plan_id'], unique=False)

    op.create_table('billing_accounts',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('plan_id', sa.UUID(), nullable=True),
    sa.Column('status', sa.String(length=16), server_default='no_plan', nullable=False),
    sa.Column('limit_behaviour', sa.String(length=16), server_default='hard_stop', nullable=False),
    sa.Column('trial_started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('trial_ends_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('provider', sa.String(length=32), nullable=True),
    sa.Column('provider_customer_id', sa.String(length=255), nullable=True),
    sa.Column('provider_subscription_id', sa.String(length=255), nullable=True),
    sa.Column('subscription_price_ids', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('current_period_start', sa.DateTime(timezone=True), nullable=True),
    sa.Column('current_period_end', sa.DateTime(timezone=True), nullable=True),
    sa.Column('cancel_at_period_end', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('last_event_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('organisation_id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
    sa.CheckConstraint("limit_behaviour IN ('hard_stop', 'overage')", name='ck_billing_accounts_limit_behaviour'),
    sa.CheckConstraint("status IN ('no_plan', 'trialing', 'active', 'past_due', 'canceled', 'incomplete')", name='ck_billing_accounts_status'),
    sa.ForeignKeyConstraint(['organisation_id'], ['organisations.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['plan_id'], ['plans.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('organisation_id', name='uq_billing_accounts_organisation_id')
    )
    op.create_index(op.f('ix_billing_accounts_organisation_id'), 'billing_accounts', ['organisation_id'], unique=False)
    op.create_index(op.f('ix_billing_accounts_plan_id'), 'billing_accounts', ['plan_id'], unique=False)
    op.create_index(op.f('ix_billing_accounts_provider_customer_id'), 'billing_accounts', ['provider_customer_id'], unique=False)
    op.create_index(op.f('ix_billing_accounts_provider_subscription_id'), 'billing_accounts', ['provider_subscription_id'], unique=False)

    op.create_table('usage_reports',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('meter', sa.String(length=16), nullable=False),
    sa.Column('quantity', sa.BigInteger(), nullable=False),
    sa.Column('provider_customer_id', sa.String(length=255), nullable=False),
    sa.Column('timestamp', sa.DateTime(timezone=True), nullable=False),
    sa.Column('status', sa.String(length=16), server_default='pending', nullable=False),
    sa.Column('attempts', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
    sa.Column('organisation_id', sa.UUID(), nullable=False),
    sa.CheckConstraint("meter IN ('messages', 'voice_minutes')", name='ck_usage_reports_meter'),
    sa.CheckConstraint('quantity > 0', name='ck_usage_reports_quantity'),
    sa.CheckConstraint("status IN ('pending', 'sent', 'failed')", name='ck_usage_reports_status'),
    sa.ForeignKeyConstraint(['organisation_id'], ['organisations.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_usage_reports_organisation_id_status', 'usage_reports', ['organisation_id', 'status'], unique=False)
    op.create_index(op.f('ix_usage_reports_organisation_id'), 'usage_reports', ['organisation_id'], unique=False)

    op.create_table('usage_events',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('meter', sa.String(length=16), nullable=False),
    sa.Column('quantity', sa.Integer(), nullable=False),
    sa.Column('occurred_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('source_id', sa.UUID(), nullable=False),
    sa.Column('report_id', sa.UUID(), nullable=True),
    sa.Column('skipped', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('organisation_id', sa.UUID(), nullable=False),
    sa.CheckConstraint("meter IN ('messages', 'voice_minutes')", name='ck_usage_events_meter'),
    sa.CheckConstraint('quantity > 0', name='ck_usage_events_quantity'),
    sa.ForeignKeyConstraint(['organisation_id'], ['organisations.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['report_id'], ['usage_reports.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('organisation_id', 'meter', 'source_id', name='uq_usage_events_organisation_id_meter_source_id')
    )
    op.create_index('ix_usage_events_organisation_id_meter_occurred_at', 'usage_events', ['organisation_id', 'meter', 'occurred_at'], unique=False)
    op.create_index('ix_usage_events_report_id', 'usage_events', ['report_id'], unique=False)
    op.create_index(op.f('ix_usage_events_organisation_id'), 'usage_events', ['organisation_id'], unique=False)

    op.create_table('usage_alerts',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('period_start', sa.DateTime(timezone=True), nullable=False),
    sa.Column('kind', sa.String(length=40), nullable=False),
    sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('organisation_id', sa.UUID(), nullable=False),
    sa.ForeignKeyConstraint(['organisation_id'], ['organisations.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('organisation_id', 'period_start', 'kind', name='uq_usage_alerts_organisation_id_period_start_kind')
    )
    op.create_index(op.f('ix_usage_alerts_organisation_id'), 'usage_alerts', ['organisation_id'], unique=False)

    op.create_table('billing_webhook_events',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('provider', sa.String(length=32), nullable=False),
    sa.Column('event_id', sa.String(length=255), nullable=False),
    sa.Column('type', sa.String(length=100), nullable=False),
    sa.Column('received_at', sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
    sa.Column('processed_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('provider', 'event_id', name='uq_billing_webhook_events_provider_event_id')
    )

    op.drop_constraint('ck_calls_end_reason', 'calls', type_='check')
    op.create_check_constraint('ck_calls_end_reason', 'calls', f"end_reason IS NULL OR end_reason IN ({_END_REASONS_8})")

    op.execute("INSERT INTO billing_accounts (id, organisation_id, status) SELECT gen_random_uuid(), id, 'no_plan' FROM organisations")


def downgrade() -> None:
    op.execute("UPDATE calls SET end_reason = 'error' WHERE end_reason = 'plan_limit'")
    op.drop_constraint('ck_calls_end_reason', 'calls', type_='check')
    op.create_check_constraint('ck_calls_end_reason', 'calls', f"end_reason IS NULL OR end_reason IN ({_END_REASONS_7B})")

    op.drop_table('billing_webhook_events')
    op.drop_index(op.f('ix_usage_alerts_organisation_id'), table_name='usage_alerts')
    op.drop_table('usage_alerts')
    op.drop_index(op.f('ix_usage_events_organisation_id'), table_name='usage_events')
    op.drop_index('ix_usage_events_report_id', table_name='usage_events')
    op.drop_index('ix_usage_events_organisation_id_meter_occurred_at', table_name='usage_events')
    op.drop_table('usage_events')
    op.drop_index(op.f('ix_usage_reports_organisation_id'), table_name='usage_reports')
    op.drop_index('ix_usage_reports_organisation_id_status', table_name='usage_reports')
    op.drop_table('usage_reports')
    op.drop_index(op.f('ix_billing_accounts_provider_subscription_id'), table_name='billing_accounts')
    op.drop_index(op.f('ix_billing_accounts_provider_customer_id'), table_name='billing_accounts')
    op.drop_index(op.f('ix_billing_accounts_plan_id'), table_name='billing_accounts')
    op.drop_index(op.f('ix_billing_accounts_organisation_id'), table_name='billing_accounts')
    op.drop_table('billing_accounts')
    op.drop_index('ix_plan_provider_prices_plan_id', table_name='plan_provider_prices')
    op.drop_table('plan_provider_prices')
    op.drop_table('plans')
