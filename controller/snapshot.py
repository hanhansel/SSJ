"""SSJ data snapshots: export every table + uploads to one file, import it by column name.

Workflow around a model refactor (e.g. a new column):
    export_snapshot()  -> on the old version: reads the tables that exist in the DB
                          (reflection, not the models) plus the uploads folder
    ...wipe / reinstall the new version...
    import_snapshot()  -> rebuild_schema() (DROP SCHEMA public + db.create_all() in one
                          shot), then inserts the rows into the *new* models matching
                          columns by name: new columns get their default/NULL, removed
                          columns are ignored

A snapshot file (.ssjbak) is a zip:
    manifest.json          format version, creation time, columns + row count per table
    data/<table>.json      rows of that table (dates/decimals tagged for a lossless round-trip)
    uploads/...            copy of UPLOAD_FOLDER (HO photos, report photos, print templates)
"""

import base64
import json
import os
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path

from sqlalchemy import MetaData, create_engine, select, text

SNAPSHOT_SUFFIX = '.ssjbak'
FORMAT_VERSION = 1
# Short-lived security state; never worth restoring.
EXCLUDED_TABLES = {'alembic_version', 'password_reset_otps', 'auth_rate_limits'}
STORED_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.pdf', '.zip'}


class SnapshotError(Exception):
    """A snapshot cannot be created or imported safely; the message is user-facing."""


def default_snapshot_dir():
    """Outside SSJ_DATA_ROOT, so data-folder wipes and reinstalls never delete snapshots."""
    return Path.home() / 'Documents' / 'SSJ Snapshots'


def _engine(database_url):
    if not database_url:
        raise SnapshotError('DATABASE_URL is not configured yet.')
    if database_url.startswith(('postgresql', 'postgres')):
        # Same schema as the web app (run.py); never the role's "$user" schema.
        return create_engine(database_url, connect_args={'options': '-c search_path=public'})
    return create_engine(database_url)


# ---------- lossless JSON encoding ----------
def _encode(value):
    if isinstance(value, datetime):
        return {'$dt': value.isoformat()}
    if isinstance(value, date):
        return {'$d': value.isoformat()}
    if isinstance(value, time):
        return {'$t': value.isoformat()}
    if isinstance(value, Decimal):
        return {'$dec': str(value)}
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {'$b64': base64.b64encode(bytes(value)).decode('ascii')}
    return value


def _decode(value):
    if isinstance(value, dict) and len(value) == 1:
        (tag, raw), = value.items()
        if tag == '$dt':
            return datetime.fromisoformat(raw)
        if tag == '$d':
            return date.fromisoformat(raw)
        if tag == '$t':
            return time.fromisoformat(raw)
        if tag == '$dec':
            return Decimal(raw)
        if tag == '$b64':
            return base64.b64decode(raw)
    return value


# ---------- export ----------
def export_snapshot(database_url, upload_dir, output_dir=None, include_uploads=True, label='snapshot'):
    """Write every table that exists in the database (plus uploads) to one .ssjbak file."""
    engine = _engine(database_url)
    output_dir = Path(output_dir or default_snapshot_dir())
    output_dir.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now().strftime('%Y-%m-%d_%H%M%S')
    final_path = output_dir / f'ssj_{label}_{stamp}{SNAPSHOT_SUFFIX}'
    temp_path = final_path.with_suffix(final_path.suffix + '.tmp')

    metadata = MetaData()
    try:
        manifest = {
            'format_version': FORMAT_VERSION,
            'created_at': datetime.now().isoformat(timespec='seconds'),
            'dialect': engine.dialect.name,
            'tables': {},
            'upload_files': 0,
        }
        with zipfile.ZipFile(temp_path, 'w', zipfile.ZIP_DEFLATED) as archive:
            with engine.connect() as conn:
                if engine.dialect.name == 'postgresql':
                    # One frozen view of the whole database, as pg_dump takes: the
                    # server keeps running, and anything written after the first
                    # read lands in the next snapshot instead of half in this one.
                    conn.execution_options(isolation_level='REPEATABLE READ', postgresql_readonly=True)
                metadata.reflect(bind=conn)
                for table in metadata.sorted_tables:
                    if table.name in EXCLUDED_TABLES:
                        continue
                    query = select(table)
                    if table.primary_key.columns:
                        query = query.order_by(*table.primary_key.columns)
                    rows = [
                        {key: _encode(value) for key, value in row._mapping.items()}
                        for row in conn.execute(query)
                    ]
                    archive.writestr(f'data/{table.name}.json', json.dumps(rows, ensure_ascii=False))
                    manifest['tables'][table.name] = {
                        'columns': [column.name for column in table.columns],
                        'rows': len(rows),
                    }

            if include_uploads and upload_dir and Path(upload_dir).is_dir():
                upload_root = Path(upload_dir)
                for path in sorted(upload_root.rglob('*')):
                    if not path.is_file():
                        continue
                    arcname = 'uploads/' + path.relative_to(upload_root).as_posix()
                    compression = zipfile.ZIP_STORED if path.suffix.lower() in STORED_EXTENSIONS else zipfile.ZIP_DEFLATED
                    archive.write(path, arcname, compress_type=compression)
                    manifest['upload_files'] += 1

            archive.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2))
        os.replace(temp_path, final_path)
    except SnapshotError:
        raise
    except Exception as error:
        raise SnapshotError(f'Could not create the snapshot: {error}') from error
    finally:
        engine.dispose()
        if temp_path.exists():
            temp_path.unlink()
    return final_path


# ---------- import planning ----------
@dataclass
class TablePlan:
    name: str
    source_name: str
    rows: int
    new_columns: list = field(default_factory=list)
    dropped_columns: list = field(default_factory=list)
    blocking_columns: list = field(default_factory=list)


@dataclass
class ImportPlan:
    manifest: dict
    tables: list
    missing_in_snapshot: list
    dropped_tables: list

    @property
    def blocking(self):
        return [t for t in self.tables if t.blocking_columns and t.rows]

    def summary(self):
        lines = [f"Snapshot created: {self.manifest.get('created_at', '-')}"]
        lines.append(f"Total rows: {sum(t.rows for t in self.tables)} | upload files: {self.manifest.get('upload_files', 0)}")
        for plan in self.tables:
            if plan.new_columns:
                lines.append(f'+ {plan.name}: new column(s) {", ".join(plan.new_columns)} (filled with default/empty)')
            if plan.dropped_columns:
                lines.append(f'- {plan.name}: column(s) {", ".join(plan.dropped_columns)} no longer in the model (dropped)')
        if self.missing_in_snapshot:
            lines.append(f'+ New table(s) (empty): {", ".join(self.missing_in_snapshot)}')
        if self.dropped_tables:
            lines.append(f'- Table(s) no longer in the model (dropped): {", ".join(self.dropped_tables)}')
        for plan in self.blocking:
            lines.append(
                f'!! {plan.name}: required column(s) {", ".join(plan.blocking_columns)} have no old value '
                'and no default. Add default= to the model before importing.'
            )
        return '\n'.join(lines)


def _rules():
    from apps.snapshot_rules import COLUMN_RENAMES, TABLE_RENAMES
    return TABLE_RENAMES, COLUMN_RENAMES


def _target_metadata():
    from apps.models import db
    return db.metadata


def _column_has_default(column):
    return (
        column.default is not None
        or column.server_default is not None
        or column.nullable
        or (column.primary_key and column.autoincrement in (True, 'auto') and not column.foreign_keys)
    )


def read_manifest(snapshot_path):
    try:
        with zipfile.ZipFile(snapshot_path) as archive:
            manifest = json.loads(archive.read('manifest.json'))
    except (OSError, KeyError, zipfile.BadZipFile, json.JSONDecodeError) as error:
        raise SnapshotError(f'Invalid snapshot file: {error}') from error
    if manifest.get('format_version') != FORMAT_VERSION:
        raise SnapshotError(f"Snapshot format version {manifest.get('format_version')} is not supported.")
    return manifest


def plan_import(snapshot_path):
    """Compare the snapshot's tables/columns with the current models before touching the DB."""
    manifest = read_manifest(snapshot_path)
    table_renames, column_renames = _rules()
    reverse_tables = {new: old for old, new in table_renames.items()}
    source_tables = manifest['tables']

    plans, missing = [], []
    used_sources = set()
    for table in _target_metadata().sorted_tables:
        if table.name in EXCLUDED_TABLES:
            continue
        source_name = reverse_tables.get(table.name, table.name)
        source = source_tables.get(source_name)
        if source is None:
            missing.append(table.name)
            continue
        used_sources.add(source_name)
        source_columns = set(source['columns'])
        mapped = {column_renames.get(f'{table.name}.{c}', c) for c in source_columns}
        plan = TablePlan(table.name, source_name, source['rows'])
        for column in table.columns:
            if column.name not in mapped:
                plan.new_columns.append(column.name)
                if not _column_has_default(column):
                    plan.blocking_columns.append(column.name)
        target_names = {c.name for c in table.columns}
        plan.dropped_columns = sorted(c for c in mapped if c not in target_names)
        plans.append(plan)

    dropped_tables = sorted(name for name in source_tables if name not in used_sources and name not in EXCLUDED_TABLES)
    return ImportPlan(manifest, plans, missing, dropped_tables)


# ---------- rebuild + import ----------
def rebuild_schema(database_url):
    """Empty the database and create every table from the current models in one shot."""
    engine = _engine(database_url)
    metadata = _target_metadata()
    try:
        if engine.dialect.name == 'postgresql':
            with engine.begin() as conn:
                # Recreated by the connecting user (ssj_app), who therefore owns it.
                conn.execute(text('DROP SCHEMA IF EXISTS public CASCADE'))
                conn.execute(text('CREATE SCHEMA public'))
                conn.execute(text('GRANT ALL ON SCHEMA public TO public'))
        else:
            existing = MetaData()
            existing.reflect(bind=engine)
            existing.drop_all(bind=engine)
        metadata.create_all(bind=engine)
    except Exception as error:
        raise SnapshotError(f'Could not rebuild the database: {error}') from error
    finally:
        engine.dispose()


def _reset_sequences(conn, metadata):
    for table in metadata.sorted_tables:
        pk = list(table.primary_key.columns)
        if len(pk) != 1 or pk[0].foreign_keys:
            continue
        column = pk[0].name
        conn.execute(text(
            f"SELECT setval(pg_get_serial_sequence('{table.name}', '{column}'), "
            f"COALESCE((SELECT MAX({column}) FROM {table.name}), 1), "
            f"(SELECT MAX({column}) FROM {table.name}) IS NOT NULL)"
        ))


def _restore_uploads(archive, upload_dir):
    upload_root = Path(upload_dir).resolve()
    restored = 0
    for member in archive.namelist():
        if not member.startswith('uploads/') or member.endswith('/'):
            continue
        target = (upload_root / member[len('uploads/'):]).resolve()
        if os.path.commonpath([upload_root, target]) != str(upload_root):
            continue  # zip-slip guard
        target.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(member) as source, open(target, 'wb') as destination:
            destination.write(source.read())
        restored += 1
    return restored


def import_snapshot(database_url, snapshot_path, upload_dir=None, restore_uploads=True, rebuild=True):
    """Rebuild the schema from the current models and load the snapshot into it.

    Everything is inserted in one transaction: either all rows land, or none do.
    Returns a short user-facing summary.
    """
    plan = plan_import(snapshot_path)
    if plan.blocking:
        raise SnapshotError('Import cancelled:\n' + plan.summary())

    if rebuild:
        rebuild_schema(database_url)

    _, column_renames = _rules()
    metadata = _target_metadata()
    tables = {table.name: table for table in metadata.sorted_tables}
    engine = _engine(database_url)
    inserted = 0
    restored = 0
    try:
        with zipfile.ZipFile(snapshot_path) as archive:
            with engine.begin() as conn:
                for table_plan in plan.tables:
                    if not table_plan.rows:
                        continue
                    table = tables[table_plan.name]
                    target_names = {c.name for c in table.columns}
                    rows = json.loads(archive.read(f'data/{table_plan.source_name}.json'))
                    payload = []
                    for row in rows:
                        record = {}
                        for source_column, value in row.items():
                            name = column_renames.get(f'{table.name}.{source_column}', source_column)
                            if name in target_names:
                                record[name] = _decode(value)
                        payload.append(record)
                    conn.execute(table.insert(), payload)
                    inserted += len(payload)
                if engine.dialect.name == 'postgresql':
                    _reset_sequences(conn, metadata)
            if restore_uploads and upload_dir:
                restored = _restore_uploads(archive, upload_dir)
    except SnapshotError:
        raise
    except Exception as error:
        raise SnapshotError(f'Import failed; no data was saved: {error}') from error
    finally:
        engine.dispose()

    return f'{inserted} data row(s) and {restored} upload file(s) restored.\n\n{plan.summary()}'
