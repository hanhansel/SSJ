"""add_doc_period

Revision ID: a1b2c3d4e5f6
Revises: 59b877f91dae
Create Date: 2026-09-24 16:30:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a1b2c3d4e5f6'
down_revision = '59b877f91dae'
branch_labels = None
depends_on = None


def upgrade():
    # 1. Add column as nullable first
    with op.batch_alter_table('documents', schema=None) as batch_op:
        batch_op.add_column(sa.Column('doc_period', sa.String(length=10), nullable=True))
    
    # 2. Backfill existing data using the year from date
    conn = op.get_bind()
    if conn.dialect.name == 'postgresql':
        op.execute(
            "UPDATE documents SET doc_period = CAST(EXTRACT(YEAR FROM date) AS VARCHAR) "
            "WHERE date IS NOT NULL"
        )
    else:
        op.execute(
            "UPDATE documents SET doc_period = strftime('%Y', date) "
            "WHERE date IS NOT NULL"
        )
    # Fallback for any rows without date
    op.execute(
        "UPDATE documents SET doc_period = 'all' WHERE doc_period IS NULL"
    )
    
    # 3. Make column NOT NULL and swap constraints
    with op.batch_alter_table('documents', schema=None) as batch_op:
        batch_op.alter_column('doc_period', nullable=False, server_default='all')
        
        # 4. Swap constraint
        batch_op.drop_constraint('uq_document_type_no', type_='unique')
        batch_op.create_unique_constraint('uq_document_type_no_period', ['document_type', 'doc_no', 'doc_period'])


def downgrade():
    with op.batch_alter_table('documents', schema=None) as batch_op:
        batch_op.drop_constraint('uq_document_type_no_period', type_='unique')
        batch_op.create_unique_constraint('uq_document_type_no', ['document_type', 'doc_no'])
        batch_op.drop_column('doc_period')
