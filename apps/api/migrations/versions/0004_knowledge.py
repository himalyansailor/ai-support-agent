"""knowledge: knowledge bases, documents, chunks and indexing jobs

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-24 15:07:18.989420

Hand-checked against supportcore.db.models.knowledge:
- Every table is a tenant table: organisation_id FK organisations CASCADE + index.
- knowledge_bases: unique index (organisation_id, lower(name)) matching KnowledgeBaseService's
  case-insensitive check; embedding_credential_id and building_embedding_credential_id FK
  provider_credentials SET NULL, each indexed for CredentialService's in-use check;
  created_by_user_id FK users SET NULL. building_* columns hold the config of building_version.
- documents: knowledge_base_id FK CASCADE; unique (knowledge_base_id, sha256) (also serves lookups by
  KB); status CHECK; uploaded_by_user_id FK users SET NULL.
- chunks: document_id and knowledge_base_id FK CASCADE; index (document_id, kb_version, ordinal) for
  the chunk browser and (knowledge_base_id, kb_version) for per-version cleanup after a rebuild.
- indexing_jobs: knowledge_base_id FK CASCADE, document_id nullable FK CASCADE (indexed); index
  (knowledge_base_id, queued_at DESC) for the job log; kind/status CHECKs.
- vector_collections already exists (0003).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('knowledge_bases',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('description', sa.Text(), server_default='', nullable=False),
    sa.Column('embedding_provider', sa.String(length=32), nullable=False),
    sa.Column('embedding_model', sa.String(length=255), nullable=False),
    sa.Column('embedding_dimensions', sa.Integer(), nullable=False),
    sa.Column('embedding_credential_id', sa.UUID(), nullable=True),
    sa.Column('chunking', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('reranker', sa.String(length=64), server_default='none', nullable=False),
    sa.Column('reranker_params', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('active_version', sa.Integer(), server_default=sa.text('1'), nullable=False),
    sa.Column('building_version', sa.Integer(), nullable=True),
    sa.Column('building_embedding_provider', sa.String(length=32), nullable=True),
    sa.Column('building_embedding_model', sa.String(length=255), nullable=True),
    sa.Column('building_embedding_dimensions', sa.Integer(), nullable=True),
    sa.Column('building_embedding_credential_id', sa.UUID(), nullable=True),
    sa.Column('building_chunking', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('created_by_user_id', sa.UUID(), nullable=True),
    sa.Column('organisation_id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text("timezone('utc', now())"), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text("timezone('utc', now())"), nullable=False),
    sa.ForeignKeyConstraint(['building_embedding_credential_id'], ['provider_credentials.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['created_by_user_id'], ['users.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['embedding_credential_id'], ['provider_credentials.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['organisation_id'], ['organisations.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_knowledge_bases_building_embedding_credential_id'), 'knowledge_bases', ['building_embedding_credential_id'], unique=False)
    op.create_index(op.f('ix_knowledge_bases_embedding_credential_id'), 'knowledge_bases', ['embedding_credential_id'], unique=False)
    op.create_index(op.f('ix_knowledge_bases_organisation_id'), 'knowledge_bases', ['organisation_id'], unique=False)
    op.create_index('uq_knowledge_bases_organisation_id_lower_name', 'knowledge_bases', ['organisation_id', sa.text('lower(name)')], unique=True)
    op.create_table('documents',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('knowledge_base_id', sa.UUID(), nullable=False),
    sa.Column('filename', sa.String(length=255), nullable=False),
    sa.Column('file_type', sa.String(length=16), nullable=False),
    sa.Column('size_bytes', sa.BigInteger(), nullable=False),
    sa.Column('sha256', sa.String(length=64), nullable=False),
    sa.Column('storage_key', sa.String(length=512), nullable=False),
    sa.Column('status', sa.String(length=16), server_default='uploaded', nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('chunk_count', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('indexed_version', sa.Integer(), nullable=True),
    sa.Column('uploaded_by_user_id', sa.UUID(), nullable=True),
    sa.Column('organisation_id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text("timezone('utc', now())"), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text("timezone('utc', now())"), nullable=False),
    sa.CheckConstraint("status IN ('uploaded', 'queued', 'processing', 'indexed', 'failed')", name='ck_documents_status'),
    sa.ForeignKeyConstraint(['knowledge_base_id'], ['knowledge_bases.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['organisation_id'], ['organisations.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['uploaded_by_user_id'], ['users.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('knowledge_base_id', 'sha256', name='uq_documents_knowledge_base_id_sha256')
    )
    op.create_index(op.f('ix_documents_organisation_id'), 'documents', ['organisation_id'], unique=False)
    op.create_table('chunks',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('knowledge_base_id', sa.UUID(), nullable=False),
    sa.Column('document_id', sa.UUID(), nullable=False),
    sa.Column('kb_version', sa.Integer(), nullable=False),
    sa.Column('ordinal', sa.Integer(), nullable=False),
    sa.Column('text', sa.Text(), nullable=False),
    sa.Column('char_start', sa.Integer(), nullable=False),
    sa.Column('char_end', sa.Integer(), nullable=False),
    sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('organisation_id', sa.UUID(), nullable=False),
    sa.ForeignKeyConstraint(['document_id'], ['documents.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['knowledge_base_id'], ['knowledge_bases.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['organisation_id'], ['organisations.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_chunks_document_id_kb_version_ordinal', 'chunks', ['document_id', 'kb_version', 'ordinal'], unique=False)
    op.create_index('ix_chunks_knowledge_base_id_kb_version', 'chunks', ['knowledge_base_id', 'kb_version'], unique=False)
    op.create_index(op.f('ix_chunks_organisation_id'), 'chunks', ['organisation_id'], unique=False)
    op.create_table('indexing_jobs',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('knowledge_base_id', sa.UUID(), nullable=False),
    sa.Column('document_id', sa.UUID(), nullable=True),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('kb_version', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=16), server_default='queued', nullable=False),
    sa.Column('attempts', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('steps', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('queued_at', sa.DateTime(timezone=True), server_default=sa.text("timezone('utc', now())"), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('organisation_id', sa.UUID(), nullable=False),
    sa.CheckConstraint("kind IN ('document', 'rebuild')", name='ck_indexing_jobs_kind'),
    sa.CheckConstraint("status IN ('queued', 'running', 'succeeded', 'failed')", name='ck_indexing_jobs_status'),
    sa.ForeignKeyConstraint(['document_id'], ['documents.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['knowledge_base_id'], ['knowledge_bases.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['organisation_id'], ['organisations.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_indexing_jobs_document_id'), 'indexing_jobs', ['document_id'], unique=False)
    op.create_index('ix_indexing_jobs_knowledge_base_id_queued_at', 'indexing_jobs', ['knowledge_base_id', sa.text('queued_at DESC')], unique=False)
    op.create_index(op.f('ix_indexing_jobs_organisation_id'), 'indexing_jobs', ['organisation_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_indexing_jobs_organisation_id'), table_name='indexing_jobs')
    op.drop_index('ix_indexing_jobs_knowledge_base_id_queued_at', table_name='indexing_jobs')
    op.drop_index(op.f('ix_indexing_jobs_document_id'), table_name='indexing_jobs')
    op.drop_table('indexing_jobs')
    op.drop_index(op.f('ix_chunks_organisation_id'), table_name='chunks')
    op.drop_index('ix_chunks_knowledge_base_id_kb_version', table_name='chunks')
    op.drop_index('ix_chunks_document_id_kb_version_ordinal', table_name='chunks')
    op.drop_table('chunks')
    op.drop_index(op.f('ix_documents_organisation_id'), table_name='documents')
    op.drop_table('documents')
    op.drop_index('uq_knowledge_bases_organisation_id_lower_name', table_name='knowledge_bases')
    op.drop_index(op.f('ix_knowledge_bases_organisation_id'), table_name='knowledge_bases')
    op.drop_index(op.f('ix_knowledge_bases_embedding_credential_id'), table_name='knowledge_bases')
    op.drop_index(op.f('ix_knowledge_bases_building_embedding_credential_id'), table_name='knowledge_bases')
    op.drop_table('knowledge_bases')
