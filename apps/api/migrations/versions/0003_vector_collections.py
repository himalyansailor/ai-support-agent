"""vector_collections: pgvector collection registry (name -> dimensions, owning organisation and KB)

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-24 18:00:00.000000

Hand-checked against supportcore.db.models.vector. organisation_id / knowledge_base_id record the
collection's owner, checked by PgVectorStore on every operation (no FKs: see the model docstring). The per-dimension kb_vectors_{dim} tables are created lazily by PgVectorStore (not by
migrations) and are excluded from autogenerate/`alembic check` in env.py's include_object.
"""
from alembic import op
import sqlalchemy as sa


revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('vector_collections',
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('dimensions', sa.Integer(), nullable=False),
    sa.Column('organisation_id', sa.UUID(), nullable=False),
    sa.Column('knowledge_base_id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text("timezone('utc', now())"), nullable=False),
    sa.PrimaryKeyConstraint('name')
    )


def downgrade() -> None:
    op.drop_table('vector_collections')
