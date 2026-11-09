"""inbox search and tags

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-25 16:10:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0011'
down_revision = '0010'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.add_column('messages', sa.Column('search_vector', postgresql.TSVECTOR(), sa.Computed("to_tsvector('simple', text)", persisted=True), nullable=True))
    op.add_column('conversations', sa.Column('version', sa.Integer(), server_default=sa.text('1'), nullable=False))
    op.create_index('ix_messages_conversation_id_created_at_id_desc', 'messages', ['conversation_id', sa.text('created_at DESC'), sa.text('id DESC')], unique=False)
    op.create_index('ix_messages_search_vector', 'messages', ['search_vector'], unique=False, postgresql_using='gin')
    op.create_index('ix_customers_name_trgm', 'customers', [sa.text('lower(name)')], unique=False, postgresql_using='gin', postgresql_ops={'lower(name)': 'gin_trgm_ops'})
    op.create_index('ix_customers_email_trgm', 'customers', [sa.text('lower(email)')], unique=False, postgresql_using='gin', postgresql_ops={'lower(email)': 'gin_trgm_ops'})
    op.create_table('tags',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('name', sa.String(length=40), nullable=False),
    sa.Column('organisation_id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text("timezone('utc', now())"), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text("timezone('utc', now())"), nullable=False),
    sa.ForeignKeyConstraint(['organisation_id'], ['organisations.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_tags_organisation_id'), 'tags', ['organisation_id'], unique=False)
    op.create_index('uq_tags_organisation_id_lower_name', 'tags', ['organisation_id', sa.text('lower(name)')], unique=True)
    op.create_table('conversation_tags',
    sa.Column('conversation_id', sa.UUID(), nullable=False),
    sa.Column('tag_id', sa.UUID(), nullable=False),
    sa.Column('organisation_id', sa.UUID(), nullable=False),
    sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['organisation_id'], ['organisations.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tag_id'], ['tags.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('conversation_id', 'tag_id')
    )
    op.create_index('ix_conversation_tags_tag_id', 'conversation_tags', ['tag_id'], unique=False)
    op.create_index(op.f('ix_conversation_tags_organisation_id'), 'conversation_tags', ['organisation_id'], unique=False)


def downgrade() -> None:
    op.drop_table('conversation_tags')
    op.drop_table('tags')
    op.drop_index('ix_customers_email_trgm', table_name='customers', postgresql_using='gin')
    op.drop_index('ix_customers_name_trgm', table_name='customers', postgresql_using='gin')
    op.drop_index('ix_messages_search_vector', table_name='messages', postgresql_using='gin')
    op.drop_index('ix_messages_conversation_id_created_at_id_desc', table_name='messages')
    op.drop_column('conversations', 'version')
    op.drop_column('messages', 'search_vector')
