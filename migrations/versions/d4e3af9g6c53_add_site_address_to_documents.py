"""add site_address to documents table

Revision ID: d4e3af9g6c53
Revises: c3d29f8e5b42
Create Date: 2026-09-22
"""
from alembic import op
import sqlalchemy as sa


revision = 'd4e3af9g6c53'
down_revision = 'c3d29f8e5b42'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('documents') as batch_op:
        batch_op.add_column(sa.Column('site_address', sa.Text(), nullable=True))


def downgrade():
    with op.batch_alter_table('documents') as batch_op:
        batch_op.drop_column('site_address')
