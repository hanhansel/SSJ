"""add Master Project client details and PRJ technician tasks

Revision ID: c3d29f8e5b42
Revises: b2c18e7d4a31
Create Date: 2026-09-09
"""
from alembic import op
import sqlalchemy as sa


revision = 'c3d29f8e5b42'
down_revision = 'b2c18e7d4a31'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('master_projects') as batch_op:
        batch_op.add_column(sa.Column('client_id', sa.Integer(), nullable=False))
        batch_op.add_column(sa.Column('site_address', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('notes', sa.Text(), nullable=True))
        batch_op.create_index('ix_master_projects_client_id', ['client_id'], unique=False)
        batch_op.create_foreign_key('fk_master_projects_client', 'client_sites', ['client_id'], ['id'])
    op.create_table(
        'prj_technician_tasks',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('prj_id', sa.Integer(), sa.ForeignKey('doc_prj.id'), nullable=False),
        sa.Column('description', sa.Text(), nullable=False),
        sa.Column('display_order', sa.Integer(), nullable=False, server_default='0'),
    )
    op.create_index('ix_prj_technician_tasks_prj_id', 'prj_technician_tasks', ['prj_id'])


def downgrade():
    op.drop_index('ix_prj_technician_tasks_prj_id', table_name='prj_technician_tasks')
    op.drop_table('prj_technician_tasks')
    with op.batch_alter_table('master_projects') as batch_op:
        batch_op.drop_constraint('fk_master_projects_client', type_='foreignkey')
        batch_op.drop_index('ix_master_projects_client_id')
        batch_op.drop_column('notes')
        batch_op.drop_column('site_address')
        batch_op.drop_column('client_id')
