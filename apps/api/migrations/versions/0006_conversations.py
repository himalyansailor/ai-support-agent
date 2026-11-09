"""conversations: customers, conversations, messages and bot turns

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-25 10:43:03.361160

Hand-checked against supportcore.db.models.conversation: all four tables are tenant tables
(organisation_id FK CASCADE + index); customers and messages have partial unique indexes
(external_id / visitor_id per organisation, client_id per conversation).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('customers',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('external_id', sa.String(length=255), nullable=True),
    sa.Column('visitor_id', sa.String(length=64), nullable=True),
    sa.Column('email', sa.String(length=320), nullable=True),
    sa.Column('name', sa.String(length=255), nullable=True),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('organisation_id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text("timezone('utc', now())"), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text("timezone('utc', now())"), nullable=False),
    sa.ForeignKeyConstraint(['organisation_id'], ['organisations.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_customers_organisation_id'), 'customers', ['organisation_id'], unique=False)
    op.create_index('uq_customers_organisation_id_external_id', 'customers', ['organisation_id', 'external_id'], unique=True, postgresql_where=sa.text('external_id IS NOT NULL'))
    op.create_index('uq_customers_organisation_id_visitor_id', 'customers', ['organisation_id', 'visitor_id'], unique=True, postgresql_where=sa.text('visitor_id IS NOT NULL'))
    op.create_table('conversations',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('widget_id', sa.UUID(), nullable=True),
    sa.Column('customer_id', sa.UUID(), nullable=False),
    sa.Column('channel', sa.String(length=16), server_default='chat', nullable=False),
    sa.Column('status', sa.String(length=16), server_default='bot', nullable=False),
    sa.Column('language', sa.String(length=16), server_default='en', nullable=False),
    sa.Column('is_test', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('escalation', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('contact_email', sa.String(length=320), nullable=True),
    sa.Column('csat_score', sa.Integer(), nullable=True),
    sa.Column('csat_comment', sa.String(length=1000), nullable=True),
    sa.Column('turn_lease_until', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_message_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('organisation_id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text("timezone('utc', now())"), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text("timezone('utc', now())"), nullable=False),
    sa.CheckConstraint("status IN ('bot', 'pending_human', 'resolved', 'closed')", name='ck_conversations_status'),
    sa.CheckConstraint('csat_score IS NULL OR csat_score BETWEEN 1 AND 5', name='ck_conversations_csat_score'),
    sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['organisation_id'], ['organisations.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['widget_id'], ['widgets.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_conversations_customer_id', 'conversations', ['customer_id'], unique=False)
    op.create_index(op.f('ix_conversations_organisation_id'), 'conversations', ['organisation_id'], unique=False)
    op.create_index('ix_conversations_organisation_id_status_last_message_at', 'conversations', ['organisation_id', 'status', 'last_message_at'], unique=False)
    op.create_index('ix_conversations_widget_id', 'conversations', ['widget_id'], unique=False)
    op.create_table('messages',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('conversation_id', sa.UUID(), nullable=False),
    sa.Column('role', sa.String(length=16), nullable=False),
    sa.Column('text', sa.Text(), nullable=False),
    sa.Column('citations', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('client_id', sa.String(length=64), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('organisation_id', sa.UUID(), nullable=False),
    sa.CheckConstraint("role IN ('customer', 'bot', 'agent', 'system')", name='ck_messages_role'),
    sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['organisation_id'], ['organisations.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_messages_conversation_id_created_at', 'messages', ['conversation_id', 'created_at'], unique=False)
    op.create_index(op.f('ix_messages_organisation_id'), 'messages', ['organisation_id'], unique=False)
    op.create_index('uq_messages_conversation_id_client_id', 'messages', ['conversation_id', 'client_id'], unique=True, postgresql_where=sa.text('client_id IS NOT NULL'))
    op.create_table('bot_turns',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('conversation_id', sa.UUID(), nullable=False),
    sa.Column('message_id', sa.UUID(), nullable=True),
    sa.Column('reply_message_id', sa.UUID(), nullable=True),
    sa.Column('outcome', sa.String(length=16), nullable=False),
    sa.Column('no_answer', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('risk', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('retrieval', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('tool_calls', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('model', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('latency_ms', sa.Integer(), nullable=True),
    sa.Column('trace_id', sa.String(length=32), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('organisation_id', sa.UUID(), nullable=False),
    sa.CheckConstraint("outcome IN ('answered', 'offered_human', 'escalated', 'error')", name='ck_bot_turns_outcome'),
    sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['message_id'], ['messages.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['organisation_id'], ['organisations.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['reply_message_id'], ['messages.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_bot_turns_conversation_id_created_at', 'bot_turns', ['conversation_id', 'created_at'], unique=False)
    op.create_index(op.f('ix_bot_turns_organisation_id'), 'bot_turns', ['organisation_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_bot_turns_organisation_id'), table_name='bot_turns')
    op.drop_index('ix_bot_turns_conversation_id_created_at', table_name='bot_turns')
    op.drop_table('bot_turns')
    op.drop_index('uq_messages_conversation_id_client_id', table_name='messages', postgresql_where=sa.text('client_id IS NOT NULL'))
    op.drop_index(op.f('ix_messages_organisation_id'), table_name='messages')
    op.drop_index('ix_messages_conversation_id_created_at', table_name='messages')
    op.drop_table('messages')
    op.drop_index('ix_conversations_widget_id', table_name='conversations')
    op.drop_index('ix_conversations_organisation_id_status_last_message_at', table_name='conversations')
    op.drop_index(op.f('ix_conversations_organisation_id'), table_name='conversations')
    op.drop_index('ix_conversations_customer_id', table_name='conversations')
    op.drop_table('conversations')
    op.drop_index('uq_customers_organisation_id_visitor_id', table_name='customers', postgresql_where=sa.text('visitor_id IS NOT NULL'))
    op.drop_index('uq_customers_organisation_id_external_id', table_name='customers', postgresql_where=sa.text('external_id IS NOT NULL'))
    op.drop_index(op.f('ix_customers_organisation_id'), table_name='customers')
    op.drop_table('customers')
