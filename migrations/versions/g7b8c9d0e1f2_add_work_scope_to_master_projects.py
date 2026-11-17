"""add work_scope (Pekerjaan) to master_projects

Revision ID: g7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-10-20
"""
from alembic import op
import sqlalchemy as sa


revision = 'g7b8c9d0e1f2'
down_revision = 'f6a7b8c9d0e1'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('master_projects') as batch_op:
        batch_op.add_column(sa.Column('work_scope', sa.Text(), nullable=True))


def downgrade():
    with op.batch_alter_table('master_projects') as batch_op:
        batch_op.drop_column('work_scope')
