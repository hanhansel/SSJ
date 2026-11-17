"""Settings > Print Templates: starter download, preview with test data, upload-then-confirm, remove."""

import os
import tempfile
import unittest
from datetime import date
from io import BytesIO
from pathlib import Path

from run import create_app
from apps.models import ALL_MENUS, ROLE_OFFICE
from apps.models import db, User, Role, ClientSite, Document, DocumentSVC, SystemSetting, DEFAULT_SYSTEM_SETTINGS


class PrintTemplateSettingsTests(unittest.TestCase):
    def setUp(self):
        self.original_env = {key: os.environ.get(key) for key in ('DATABASE_URL', 'APP_ENV', 'SSJ_DATA_ROOT')}
        self.data_root = tempfile.TemporaryDirectory()
        os.environ['DATABASE_URL'] = 'sqlite:///:memory:'
        os.environ['APP_ENV'] = 'testing'
        os.environ['SSJ_DATA_ROOT'] = self.data_root.name  # staging dir lives under the data root
        self.app = create_app()
        self.app.config['TESTING'] = True
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.app.config['UPLOAD_FOLDER'] = self.data_root.name
        self.template_dir = Path(self.data_root.name) / 'print-templates'
        self.app.config['PRINT_TEMPLATE_FOLDER'] = str(self.template_dir)
        self.client = self.app.test_client()
        with self.app.app_context():
            db.create_all()
            role = Role.query.filter_by(code='Office').one()
            admin = User(name='Admin', email='admin@example.test', password='x', role_id=role.id, menus=ALL_MENUS)
            db.session.add(admin)
            db.session.commit()
            admin_id = admin.id
        with self.client.session_transaction() as session:
            session['user_id'] = admin_id

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.drop_all()
        # The app log lives under the temporary data root; Windows cannot delete it while open.
        for handler in [h for h in self.app.logger.handlers if hasattr(h, 'baseFilename')]:
            self.app.logger.removeHandler(handler)
            handler.close()
        for key, value in self.original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.data_root.cleanup()

    def _stage(self, doc_type, source, filename='template_x.html'):
        return self.client.post(
            f'/system/settings/print-template/{doc_type}/stage',
            data={'template': (BytesIO(source.encode('utf-8')), filename)},
            content_type='multipart/form-data',
        )

    def test_settings_page_shows_three_actions_per_type(self):
        page = self.client.get('/system/settings')
        self.assertEqual(page.status_code, 200)
        for doc_type in ('svc', 'xprj', 'prj', 'ho'):
            self.assertIn(f'/system/settings/print-template/{doc_type}/download'.encode(), page.data)
            self.assertIn(f'tplFile-{doc_type}'.encode(), page.data)
        self.assertIn(b'Reset to Defaults', page.data)

    def test_preview_without_a_template_lists_the_variables_without_saving_anything(self):
        for doc_type, label, variable in (('svc', 'SVC', b'doc.customer_complaint'),
                                          ('xprj', 'XPRJ', b'doc.prj_parent.display_no'),
                                          ('prj', 'PRJ', b'doc.items'), ('ho', 'HO', b'ho.display_no')):
            response = self.client.get(f'/system/settings/print-template/{doc_type}/preview')
            self.assertEqual(response.status_code, 200, doc_type)
            self.assertIn(f'No print template for {label} documents'.encode(), response.data)
            self.assertIn(variable, response.data, doc_type)
        with self.app.app_context():
            self.assertEqual(ClientSite.query.count(), 0)
            self.assertEqual(Document.query.count(), 0)

    def test_starter_file_renders_and_lists_every_variable(self):
        from apps.services.print_template_service import PRINT_VARIABLES
        for doc_type, variables in PRINT_VARIABLES.items():
            starter = self.client.get(f'/system/settings/print-template/{doc_type}/download')
            self.assertEqual(starter.status_code, 200)
            self.assertIn(f'template_{doc_type}_starter.html', starter.headers['Content-Disposition'])
            for name, _ in variables:
                self.assertIn(name.encode(), starter.data, (doc_type, name))
            # The starter itself is a valid template: uploading it unchanged is accepted.
            self.assertEqual(self._stage(doc_type, starter.data.decode('utf-8')).status_code, 200, doc_type)

    def test_download_gives_the_starter_until_a_custom_template_is_applied(self):
        default = self.client.get('/system/settings/print-template/svc/download')
        self.assertEqual(default.status_code, 200)
        self.assertIn('template_svc_starter.html', default.headers['Content-Disposition'])
        self.assertIn(b'Variables available', default.data)

        custom_source = '<html><body>CUSTOM {{ doc.display_no }} {{ doc.client.company_name }}</body></html>'
        staged = self._stage('svc', custom_source)
        self.assertEqual(staged.status_code, 200)
        token = staged.get_json()['token']

        preview = self.client.get(staged.get_json()['preview_url'])
        self.assertIn(b'CUSTOM', preview.data)
        self.assertIn(b'Example Client Ltd', preview.data)

        applied = self.client.post('/system/settings/print-template/svc/apply', json={'token': token})
        self.assertTrue(applied.get_json()['ok'])

        downloaded = self.client.get('/system/settings/print-template/svc/download')
        self.assertIn('template_x.html', downloaded.headers['Content-Disposition'])
        self.assertEqual(downloaded.data.decode('utf-8'), custom_source)

        # Real documents now print with the custom template.
        with self.app.app_context():
            client = ClientSite(company_name='Klien Asli', address='Jl. A')
            db.session.add(client)
            db.session.flush()
            svc = DocumentSVC(document_type='svc', doc_no=1, date=date(2026, 10, 1), client_id=client.id, status='Online')
            db.session.add(svc)
            db.session.commit()
            svc_id = svc.id
        printed = self.client.get(f'/documents/print/svc/{svc_id}')
        self.assertIn(b'CUSTOM', printed.data)
        self.assertIn(b'Klien Asli', printed.data)

    def test_broken_template_is_rejected_with_its_line_number(self):
        response = self._stage('prj', '<html>\n<body>\n{% for item in %}\n</body></html>')
        self.assertEqual(response.status_code, 422)
        self.assertIn('Line 3', response.get_json()['error'])
        with self.app.app_context():
            self.assertEqual(SystemSetting.get_val('print_template_prj'), '')
        self.assertFalse(self.template_dir.exists() and any(self.template_dir.iterdir()))

    def test_non_html_upload_is_rejected(self):
        response = self._stage('ho', 'hello', filename='template.txt')
        self.assertEqual(response.status_code, 400)

    def test_cancelled_upload_is_discarded(self):
        token = self._stage('ho', '<p>{{ ho.display_no }}</p>').get_json()['token']
        staging_file = Path(self.data_root.name) / 'staging' / f'print_template_{token}.html'
        self.assertTrue(staging_file.exists())
        self.client.post('/system/settings/print-template/ho/discard', json={'token': token})
        self.assertFalse(staging_file.exists())
        # A discarded token can no longer be applied.
        applied = self.client.post('/system/settings/print-template/ho/apply', json={'token': token})
        self.assertEqual(applied.status_code, 404)

    def test_reset_to_default_restores_every_setting_and_template(self):
        token = self._stage('xprj', '<p>{{ doc.display_no }}</p>').get_json()['token']
        self.client.post('/system/settings/print-template/xprj/apply', json={'token': token})
        self.client.post('/system/settings/update', data={'svc_format': 'X/{NO}', 'doc_no_padding': '5'})

        self.client.post('/system/settings/reset-defaults')

        with self.app.app_context():
            for key, value, _, _ in DEFAULT_SYSTEM_SETTINGS:
                self.assertEqual(SystemSetting.get_val(key), value, key)
            self.assertEqual(SystemSetting.get_val('print_template_xprj'), '')
        self.assertIn(b'No template yet', self.client.get('/system/settings').data)

    def test_removing_a_template_returns_to_the_starter(self):
        token = self._stage('prj', '<p>{{ doc.title }}</p>').get_json()['token']
        self.client.post('/system/settings/print-template/prj/apply', json={'token': token})
        self.client.post('/system/settings/print-template/prj/reset')
        with self.app.app_context():
            self.assertEqual(SystemSetting.get_val('print_template_prj'), '')
        default = self.client.get('/system/settings/print-template/prj/download')
        self.assertIn('template_prj_starter.html', default.headers['Content-Disposition'])


if __name__ == '__main__':
    unittest.main()
