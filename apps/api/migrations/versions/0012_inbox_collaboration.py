"""inbox collaboration: internal notes, their mentions, and the author of agent messages

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-25 16:16:37.866113
"""
from alembic import op
import sqlalchemy as sa


revision = '0012'
down_revision = '0011'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('notes',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('conversation_id', sa.UUID(), nullable=False),
    sa.Column('author_membership_id', sa.UUID(), nullable=True),
    sa.Column('text', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('organisation_id', sa.UUID(), nullable=False),
    sa.ForeignKeyConstraint(['author_membership_id'], ['memberships.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['organisation_id'], ['organisations.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_notes_author_membership_id', 'notes', ['author_membership_id'], unique=False)
    op.create_index('ix_notes_conversation_id_created_at', 'notes', ['conversation_id', 'created_at'], unique=False)
    op.create_index(op.f('ix_notes_organisation_id'), 'notes', ['organisation_id'], unique=False)
    op.create_table('mentions',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('note_id', sa.UUID(), nullable=False),
    sa.Column('conversation_id', sa.UUID(), nullable=False),
    sa.Column('membership_id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('organisation_id', sa.UUID(), nullable=False),
    sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['membership_id'], ['memberships.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['note_id'], ['notes.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['organisation_id'], ['organisations.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('note_id', 'membership_id', name='uq_mentions_note_id_membership_id')
    )
    op.create_index('ix_mentions_conversation_id', 'mentions', ['conversation_id'], unique=False)
    op.create_index('ix_mentions_membership_id_conversation_id', 'mentions', ['membership_id', 'conversation_id'], unique=False)
    op.create_index(op.f('ix_mentions_organisation_id'), 'mentions', ['organisation_id'], unique=False)
    op.add_column('messages', sa.Column('author_membership_id', sa.UUID(), nullable=True))
    op.create_index('ix_messages_author_membership_id', 'messages', ['author_membership_id'], unique=False, postgresql_where=sa.text('author_membership_id IS NOT NULL'))
    op.create_foreign_key('messages_author_membership_id_fkey', 'messages', 'memberships', ['author_membership_id'], ['id'], ondelete='SET NULL')


def downgrade() -> None:
    op.drop_constraint('messages_author_membership_id_fkey', 'messages', type_='foreignkey')
    op.drop_index('ix_messages_author_membership_id', table_name='messages', postgresql_where=sa.text('author_membership_id IS NOT NULL'))
    op.drop_column('messages', 'author_membership_id')
    op.drop_index(op.f('ix_mentions_organisation_id'), table_name='mentions')
    op.drop_index('ix_mentions_membership_id_conversation_id', table_name='mentions')
    op.drop_index('ix_mentions_conversation_id', table_name='mentions')
    op.drop_table('mentions')
    op.drop_index(op.f('ix_notes_organisation_id'), table_name='notes')
    op.drop_index('ix_notes_conversation_id_created_at', table_name='notes')
    op.drop_index('ix_notes_author_membership_id', table_name='notes')
    op.drop_table('notes')
