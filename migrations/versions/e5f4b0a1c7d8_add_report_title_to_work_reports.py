"""add report_title to work_reports

Revision ID: e5f4b0a1c7d8
Revises: a1b2c3d4e5f6
Create Date: 2026-10-06
"""
from alembic import op
import sqlalchemy as sa


revision = 'e5f4b0a1c7d8'
down_revision = 'a1b2c3d4e5f6'
branch_labels = None
depends_on = None


def upgrade():
    # Nullable so existing reports (created before titles existed) stay valid.
    with op.batch_alter_table('work_reports') as batch_op:
        batch_op.add_column(sa.Column('report_title', sa.String(length=200), nullable=True))


def downgrade():
    with op.batch_alter_table('work_reports') as batch_op:
        batch_op.drop_column('report_title')
