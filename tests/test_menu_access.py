"""Per-menu access: each menu guards its pages, cross-menu actions, the dashboard, and account management."""

import os
import unittest
from datetime import date
from io import BytesIO

import pandas as pd

from run import create_app
from apps.models import (
    db, User, Role, ClientSite, DocumentSVC, MasterProject, ALL_MENUS, ROLE_OFFICE, ROLE_TECHNICIAN,
    MENU_DOCUMENTS, MENU_MASTER_PROJECT, MENU_STORAGE, MENU_SCHEDULING, MENU_SETTINGS, MENU_ACCESS,
)
from apps.services.document_service import DocumentService

# Pages of each menu; every other menu's account must be turned away from them.
MENU_PAGES = {
    MENU_DOCUMENTS: ['/documents/generator', '/documents/list', '/documents/prj', '/documents/ho'],
    MENU_MASTER_PROJECT: ['/documents/master-projects', '/documents/master-projects/completed'],
    MENU_STORAGE: ['/system/clients', '/documents/list?storage=1', '/documents/prj?storage=1',
                       '/documents/ho?storage=1'],
    MENU_SCHEDULING: ['/scheduling/'],
    MENU_SETTINGS: ['/system/settings'],
    MENU_ACCESS: ['/system/accounts'],
}


class MenuAccessTests(unittest.TestCase):
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
            self.boss = self._user('boss', ROLE_OFFICE, ALL_MENUS)
            self.users = {menu: self._user(f'only_{menu}', ROLE_OFFICE, (menu,)) for menu in ALL_MENUS}
            self.tech = self._user('andi', ROLE_TECHNICIAN)
            client = ClientSite(company_name='PT Klien', address='Jl. A', client_type='On-Call')
            db.session.add(client)
            db.session.flush()
            svc = DocumentSVC(doc_no=1, doc_period='2026', doc_number_text='001/10/SVC/26', date=date(2026, 10, 1),
                              client_id=client.id, status='Completed', actual_completion=date(2026, 10, 2))
            db.session.add(svc)
            db.session.commit()
            self.client_id, self.svc_id = client.id, svc.id

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.drop_all()
        for key, value in self.original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _user(self, name, role_code, menus=()):
        role = Role.query.filter_by(code=role_code).one()
        user = User(name=name, email=f'{name}@example.test', password='x', role_id=role.id, menus=menus)
        db.session.add(user)
        db.session.commit()
        return user.id

    def _login(self, user_id):
        with self.client.session_transaction() as session:
            session['user_id'] = user_id

    def _account(self, user_id):
        with self.app.app_context():
            user = db.session.get(User, user_id)
            return user.name, user.email, user.role, user.menus, user.is_active

    # ---------- pages ----------
    def test_each_menu_opens_only_its_own_pages(self):
        for menu, user_id in self.users.items():
            self._login(user_id)
            for page_menu, pages in MENU_PAGES.items():
                for page in pages:
                    response = self.client.get(page)
                    if page_menu == menu:
                        self.assertEqual(response.status_code, 200, (menu, page))
                    else:
                        self.assertEqual(response.status_code, 302, (menu, page))
                        self.assertTrue(response.headers['Location'].endswith('/system/dashboard'), (menu, page))

    def test_technician_and_office_accounts_keep_to_their_own_app(self):
        self._login(self.tech)
        for page in ('/system/dashboard', '/documents/list', '/system/accounts'):
            self.assertTrue(self.client.get(page).headers['Location'].endswith('/technician/dashboard'), page)
        self._login(self.boss)
        self.assertTrue(self.client.get('/technician/dashboard').headers['Location'].endswith('/system/dashboard'))

    # ---------- actions used across menus ----------
    def _user_in_app(self, name, menus):
        with self.app.app_context():
            return self._user(name, ROLE_OFFICE, menus)

    def _active_svc(self):
        with self.app.app_context():
            svc = DocumentSVC(doc_no=2, doc_period='2026', doc_number_text='002/10/SVC/26', date=date(2026, 10, 3),
                              client_id=self.client_id, status='Online')
            db.session.add(svc)
            db.session.commit()
            return svc.id

    def test_adding_a_client_from_a_document_needs_dokumen_and_penyimpanan(self):
        both = self._user_in_app('dua_menu', (MENU_DOCUMENTS, MENU_STORAGE))
        quick_add = {'company_name': 'PT Baru', 'doc_type': 'prj'}
        for user_id, allowed in ((self.users[MENU_DOCUMENTS], False), (self.users[MENU_STORAGE], False),
                                 (both, True)):
            self._login(user_id)
            self.assertEqual(self.client.post('/system/api/clients/quick-add', json=quick_add).status_code,
                             200 if allowed else 302, user_id)
            if user_id == self.users[MENU_STORAGE]:
                continue  # no generator at all without Documents
            generator = self.client.get('/documents/generator').get_data(as_text=True)
            self.assertEqual('id="modalQuickAddClient"' in generator, allowed, user_id)
            self.assertEqual('data-bs-target="#modalQuickAddClient"' in generator, allowed, user_id)
            self.assertNotIn('New clients are added in Client Data', generator, user_id)
        with self.app.app_context():
            self.assertEqual(ClientSite.query.filter_by(company_name='PT Baru').count(), 1)

    def test_penyimpanan_alone_opens_only_finished_documents(self):
        active_id = self._active_svc()
        cases = ((MENU_DOCUMENTS, True, True), (MENU_STORAGE, True, False), (MENU_SCHEDULING, False, False))
        for menu, finished_ok, active_ok in cases:
            self._login(self.users[menu])
            for doc_id, allowed in ((self.svc_id, finished_ok), (active_id, active_ok)):
                for page in (f'/documents/view/svc/{doc_id}', f'/documents/print/svc/{doc_id}',
                             f'/technician/task/svc/{doc_id}'):
                    response = self.client.get(page)
                    if allowed:
                        self.assertEqual(response.status_code, 200, (menu, page))
                    else:
                        self.assertEqual(response.status_code, 302, (menu, page))
                        self.assertNotIn('/view/', response.headers['Location'], (menu, page))

        self._login(self.users[MENU_DOCUMENTS])
        self.assertEqual(self.client.get('/system/api/preview-doc-no?doc_type=svc').status_code, 200)
        self._login(self.users[MENU_SETTINGS])
        self.assertEqual(self.client.get('/system/api/preview-doc-no?doc_type=svc').status_code, 302)

    def test_document_page_has_no_schedule_button(self):
        # Scheduling happens only in Scheduling, even for accounts with both menus.
        active_id = self._active_svc()
        both = self._user_in_app('dok_jadwal', (MENU_DOCUMENTS, MENU_SCHEDULING))
        for user_id in (self.users[MENU_DOCUMENTS], both):
            self._login(user_id)
            page = self.client.get(f'/documents/view/svc/{active_id}').get_data(as_text=True)
            self.assertNotIn('Open Scheduling Calendar', page)

    def test_acc_never_closes_the_master_project(self):
        with self.app.app_context():
            project = MasterProject(name='Gedung A', client_id=self.client_id, status='Online')
            db.session.add(project)
            db.session.flush()
            prj_ids = []
            for number in (1, 2):
                ok, prj, error = DocumentService.create_prj(
                    self.client_id, f'PRJ {number}', f'SO-{number}', date(2026, 10, 1), 2, project.id, str(number),
                    '1', '', [{'description': 'Kabel', 'qty': 1, 'uom': 'Unit', 'display_order': 1}], [],
                )
                self.assertTrue(ok, error)
                prj_ids.append(prj.id)
            db.session.commit()
            project_id = project.id

        both = self._user_in_app('dok_mp', (MENU_DOCUMENTS, MENU_MASTER_PROJECT))
        self._login(both)
        # Even with both menus, ACC of the last PRJ leaves the project open.
        self.client.post(f'/documents/review/prj/{prj_ids[0]}', data={'status': 'Void'})
        self.client.post(f'/documents/review/prj/{prj_ids[1]}', data={'status': 'Void', 'complete_master_project': '1'})
        with self.app.app_context():
            self.assertEqual(db.session.get(MasterProject, project_id).status, 'Online')
        # The project is closed on its own page, without a "ready to close" badge.
        self.assertNotIn('ready to close', self.client.get('/documents/master-projects').get_data(as_text=True))

    # ---------- dashboard and sidebar ----------
    def test_dashboard_and_sidebar_show_only_permitted_parts(self):
        self._login(self.users[MENU_DOCUMENTS])
        page = self.client.get('/system/dashboard').get_data(as_text=True)
        self.assertIn('Create New Document', page)
        self.assertIn('Active SVC', page)
        self.assertNotIn('Not Scheduled', page)
        self.assertNotIn('Data Storage', page)
        self.assertNotIn('Access Management', page)

        self._login(self.users[MENU_SCHEDULING])
        page = self.client.get('/system/dashboard').get_data(as_text=True)
        self.assertIn('Not Scheduled', page)
        self.assertNotIn('Create New Document', page)

        self._login(self.users[MENU_SETTINGS])
        page = self.client.get('/system/dashboard').get_data(as_text=True)
        self.assertIn('does not have access to either menu', page)
        self.assertIn('href="/system/settings"', page)
        self.assertNotIn('href="/scheduling/"', page)

        self._login(self.boss)
        page = self.client.get('/system/dashboard').get_data(as_text=True)
        for label in ('Not Scheduled', 'Active SVC', 'Data Storage', 'Master Project', 'Access Management'):
            self.assertIn(label, page)

    # ---------- account management ----------
    def test_create_superadmin_admin_and_technician(self):
        self._login(self.boss)
        for name, level, menus in (
            ('citra', 'admin', [MENU_DOCUMENTS, MENU_STORAGE, 'not_a_menu']),
            ('dodi', 'technician', [MENU_ACCESS]),
            ('eka', 'admin', []),
            ('fani', 'superadmin', [MENU_DOCUMENTS]),
            ('gani', 'owner', [MENU_DOCUMENTS]),
        ):
            self.client.post('/system/accounts/create', data={
                'name': name, 'email': f'{name}@example.test', 'password': 'secret123',
                'access_level': level, 'menus': menus,
            })
        with self.app.app_context():
            accounts = {u.name: (u.role, u.menus, u.access_label)
                        for u in User.query.filter(User.name.in_(['citra', 'dodi', 'eka', 'fani', 'gani']))}
        self.assertEqual(accounts, {
            'citra': (ROLE_OFFICE, [MENU_DOCUMENTS, MENU_STORAGE], 'Admin'),
            'dodi': (ROLE_TECHNICIAN, [], ROLE_TECHNICIAN),  # a technician never gets menus
            'fani': (ROLE_OFFICE, list(ALL_MENUS), 'Superadmin'),  # every menu, whatever was ticked
        })  # eka: an Admin needs at least one menu; gani: unknown level

    def test_edit_switches_between_superadmin_and_admin(self):
        self._login(self.boss)
        user_id = self.users[MENU_DOCUMENTS]
        identity = {'name': f'only_{MENU_DOCUMENTS}', 'email': f'only_{MENU_DOCUMENTS}@example.test'}
        self.client.post(f'/system/accounts/edit/{user_id}', data={**identity, 'access_level': 'superadmin'})
        self.assertEqual(self._account(user_id)[3], list(ALL_MENUS))
        self.client.post(f'/system/accounts/edit/{user_id}', data={**identity, 'access_level': 'admin',
                                                                  'menus': [MENU_SCHEDULING]})
        self.assertEqual(self._account(user_id)[3], [MENU_SCHEDULING])
        # A technician stays one, whatever the form says.
        self.client.post(f'/system/accounts/edit/{self.tech}', data={
            'name': 'andi', 'email': 'andi@example.test', 'access_level': 'superadmin'})
        self.assertEqual(self._account(self.tech)[2:4], (ROLE_TECHNICIAN, []))

    def test_accounts_page_explains_menus_and_cross_menu_rules(self):
        self._login(self.boss)
        page = self.client.get('/system/accounts').get_data(as_text=True)
        self.assertIn('Superadmin', page)
        self.assertIn('Client Data and the archive of completed documents', page)
        self.assertIn('Add new clients while creating or importing documents', page)
        self.assertIn('Open documents that are still active', page)

    def test_edit_changes_name_email_and_menus_but_never_the_type(self):
        self._login(self.boss)
        user_id = self.users[MENU_DOCUMENTS]
        self.client.post(f'/system/accounts/edit/{user_id}', data={
            'name': 'Doni', 'email': 'DONI@Example.test', 'account_type': ROLE_TECHNICIAN,
            'menus': [MENU_SCHEDULING, MENU_SETTINGS],
        })
        self.assertEqual(self._account(user_id)[:4],
                         ('Doni', 'doni@example.test', ROLE_OFFICE, [MENU_SCHEDULING, MENU_SETTINGS]))

        self.client.post(f'/system/accounts/edit/{self.tech}', data={'name': 'Andi S', 'email': 'andi@example.test',
                                                                    'menus': [MENU_ACCESS]})
        self.assertEqual(self._account(self.tech)[2:4], (ROLE_TECHNICIAN, []))

        # Duplicate email or name is refused.
        self.client.post(f'/system/accounts/edit/{self.tech}', data={'name': 'andi s', 'email': 'boss@example.test'})
        self.client.post(f'/system/accounts/edit/{self.tech}', data={'name': 'BOSS', 'email': 'andi@example.test'})
        self.assertEqual(self._account(self.tech)[:2], ('Andi S', 'andi@example.test'))

        # Without Access Management nobody can edit accounts.
        self._login(self.users[MENU_DOCUMENTS])
        self.client.post(f'/system/accounts/edit/{self.tech}', data={'name': 'hacked', 'email': 'andi@example.test'})
        self.assertEqual(self._account(self.tech)[0], 'Andi S')

    def test_someone_always_keeps_manajemen_akses(self):
        akses_user = self.users[MENU_ACCESS]
        self._login(self.boss)
        # Two managers: boss may deactivate the other one...
        self.client.post(f'/system/accounts/toggle/{akses_user}')
        self.assertFalse(self._account(akses_user)[4])
        # ...but then cannot drop their own Access Management: nobody would be left.
        self.client.post(f'/system/accounts/edit/{self.boss}', data={'name': 'boss', 'email': 'boss@example.test',
                                                                    'menus': [MENU_DOCUMENTS]})
        self.assertIn(MENU_ACCESS, self._account(self.boss)[3])

        # With the other manager active again, the last-manager rule also blocks deactivating them.
        self.client.post(f'/system/accounts/toggle/{akses_user}')
        self.client.post(f'/system/accounts/edit/{self.boss}', data={'name': 'boss', 'email': 'boss@example.test',
                                                                    'menus': [MENU_DOCUMENTS]})
        self.assertEqual(self._account(self.boss)[3], [MENU_DOCUMENTS])
        self._login(akses_user)
        self.assertEqual(self.client.get('/system/accounts').status_code, 200)
        self.client.post(f'/system/accounts/edit/{akses_user}', data={'name': 'only_akses',
                                                                     'email': 'only_akses@example.test',
                                                                     'menus': [MENU_SETTINGS]})
        self.assertEqual(self._account(akses_user)[3], [MENU_ACCESS])

    def test_import_reads_account_type_and_menu_columns(self):
        self._login(self.boss)
        template = self.client.get('/system/accounts/template')
        headers = list(pd.read_excel(BytesIO(template.data)).columns)
        self.assertEqual(headers, ['Name', 'Email', 'Password', 'Account Type', 'Documents', 'Master Project',
                                   'Data Storage', 'Scheduling', 'Settings', 'Access Management'])

        rows = [
            ['fajar', 'fajar@example.test', 'rahasia123', 'Admin', 'Y', 'N', 'y', 'N', 'N', 'N'],
            ['gita', 'gita@example.test', 'rahasia123', 'Technician', 'Y', 'Y', 'Y', 'Y', 'Y', 'Y'],
            ['hadi', 'hadi@example.test', 'rahasia123', 'Office', 'N', 'N', 'N', 'N', 'N', 'N'],
            ['indah', 'indah@example.test', 'rahasia123', 'Super Admin', 'N', 'N', 'N', 'N', 'N', 'N'],
        ]
        buffer = BytesIO()
        pd.DataFrame(rows, columns=headers).to_excel(buffer, index=False)
        buffer.seek(0)
        response = self.client.post('/system/accounts/import', data={'file': (buffer, 'akun.xlsx')},
                                    content_type='multipart/form-data', follow_redirects=True)
        self.assertIn('1 Admin account(s) skipped because no menu is marked Y', response.get_data(as_text=True))
        with self.app.app_context():
            imported = {u.name: (u.role, u.menus)
                        for u in User.query.filter(User.name.in_(['fajar', 'gita', 'hadi', 'indah']))}
        self.assertEqual(imported, {
            'fajar': (ROLE_OFFICE, [MENU_DOCUMENTS, MENU_STORAGE]),
            'gita': (ROLE_TECHNICIAN, []),
            'indah': (ROLE_OFFICE, list(ALL_MENUS)),
        })

    def test_account_type_from_an_old_database_cannot_log_in(self):
        from werkzeug.security import generate_password_hash
        with self.app.app_context():
            legacy = Role(code='Superadmin', name='Superadmin')
            db.session.add(legacy)
            db.session.flush()
            db.session.add(User(name='lama', email='lama@example.test', password=generate_password_hash('rahasia123'),
                                role_id=legacy.id))
            db.session.commit()
        response = self.client.post('/login', data={'username': 'lama', 'password': 'rahasia123'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('This account type is no longer recognised', response.get_data(as_text=True))
        with self.client.session_transaction() as session:
            self.assertNotIn('user_id', session)

    def test_first_setup_account_gets_every_menu(self):
        with self.app.app_context():
            for user in User.query.all():
                db.session.delete(user)  # one by one, so their menu rows go with them
            db.session.commit()
        self.client.post('/first-setup', data={'username': 'pertama', 'email': 'pertama@ssj', 'password': 'rahasia123'})
        with self.app.app_context():
            first = User.query.one()
            self.assertEqual((first.role, first.menus), (ROLE_OFFICE, list(ALL_MENUS)))


if __name__ == '__main__':
    unittest.main()
