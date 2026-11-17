"""add captain_id (team captain) to documents

Revision ID: h8c9d0e1f2a3
Revises: g7b8c9d0e1f2
Create Date: 2026-10-27
"""
from alembic import op
import sqlalchemy as sa


revision = 'h8c9d0e1f2a3'
down_revision = 'g7b8c9d0e1f2'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('documents') as batch_op:
        batch_op.add_column(sa.Column('captain_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            'fk_documents_captain_id_users', 'users', ['captain_id'], ['id'], ondelete='SET NULL',
        )


def downgrade():
    with op.batch_alter_table('documents') as batch_op:
        batch_op.drop_constraint('fk_documents_captain_id_users', type_='foreignkey')
        batch_op.drop_column('captain_id')
