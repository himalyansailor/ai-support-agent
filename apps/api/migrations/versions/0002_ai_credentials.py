"""ai_credentials: provider credentials (encrypted API keys) and per-org AI settings

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-24 13:46:48.996432

Hand-checked against supportcore.db.models.ai:
- provider_credentials: tenant table (organisation_id FK CASCADE + index), unique index (organisation_id, lower(label)),
  created_by_user_id FK SET NULL. encrypted_api_key holds SecretCipher ciphertext only.
- organisation_ai_settings: organisation_id is the primary key (FK CASCADE, so no extra index);
  chat_credential_id FK SET NULL, indexed for the credential in-use check.
"""
from alembic import op
import sqlalchemy as sa


revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('provider_credentials',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('provider', sa.String(length=32), nullable=False),
    sa.Column('label', sa.String(length=100), nullable=False),
    sa.Column('encrypted_api_key', sa.Text(), nullable=False),
    sa.Column('base_url', sa.String(length=2048), nullable=True),
    sa.Column('last4', sa.String(length=4), nullable=False),
    sa.Column('created_by_user_id', sa.UUID(), nullable=True),
    sa.Column('last_validated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('organisation_id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text("timezone('utc', now())"), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text("timezone('utc', now())"), nullable=False),
    sa.ForeignKeyConstraint(['created_by_user_id'], ['users.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['organisation_id'], ['organisations.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('uq_provider_credentials_organisation_id_lower_label', 'provider_credentials', ['organisation_id', sa.text('lower(label)')], unique=True)
    op.create_index(op.f('ix_provider_credentials_organisation_id'), 'provider_credentials', ['organisation_id'], unique=False)
    op.create_table('organisation_ai_settings',
    sa.Column('organisation_id', sa.UUID(), nullable=False),
    sa.Column('chat_provider', sa.String(length=32), nullable=True),
    sa.Column('chat_model', sa.String(length=255), nullable=True),
    sa.Column('chat_credential_id', sa.UUID(), nullable=True),
    sa.Column('temperature', sa.Float(), server_default=sa.text('0.2'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text("timezone('utc', now())"), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text("timezone('utc', now())"), nullable=False),
    sa.ForeignKeyConstraint(['chat_credential_id'], ['provider_credentials.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['organisation_id'], ['organisations.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('organisation_id')
    )
    op.create_index(op.f('ix_organisation_ai_settings_chat_credential_id'), 'organisation_ai_settings', ['chat_credential_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_organisation_ai_settings_chat_credential_id'), table_name='organisation_ai_settings')
    op.drop_table('organisation_ai_settings')
    op.drop_index('uq_provider_credentials_organisation_id_lower_label', table_name='provider_credentials')
    op.drop_index(op.f('ix_provider_credentials_organisation_id'), table_name='provider_credentials')
    op.drop_table('provider_credentials')
