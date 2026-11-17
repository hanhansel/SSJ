"""Snapshot export -> rebuild -> import round trips, simulating a model refactor."""

import os
import tempfile
import unittest
import zipfile
from datetime import date
from pathlib import Path

from sqlalchemy import create_engine, text

from run import create_app
from apps.models import ALL_MENUS, ROLE_OFFICE
from apps.models import (
    db, User, Role, ClientSite, HOPhoto, SystemHoliday,
)
from apps.services.document_service import DocumentService
from controller.snapshot import SnapshotError, export_snapshot, import_snapshot, plan_import


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.db_url = f"sqlite:///{(root / 'ssj_test.db').as_posix()}"
        self.upload_dir = root / 'uploads'
        self.snapshot_dir = root / 'snapshots'
        self.original_env = {key: os.environ.get(key) for key in ('DATABASE_URL', 'APP_ENV')}
        os.environ['DATABASE_URL'] = self.db_url
        os.environ['APP_ENV'] = 'testing'

        app = create_app()
        self.assertTrue(app.config['SQLALCHEMY_DATABASE_URI'].startswith('sqlite'))
        app.config['UPLOAD_FOLDER'] = str(self.upload_dir)
        with app.app_context():
            role = Role.query.filter_by(code='Office').one()
            tech_role = Role.query.filter_by(code='Technician').one()
            admin = User(name='Admin Uji', email='admin@uji.test', password='hash', role_id=role.id, menus=ALL_MENUS)
            tech = User(name='Tek Uji', email='tek@uji.test', password='hash', role_id=tech_role.id)
            client = ClientSite(company_name='PT Klien Uji', address='Jl. Uji 1')
            db.session.add_all([admin, tech, client, SystemHoliday(date=date(2026, 12, 25), description='Natal')])
            db.session.flush()
            ok, prj, err = DocumentService.create_prj(
                client.id, 'PRJ Uji', 'SO-UJI-1', date(2026, 10, 1), 3, None, '', '2', 'catatan',
                [{'description': 'Tarik kabel', 'qty': 2, 'uom': 'Meter', 'display_order': 1}], [],
            )
            self.assertTrue(ok, err)
            prj.ho.photos.append(HOPhoto(stored_path='documents/ho/foto.jpg', original_filename='foto.jpg'))
            db.session.commit()
            self.prj_id = prj.id
            db.session.remove()
            db.engine.dispose()

        (self.upload_dir / 'documents' / 'ho').mkdir(parents=True)
        (self.upload_dir / 'documents' / 'ho' / 'foto.jpg').write_bytes(b'\xff\xd8jpeg-bytes')

    def tearDown(self):
        for key, value in self.original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def _scalar(self, sql):
        engine = create_engine(self.db_url)
        try:
            with engine.connect() as conn:
                return conn.execute(text(sql)).scalar()
        finally:
            engine.dispose()

    def _simulate_old_schema(self, table, column):
        """Pretend the database predates a column that the models now declare."""
        engine = create_engine(self.db_url)
        try:
            with engine.begin() as conn:
                conn.execute(text(f'ALTER TABLE {table} DROP COLUMN {column}'))
        finally:
            engine.dispose()

    def test_export_then_import_keeps_all_data_and_adds_new_column(self):
        # Old version: the column does not exist yet. Export from that database.
        self._simulate_old_schema('documents', 'doc_number_text')
        snapshot_path = export_snapshot(self.db_url, str(self.upload_dir), self.snapshot_dir)

        # New version: import rebuilds every table from the current models.
        summary = import_snapshot(self.db_url, snapshot_path, str(self.upload_dir))

        self.assertIn('documents: new column(s) doc_number_text', summary)
        # Every kind of user input survived: accounts, clients, documents, HO photos, holidays.
        self.assertEqual(self._scalar("SELECT COUNT(*) FROM users"), 2)
        self.assertEqual(self._scalar("SELECT company_name FROM client_sites"), 'PT Klien Uji')
        self.assertEqual(self._scalar(f"SELECT title FROM doc_prj WHERE id = {self.prj_id}"), 'PRJ Uji')
        self.assertEqual(self._scalar("SELECT COUNT(*) FROM ho_photos"), 1)
        self.assertEqual(self._scalar("SELECT date FROM system_holidays"), '2026-12-25')
        # The column exists again (rebuilt by create_all) and is empty for old rows.
        self.assertIsNone(self._scalar("SELECT doc_number_text FROM documents"))

    def test_required_new_column_blocks_before_touching_database(self):
        self._simulate_old_schema('doc_prj', 'title')  # NOT NULL, no default in the model
        snapshot_path = export_snapshot(self.db_url, str(self.upload_dir), self.snapshot_dir)

        with self.assertRaises(SnapshotError) as raised:
            import_snapshot(self.db_url, snapshot_path, str(self.upload_dir))

        self.assertIn('title', str(raised.exception))
        # Database untouched: the PRJ row is still there.
        self.assertEqual(self._scalar("SELECT COUNT(*) FROM doc_prj"), 1)

    def test_import_into_fresh_machine_restores_uploads(self):
        snapshot_path = export_snapshot(self.db_url, str(self.upload_dir), self.snapshot_dir)
        with zipfile.ZipFile(snapshot_path) as archive:
            self.assertIn('uploads/documents/ho/foto.jpg', archive.namelist())
            self.assertNotIn('data/password_reset_otps.json', archive.namelist())

        new_uploads = Path(self.tmp.name) / 'new_machine_uploads'
        self.assertEqual(plan_import(snapshot_path).blocking, [])
        import_snapshot(self.db_url, snapshot_path, str(new_uploads))

        self.assertEqual((new_uploads / 'documents' / 'ho' / 'foto.jpg').read_bytes(), b'\xff\xd8jpeg-bytes')
        self.assertEqual(self._scalar("SELECT COUNT(*) FROM prj_items"), 1)


if __name__ == '__main__':
    unittest.main()
