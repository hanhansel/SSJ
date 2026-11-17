"""Scheduler window: a long block that started weeks before the calendar window stays visible."""

import os
import unittest
from datetime import timedelta
from io import BytesIO

from openpyxl import load_workbook

from run import create_app
from apps.models import ALL_MENUS, ROLE_OFFICE
from apps.models import db, User, Role, ClientSite, DocumentPRJ, ScheduleBlock, wib_now
from apps.scheduling.routes import MAX_BLOCK_DAYS


class ScheduleWindowTests(unittest.TestCase):
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
            self.tech = self._user('Technician', 'andi@example.test')
            client = ClientSite(company_name='PT Proyek', address='Jl. A')
            db.session.add(client)
            db.session.commit()
            prj = DocumentPRJ(doc_no=1, doc_period=str(self.today.year), doc_number_text='1/PRJ',
                              date=self.today, client_id=client.id, title='Proyek Panjang',
                              so_number='SO-1', status='Online')
            db.session.add(prj)
            db.session.commit()
            self.doc_id = prj.id
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

    def _long_block(self):
        # Started 100 days ago, 120 days long: still running inside the window.
        with self.app.app_context():
            db.session.add(ScheduleBlock(user_id=self.tech, document_id=self.doc_id,
                                         start_date=self.today - timedelta(days=100), days_duration=120))
            db.session.commit()

    def test_long_running_block_is_shown_in_the_calendar_window(self):
        self._long_block()
        state = self.client.get(f'/scheduling/api/dashboard_state?start_date={self.today.isoformat()}').get_json()
        self.assertEqual([s['project_id'] for s in state['schedules']], [self.doc_id])

    def test_long_running_block_is_in_the_schedule_export(self):
        self._long_block()
        response = self.client.get(f'/scheduling/api/export_schedule_xlsx?start_date={self.today.isoformat()}')
        sheet = load_workbook(BytesIO(response.data))['Schedule Calendar']
        self.assertTrue(str(sheet.cell(4, 3).value).startswith('← PRJ 1/PRJ'))

    def test_block_longer_than_the_limit_is_rejected(self):
        payload = {'user_id': self.tech, 'doc_type': 'prj', 'doc_id': self.doc_id,
                   'start_date': self.today.isoformat(), 'days_duration': MAX_BLOCK_DAYS + 1}
        response = self.client.post('/scheduling/api/assign_task', json=payload)
        self.assertEqual(response.status_code, 400)
        self.assertIn(f'at most {MAX_BLOCK_DAYS} days', response.get_json()['message'])

        payload['days_duration'] = MAX_BLOCK_DAYS
        self.assertEqual(self.client.post('/scheduling/api/assign_task', json=payload).status_code, 200)


if __name__ == '__main__':
    unittest.main()
