"""add PRJ project fields

Revision ID: b2c18e7d4a31
Revises: 9fd6fa41c2df
Create Date: 2026-09-08
"""
from alembic import op
import sqlalchemy as sa


revision = 'b2c18e7d4a31'
down_revision = '9fd6fa41c2df'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('doc_prj') as batch_op:
        batch_op.add_column(sa.Column('master_project_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('so_number', sa.String(length=200), nullable=False))
        batch_op.add_column(sa.Column('site_address', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('admin_notes', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('target_completion', sa.Date(), nullable=True))
        batch_op.add_column(sa.Column('actual_completion', sa.Date(), nullable=True))
        batch_op.alter_column('urgency_level', existing_type=sa.String(length=50), type_=sa.Integer(), postgresql_using="CASE WHEN lower(urgency_level) IN ('urgent', '2') THEN 2 ELSE 1 END")
        batch_op.drop_column('installation_goal')
        batch_op.drop_column('notes')
        batch_op.create_index('ix_doc_prj_master_project_id', ['master_project_id'], unique=False)
        batch_op.create_unique_constraint('uq_doc_prj_so_number', ['so_number'])
        batch_op.create_foreign_key('fk_doc_prj_master_project', 'master_projects', ['master_project_id'], ['id'])

    with op.batch_alter_table('prj_items') as batch_op:
        batch_op.drop_column('estimated_days')


def downgrade():
    with op.batch_alter_table('prj_items') as batch_op:
        batch_op.add_column(sa.Column('estimated_days', sa.Integer(), nullable=True))

    with op.batch_alter_table('doc_prj') as batch_op:
        batch_op.drop_constraint('fk_doc_prj_master_project', type_='foreignkey')
        batch_op.drop_constraint('uq_doc_prj_so_number', type_='unique')
        batch_op.drop_index('ix_doc_prj_master_project_id')
        batch_op.add_column(sa.Column('notes', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('installation_goal', sa.Text(), nullable=True))
        batch_op.alter_column('urgency_level', existing_type=sa.Integer(), type_=sa.String(length=50), postgresql_using="CASE WHEN urgency_level = 2 THEN 'Urgent' ELSE 'Normal' END")
        batch_op.drop_column('actual_completion')
        batch_op.drop_column('target_completion')
        batch_op.drop_column('admin_notes')
        batch_op.drop_column('site_address')
        batch_op.drop_column('so_number')
        batch_op.drop_column('master_project_id')
