"""Captain: one display-only team lead per document, shown in the UI and exports."""

import os
import unittest
from io import BytesIO

from openpyxl import load_workbook

from run import create_app
from apps.models import ALL_MENUS, ROLE_OFFICE
from apps.models import db, User, Role, ClientSite, Document, DocumentSVC, ScheduleBlock, wib_now


class CaptainTests(unittest.TestCase):
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
        self.today = wib_now().date()
        with self.app.app_context():
            db.create_all()
            admin_id = self._user('Office', 'admin@example.test')
            self.andi = self._user('Technician', 'andi@example.test')
            self.budi = self._user('Technician', 'budi@example.test')
            client = ClientSite(company_name='PT Klien', address='Jl. A')
            db.session.add(client)
            db.session.commit()
            svc = DocumentSVC(doc_no=1, doc_period=str(self.today.year), doc_number_text='1/SVC',
                              date=self.today, client_id=client.id, status='Online')
            db.session.add(svc)
            db.session.commit()
            self.doc_id = svc.id
            self.andi_block = self._block(self.andi)
            self.budi_block = self._block(self.budi)
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

    def _user(self, role_code, email):
        role = Role.query.filter_by(code=role_code).one()
        user = User(name=email.split('@')[0], email=email, password='x', role_id=role.id, menus=ALL_MENUS if role_code == ROLE_OFFICE else ())
        db.session.add(user)
        db.session.commit()
        return user.id

    def _block(self, user_id):
        block = ScheduleBlock(user_id=user_id, document_id=self.doc_id, start_date=self.today, days_duration=2)
        db.session.add(block)
        db.session.commit()
        return block.id

    def _edit(self, block_id, **extra):
        response = self.client.post('/scheduling/api/update_task', json={'block_id': block_id, 'days_duration': 2, **extra})
        self.assertEqual(response.status_code, 200, response.get_json())

    def _doc(self):
        return db.session.get(Document, self.doc_id)

    def test_captain_is_listed_first_and_flagged_on_the_scheduler_block(self):
        self._edit(self.budi_block, is_captain=True)
        with self.app.app_context():
            self.assertEqual(self._doc().assigned_technicians, 'budi (Captain), andi')
        state = self.client.get(f'/scheduling/api/dashboard_state?start_date={self.today.isoformat()}').get_json()
        flags = {s['user_id']: s['is_captain'] for s in state['schedules']}
        self.assertEqual(flags, {self.andi: False, self.budi: True})

    def test_new_captain_replaces_old_and_unchecking_clears_it(self):
        self._edit(self.andi_block, is_captain=True)
        self._edit(self.budi_block, is_captain=True)
        with self.app.app_context():
            self.assertEqual(self._doc().captain_id, self.budi)
        # Unchecking someone who is not the captain changes nothing.
        self._edit(self.andi_block, is_captain=False)
        with self.app.app_context():
            self.assertEqual(self._doc().captain_id, self.budi)
        self._edit(self.budi_block, is_captain=False)
        with self.app.app_context():
            self.assertIsNone(self._doc().captain_id)
            self.assertEqual(self._doc().assigned_technicians, 'andi, budi')

    def test_resize_keeps_captain_and_removing_last_block_clears_it(self):
        self._edit(self.andi_block, is_captain=True)
        self._edit(self.andi_block)  # drag-resize sends no is_captain
        with self.app.app_context():
            self.assertEqual(self._doc().captain_id, self.andi)
        self.client.post('/scheduling/api/remove_task', json={'block_id': self.andi_block})
        with self.app.app_context():
            self.assertIsNone(self._doc().captain_id)

    def test_schedule_export_marks_the_captain_block(self):
        self._edit(self.budi_block, is_captain=True)
        response = self.client.get(f'/scheduling/api/export_schedule_xlsx?start_date={self.today.isoformat()}')
        sheet = load_workbook(BytesIO(response.data))['Schedule Calendar']
        rows = {row[0].value: row[2].value for row in sheet.iter_rows(min_row=4) if row[0].value}
        self.assertTrue(rows['budi'].endswith('\nCAPTAIN'))
        self.assertNotIn('CAPTAIN', rows['andi'])


if __name__ == '__main__':
    unittest.main()
