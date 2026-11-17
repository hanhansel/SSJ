"""one HO per PRJ sharing its number; final Completed/Void statuses; free-text SO number

Revision ID: f6a7b8c9d0e1
Revises: e5f4b0a1c7d8
Create Date: 2026-10-16

* Status vocabulary: 'Batal/Void' / 'Batal' -> 'Void', 'Completed' -> 'Completed'.
* doc_prj.so_number is no longer unique (a replacement PRJ may reuse the SO of a voided one).
* hos: every HO belongs to exactly one PRJ and carries the PRJ's number and
  date, so ho_no / ho_period are dropped and prj_id becomes NOT NULL.
  Orphan HOs are deleted, PRJs without a HO get one, and every HO status
  mirrors its PRJ.  ho_number_text is cleared here and re-frozen from the PRJ
  by run.py's backfill_document_numbers() on the next start.

The hos tables used to be created only by db.create_all(), so this revision
also creates them when they do not exist yet.
"""
from alembic import op
import sqlalchemy as sa


revision = 'f6a7b8c9d0e1'
down_revision = 'e5f4b0a1c7d8'
branch_labels = None
depends_on = None

# Lets SQLite batch mode name the unnamed UNIQUE constraints that create_all() produced.
NAMING_CONVENTION = {'uq': 'uq_%(table_name)s_%(column_0_name)s'}

HO_STATUS_FROM_PRJ = (
    "CASE (SELECT documents.status FROM documents WHERE documents.id = hos.prj_id) "
    "WHEN 'Completed' THEN 'Approved' "
    "WHEN 'Void' THEN 'Void' "
    "ELSE (SELECT documents.status FROM documents WHERE documents.id = hos.prj_id) END"
)


def _create_ho_tables():
    op.create_table(
        'hos',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('ho_number_text', sa.String(length=100), nullable=True),
        sa.Column('date', sa.Date(), nullable=False),
        sa.Column('prj_id', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(length=50), nullable=False),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('uploaded_at', sa.DateTime(), nullable=True),
        sa.Column('uploaded_by', sa.Integer(), nullable=True),
        sa.Column('approved_at', sa.DateTime(), nullable=True),
        sa.Column('approved_by', sa.Integer(), nullable=True),
        sa.Column('completion_date', sa.Date(), nullable=True),
        sa.ForeignKeyConstraint(['prj_id'], ['doc_prj.id']),
        sa.ForeignKeyConstraint(['uploaded_by'], ['users.id']),
        sa.ForeignKeyConstraint(['approved_by'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('prj_id'),
    )
    op.create_table(
        'ho_photos',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('ho_id', sa.Integer(), nullable=False),
        sa.Column('stored_path', sa.String(length=500), nullable=False),
        sa.Column('original_filename', sa.String(length=255), nullable=False),
        sa.Column('file_size', sa.Integer(), nullable=True),
        sa.Column('uploaded_at', sa.DateTime(), nullable=False),
        sa.Column('uploaded_by', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['ho_id'], ['hos.id']),
        sa.ForeignKeyConstraint(['uploaded_by'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('stored_path', name='uq_ho_photos_stored_path'),
    )
    op.create_index('ix_ho_photos_ho_id', 'ho_photos', ['ho_id'], unique=False)


def _reshape_existing_hos(inspector):
    # Orphan HOs cannot exist any more; their photo rows go with them.
    op.execute("DELETE FROM ho_photos WHERE ho_id IN (SELECT id FROM hos WHERE prj_id IS NULL)")
    op.execute("DELETE FROM hos WHERE prj_id IS NULL")
    op.execute(
        "UPDATE hos SET ho_number_text = NULL, "
        "date = (SELECT documents.date FROM documents WHERE documents.id = hos.prj_id)"
    )

    columns = {column['name'] for column in inspector.get_columns('hos')}
    numbering_constraints = [
        constraint['name'] or f"uq_hos_{constraint['column_names'][0]}"
        for constraint in inspector.get_unique_constraints('hos')
        if {'ho_no', 'ho_period'} & set(constraint['column_names'])
    ]
    with op.batch_alter_table('hos', naming_convention=NAMING_CONVENTION) as batch_op:
        for name in numbering_constraints:
            batch_op.drop_constraint(name, type_='unique')
        for column in ('ho_no', 'ho_period'):
            if column in columns:
                batch_op.drop_column(column)
        batch_op.alter_column('prj_id', existing_type=sa.Integer(), nullable=False)


def _sqlite_foreign_keys(enabled):
    """SQLite batch mode rebuilds a table by dropping it, which the app's
    PRAGMA foreign_keys=ON would refuse while other rows reference it.
    The PRAGMA only takes effect outside a transaction."""
    if op.get_bind().dialect.name == 'sqlite':
        with op.get_context().autocommit_block():
            op.execute(f"PRAGMA foreign_keys={'ON' if enabled else 'OFF'}")


def upgrade():
    _sqlite_foreign_keys(False)
    _upgrade()
    _sqlite_foreign_keys(True)


def downgrade():
    _sqlite_foreign_keys(False)
    _downgrade()
    _sqlite_foreign_keys(True)


def _upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    op.execute("UPDATE documents SET status = 'Void' WHERE status IN ('Batal/Void', 'Batal')")
    op.execute("UPDATE documents SET status = 'Completed' WHERE status = 'Completed'")
    op.execute("UPDATE master_projects SET status = 'Completed' WHERE status = 'Completed'")

    so_constraints = [
        constraint['name'] or 'uq_doc_prj_so_number'
        for constraint in inspector.get_unique_constraints('doc_prj')
        if constraint['column_names'] == ['so_number']
    ]
    so_indexes = [
        index['name'] for index in inspector.get_indexes('doc_prj')
        if index.get('unique') and index['column_names'] == ['so_number']
    ]
    if so_constraints or so_indexes:
        with op.batch_alter_table('doc_prj', naming_convention=NAMING_CONVENTION) as batch_op:
            for name in so_constraints:
                batch_op.drop_constraint(name, type_='unique')
            for name in so_indexes:
                batch_op.drop_index(name)

    if inspector.has_table('hos'):
        _reshape_existing_hos(inspector)
    else:
        _create_ho_tables()

    # Every PRJ has exactly one HO.
    op.execute(
        "INSERT INTO hos (prj_id, date, status, notes, created_at) "
        "SELECT doc_prj.id, documents.date, 'Online', '', documents.created_at "
        "FROM doc_prj JOIN documents ON documents.id = doc_prj.id "
        "WHERE NOT EXISTS (SELECT 1 FROM hos WHERE hos.prj_id = doc_prj.id)"
    )
    op.execute(f"UPDATE hos SET status = {HO_STATUS_FROM_PRJ}")


def _downgrade():
    with op.batch_alter_table('hos', naming_convention=NAMING_CONVENTION) as batch_op:
        batch_op.alter_column('prj_id', existing_type=sa.Integer(), nullable=True)
        batch_op.add_column(sa.Column('ho_no', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('ho_period', sa.String(length=10), nullable=True))
    op.execute("UPDATE hos SET ho_no = (SELECT documents.doc_no FROM documents WHERE documents.id = hos.prj_id)")
    op.execute("UPDATE hos SET ho_period = (SELECT documents.doc_period FROM documents WHERE documents.id = hos.prj_id)")
    with op.batch_alter_table('hos', naming_convention=NAMING_CONVENTION) as batch_op:
        batch_op.alter_column('ho_no', existing_type=sa.Integer(), nullable=False)
        batch_op.alter_column('ho_period', existing_type=sa.String(length=10), nullable=False)
        batch_op.create_unique_constraint('uq_ho_period_no', ['ho_period', 'ho_no'])

    # Fails if PRJs now share an SO number; resolve those duplicates before downgrading.
    with op.batch_alter_table('doc_prj') as batch_op:
        batch_op.create_unique_constraint('uq_doc_prj_so_number', ['so_number'])

    op.execute("UPDATE hos SET status = 'Batal/Void' WHERE status = 'Void'")
    op.execute("UPDATE documents SET status = 'Batal/Void' WHERE status = 'Void'")
