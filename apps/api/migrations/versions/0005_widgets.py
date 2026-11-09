"""widgets: embeddable chat widgets and their knowledge base links

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-25 10:33:19.327962

Hand-checked against supportcore.db.models.widget:
- widgets is a tenant table (organisation_id FK CASCADE + index); public_key has a unique index.
- widget_knowledge_bases: composite PK, both FKs CASCADE; knowledge_base_id indexed for KB deletes.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('widgets',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('public_key', sa.String(length=64), nullable=False),
    sa.Column('enabled', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('allowed_domains', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('appearance', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('csat_enabled', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('encrypted_identity_secret', sa.String(), nullable=True),
    sa.Column('identity_secret_last4', sa.String(length=4), nullable=True),
    sa.Column('identity_required', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('created_by_user_id', sa.UUID(), nullable=True),
    sa.Column('organisation_id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text("timezone('utc', now())"), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text("timezone('utc', now())"), nullable=False),
    sa.ForeignKeyConstraint(['created_by_user_id'], ['users.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['organisation_id'], ['organisations.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_widgets_organisation_id'), 'widgets', ['organisation_id'], unique=False)
    op.create_index(op.f('ix_widgets_public_key'), 'widgets', ['public_key'], unique=True)
    op.create_table('widget_knowledge_bases',
    sa.Column('widget_id', sa.UUID(), nullable=False),
    sa.Column('knowledge_base_id', sa.UUID(), nullable=False),
    sa.ForeignKeyConstraint(['knowledge_base_id'], ['knowledge_bases.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['widget_id'], ['widgets.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('widget_id', 'knowledge_base_id')
    )
    op.create_index(op.f('ix_widget_knowledge_bases_knowledge_base_id'), 'widget_knowledge_bases', ['knowledge_base_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_widget_knowledge_bases_knowledge_base_id'), table_name='widget_knowledge_bases')
    op.drop_table('widget_knowledge_bases')
    op.drop_index(op.f('ix_widgets_public_key'), table_name='widgets')
    op.drop_index(op.f('ix_widgets_organisation_id'), table_name='widgets')
    op.drop_table('widgets')
