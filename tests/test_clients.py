"""One client table for every document type: quick add, pickers, XPRJ clients and the client import."""

import os
import unittest
from datetime import date
from io import BytesIO

import pandas as pd

from run import create_app
from apps.models import ALL_MENUS, ROLE_OFFICE
from apps.models import db, User, Role, ClientSite
from apps.services.document_service import DocumentService


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.original_env = {key: os.environ.get(key) for key in ('DATABASE_URL', 'APP_ENV')}
        os.environ['DATABASE_URL'] = 'sqlite:///:memory:'
        os.environ['APP_ENV'] = 'testing'
        self.app = create_app()
        if not self.app.config['SQLALCHEMY_DATABASE_URI'].startswith('sqlite'):
            raise RuntimeError('Test harus memakai SQLite, bukan database server (ssj_db).')
        self.app.config['TESTING'] = True
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.app.test_client()
        with self.app.app_context():
            db.create_all()
            role = Role.query.filter_by(code='Office').one()
            admin = User(name='Admin', email='admin@example.test', password='x', role_id=role.id, menus=ALL_MENUS)
            service_client = ClientSite(company_name='Klien Servis Satu', client_type='On-Call')
            project_client = ClientSite(company_name='Klien Proyek Satu', address='Jl. Proyek')
            db.session.add_all([admin, service_client, project_client])
            db.session.commit()
            self.service_client_id, self.project_client_id = service_client.id, project_client.id
            admin_id = admin.id
        with self.client.session_transaction() as session:
            session['user_id'] = admin_id

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.drop_all()
        for key, value in self.original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_quick_add_has_an_svc_and_an_prj_version(self):
        # SVC version: the type is mandatory.
        quick = self.client.post('/system/api/clients/quick-add', json={'company_name': 'Cepat', 'doc_type': 'svc'})
        self.assertEqual(quick.status_code, 400)
        quick = self.client.post('/system/api/clients/quick-add',
                                 json={'company_name': 'Cepat', 'doc_type': 'svc', 'client_type': 'Survey'})
        self.assertEqual(quick.get_json()['client']['client_type'], 'Survey')
        # PRJ / XPRJ version: no type is stored.
        for doc_type in ('prj', 'xprj'):
            quick = self.client.post('/system/api/clients/quick-add',
                                     json={'company_name': f'Proyek {doc_type}', 'doc_type': doc_type,
                                           'client_type': 'Survey'})
            self.assertEqual(quick.get_json()['client']['client_type'], '')

        generator = self.client.get('/documents/generator').data.decode()
        self.assertIn('id="quickClientTypeWrapper"', generator)

    def test_every_picker_lists_all_clients(self):
        generator = self.client.get('/documents/generator').data.decode()
        projects = self.client.get('/documents/master-projects').data.decode()
        for page in (generator, projects):
            self.assertIn('Klien Servis Satu', page)
            self.assertIn('Klien Proyek Satu', page)

    def test_xprj_takes_the_client_of_its_parent_prj(self):
        with self.app.app_context():
            ok, prj, err = DocumentService.create_prj(
                client_id=self.project_client_id, title='Pasang', so_number='SO-1', doc_date=date(2026, 10, 1),
                estimated_days=2, master_project_id=None, custom_no='', urgency_level='1', notes='',
                items_data=[{'description': 'Pasang CCTV', 'qty': 1, 'uom': 'Unit', 'display_order': 1}],
                tasks_data=[],
            )
            self.assertTrue(ok, err)
            ok, xprj, err = DocumentService.create_xprj(
                client_id=self.service_client_id, doc_date=date(2026, 10, 2), custom_no='',
                customer_complaint='Kamera mati', task_description='Ganti kamera', estimated_days=1,
                prj_id_val=str(prj.id), reference='', site_address='', notes='',
            )
            self.assertTrue(ok, err)
            self.assertEqual((xprj.client_id, xprj.site_address), (self.project_client_id, 'Jl. Proyek'))
            xprj_id = xprj.id
        # An XPRJ prints as an XPRJ (no template uploaded: the XPRJ variable list).
        printout = self.client.get(f'/documents/print/xprj/{xprj_id}').data.decode()
        self.assertIn('No print template for XPRJ documents', printout)

    def test_client_import_cleans_excel_line_breaks(self):
        output = BytesIO()
        pd.DataFrame([[1, 'PT Bersih', 'Manhattan 1 no 51_x000D_', 'Jakarta Utara', None,
                       'Pak Irvan', '0812', 'On-Call']],
                     columns=['No', 'Company Name', 'Company Address 1', 'Company Address 2',
                              'Company Address 3', 'PIC', 'PIC Phone', 'Customer Type']).to_excel(output, index=False)
        output.seek(0)
        self.client.post('/system/clients/import', data={'file': (output, 'klien.xlsx')},
                         content_type='multipart/form-data')
        with self.app.app_context():
            client = ClientSite.query.filter_by(company_name='PT Bersih').one()
            self.assertEqual(client.address, 'Manhattan 1 no 51, Jakarta Utara')
            self.assertEqual(client.client_type, 'On-Call')


if __name__ == '__main__':
    unittest.main()
