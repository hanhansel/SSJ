import unittest
from io import BytesIO
import os
from pathlib import Path
import re
import tempfile
from datetime import date, timedelta
from run import create_app
from apps.models import ALL_MENUS, ROLE_OFFICE
from apps.models import db, User, Role, SystemSetting, ClientSite, MasterProject, DocumentPRJ, DocumentSVC, DocumentXPRJ, PRJItem, ScheduleBlock, HO, HOPhoto, WorkReport


class BasicAppTests(unittest.TestCase):
    def setUp(self):
        # Tests must never inherit the PostgreSQL URL from a developer/server
        # .env file.  Configure the disposable database before app creation.
        self.original_database_url = os.environ.get('DATABASE_URL')
        self.original_app_env = os.environ.get('APP_ENV')
        os.environ['DATABASE_URL'] = 'sqlite:///:memory:'
        os.environ['APP_ENV'] = 'testing'
        self.app = create_app()
        # Hard guard: tearDown drops every table, so never run against a real server DB.
        if not self.app.config['SQLALCHEMY_DATABASE_URI'].startswith('sqlite'):
            raise RuntimeError('Test harus memakai SQLite, bukan database server (ssj_db).')
        self.app.config['TESTING'] = True
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.print_template_temp = tempfile.TemporaryDirectory()
        self.app.config['PRINT_TEMPLATE_FOLDER'] = self.print_template_temp.name
        self.client = self.app.test_client()

        with self.app.app_context():
            db.create_all()

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.drop_all()
        if self.original_database_url is None:
            os.environ.pop('DATABASE_URL', None)
        else:
            os.environ['DATABASE_URL'] = self.original_database_url
        if self.original_app_env is None:
            os.environ.pop('APP_ENV', None)
        else:
            os.environ['APP_ENV'] = self.original_app_env
        self.print_template_temp.cleanup()

    def test_app_initialization(self):
        self.assertIsNotNone(self.app)

    def test_first_setup_redirect_when_no_user(self):
        response = self.client.get('/', follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertIn('/first-setup', response.headers.get('Location', ''))

    def test_roles_seeded(self):
        with self.app.app_context():
            roles = [r.code for r in Role.query.all()]
            self.assertIn('Office', roles)
            self.assertNotIn('Admin', roles)
            self.assertIn('Technician', roles)
            self.assertNotIn('Finance', roles)

    def test_settings_seeded(self):
        with self.app.app_context():
            svc_fmt = SystemSetting.get_val('svc_format')
            self.assertTrue(bool(svc_fmt))

    def test_print_templates_are_uploaded_and_selected_per_document_type(self):
        with self.app.app_context():
            admin_role = Role.query.filter_by(code='Office').one()
            admin = User(name='Template Admin', email='template@example.test', password='not-used', role_id=admin_role.id, menus=ALL_MENUS)
            client = ClientSite(company_name='Template Client', address='Template Address')
            db.session.add_all([admin, client])
            db.session.flush()
            documents = [
                DocumentPRJ(doc_no=1, date=date(2026, 9, 17), client_id=client.id,
                            title='PRJ Template Test', so_number='SO-TEMPLATE-1'),
                DocumentSVC(doc_no=1, date=date(2026, 9, 17), client_id=client.id),
                DocumentXPRJ(doc_no=1, date=date(2026, 9, 17), client_id=client.id),
            ]
            db.session.add_all(documents)
            db.session.commit()
            admin_id = admin.id
            document_ids = {document.document_type: document.id for document in documents}

        with self.client.session_transaction() as session:
            session['user_id'] = admin_id

        settings_page = self.client.get('/system/settings')
        self.assertEqual(settings_page.status_code, 200)
        for doc_type in ('prj', 'svc', 'xprj'):
            self.assertIn(f'tplFile-{doc_type}'.encode(), settings_page.data)
        self.assertNotIn(b'tplFile-svc_xprj', settings_page.data)

        # Each type is uploaded, previewed with test data, then confirmed separately.
        for doc_type, source, filename in [
            ('prj', b'<p>ACTIVE-PRJ {{ doc_type }}</p>', 'company prj.html'),
            ('svc', b'<p>ACTIVE-SVC {{ doc_type }}</p>', 'company svc.html'),
            ('xprj', b'<p>ACTIVE-XPRJ {{ doc_type }}</p>', 'company xprj.html'),
        ]:
            staged = self.client.post(f'/system/settings/print-template/{doc_type}/stage', data={
                'template': (BytesIO(source), filename),
            }, content_type='multipart/form-data')
            self.assertEqual(staged.status_code, 200)
            applied = self.client.post(f'/system/settings/print-template/{doc_type}/apply',
                                       json={'token': staged.get_json()['token']})
            self.assertTrue(applied.get_json()['ok'])

        expected_patterns = {
            'prj': r'^prj_\d{8}_\d{6}_[0-9a-f]{8}_company_prj\.html$',
            'svc': r'^svc_\d{8}_\d{6}_[0-9a-f]{8}_company_svc\.html$',
            'xprj': r'^xprj_\d{8}_\d{6}_[0-9a-f]{8}_company_xprj\.html$',
        }
        with self.app.app_context():
            for doc_type, pattern in expected_patterns.items():
                filename = SystemSetting.get_val(f'print_template_{doc_type}')
                self.assertTrue(re.fullmatch(pattern, filename), filename)
                self.assertTrue((Path(self.print_template_temp.name) / filename).is_file())

        for doc_type, marker in [('prj', b'ACTIVE-PRJ'), ('svc', b'ACTIVE-SVC'), ('xprj', b'ACTIVE-XPRJ')]:
            response = self.client.get(f'/documents/print/{doc_type}/{document_ids[doc_type]}')
            self.assertEqual(response.status_code, 200)
            self.assertIn(marker, response.data)

    def test_legacy_combined_print_template_is_only_a_missing_setting_fallback(self):
        from apps.services.print_template_service import active_template_name as _active_print_template

        with self.app.app_context():
            SystemSetting.set_val('print_template_svc_xprj', 'legacy_combined.html')
            self.assertEqual(_active_print_template('svc'), 'legacy_combined.html')
            self.assertEqual(_active_print_template('xprj'), 'legacy_combined.html')

            # An explicit reset creates the new setting with an empty value and
            # must not reactivate the legacy template for that document type.
            SystemSetting.set_val('print_template_svc', '')
            self.assertEqual(_active_print_template('svc'), '')
            self.assertEqual(_active_print_template('xprj'), 'legacy_combined.html')

    def test_attendance_and_finance_are_gone(self):
        with self.app.app_context():
            technician_role = Role.query.filter_by(code='Technician').one()
            technician = User(name='Test Technician', email='technician@example.test',
                              password='not-used-by-this-test', role_id=technician_role.id)
            db.session.add(technician)
            db.session.commit()
            technician_id = technician.id

        with self.client.session_transaction() as session:
            session['user_id'] = technician_id

        # Three tabs on the phone: Calendar, Documents, Settings.
        home = self.client.get('/technician/dashboard')
        self.assertEqual(home.status_code, 200)
        self.assertEqual(home.data.count(b'class="tech-nav-item'), 3)
        self.assertNotIn(b'Absensi', home.data)
        for url in ('/technician/attendance', '/technician/attendance/history',
                    '/system/finance/attendance', '/system/finance/export-attendance'):
            self.assertEqual(self.client.get(url).status_code, 404, url)

    def test_prj_can_be_standalone_or_grouped_under_master_project(self):
        with self.app.app_context():
            admin_role = Role.query.filter_by(code='Office').one()
            admin = User(name='Admin Test', email='admin@example.test', password='not-used', role_id=admin_role.id, menus=ALL_MENUS)
            second_admin = User(name='Admin Dua', email='admin2@example.test', password='not-used', role_id=admin_role.id, menus=ALL_MENUS)
            client = ClientSite(company_name='Site Test', address='Alamat Test')
            db.session.add(client)
            db.session.flush()
            master = MasterProject(name='Project Induk', client_id=client.id, status='Online')
            db.session.add_all([admin, second_admin, master])
            db.session.flush()
            grouped = DocumentPRJ(doc_no=1, date=date(2026, 1, 1), client_id=client.id,
                                  title='Pekerjaan Tergrup', so_number='SO-TEST-001',
                                  site_address=client.address, master_project_id=master.id)
            standalone = DocumentPRJ(doc_no=2, date=date(2026, 1, 2), client_id=client.id,
                                     title='Pekerjaan Mandiri', so_number='SO-TEST-002',
                                     site_address=client.address)
            grouped.items.append(PRJItem(description='Instalasi perangkat', qty=2, uom='Titik', display_order=1))
            db.session.add_all([grouped, standalone])
            db.session.flush()
            db.session.add(ScheduleBlock(user_id=admin.id, document_id=grouped.id,
                                         start_date=date(2026, 1, 4), days_duration=4))
            # A second technician overlaps for three days and continues two days
            # longer, so the document covers Jan 4 through Jan 10: seven unique days.
            db.session.add(ScheduleBlock(user_id=second_admin.id, document_id=grouped.id,
                                         start_date=date(2026, 1, 6), days_duration=5))
            db.session.commit()
            admin_id = admin.id
            grouped_id = grouped.id
            self.assertEqual(grouped.scheduled_target_days, 7)

        with self.client.session_transaction() as session:
            session['user_id'] = admin_id

        listing = self.client.get('/documents/prj')
        self.assertEqual(listing.status_code, 200)
        self.assertIn(b'Project Induk', listing.data)
        detail = self.client.get(f'/documents/view/prj/{grouped_id}')
        self.assertEqual(detail.status_code, 200)
        self.assertIn(b'SO-TEST-001', detail.data)
        self.assertNotIn(b'PRJ DOCUMENT', detail.data)
        self.assertIn(b'table-info-aligned', detail.data)
        self.assertIn(b'info-colon', detail.data)
        self.assertIn(b'Estimated Work Time', detail.data)

        scheduling = self.client.get('/scheduling/')
        self.assertEqual(scheduling.status_code, 200)
        self.assertIn(b'return { unscheduled: false, inProgress: false };', scheduling.data)
        self.assertIn(b'mpStates[projectName] === true', scheduling.data)
        self.assertNotIn(b'_prevInProgressCount', scheduling.data)

    def test_prj_edit_flow_and_no_delete(self):
        with self.app.app_context():
            admin_role = Role.query.filter_by(code='Office').one()
            admin = User(name='Admin Test 3', email='admin3@example.test', password='not-used', role_id=admin_role.id, menus=ALL_MENUS)
            client = ClientSite(company_name='Client PT XYZ', address='Jl. Sudirman')
            db.session.add_all([admin, client])
            db.session.flush()
            prj = DocumentPRJ(doc_no=10, date=date(2026, 1, 1), client_id=client.id,
                              title='Sebelum Edit', so_number='SO-EDIT-001',
                              site_address=client.address, status='Online')
            prj.items.append(PRJItem(description='Item A', qty=1, uom='Unit'))
            db.session.add(prj)
            db.session.commit()
            admin_id = admin.id
            prj_id = prj.id
            client_id = client.id

        with self.client.session_transaction() as session:
            session['user_id'] = admin_id

        # Verify View mode has no editable document-date field.
        view_resp = self.client.get(f'/documents/view/prj/{prj_id}')
        self.assertEqual(view_resp.status_code, 200)
        self.assertNotIn(b'name="doc_date"', view_resp.data)

        # Verify Edit mode renders the dedicated Internal Notes field.
        edit_view = self.client.get(f'/documents/view/prj/{prj_id}?edit=1')
        self.assertEqual(edit_view.status_code, 200)
        self.assertIn(b'name="notes"', edit_view.data)
        self.assertIn(b'Internal Notes', edit_view.data)

        # 1. Edit PRJ via POST
        edit_resp = self.client.post(f'/documents/prj/{prj_id}/edit', data={
            'title': 'Sesudah Edit Berhasil',
            'so_number': 'SO-EDIT-001',
            'client_id': client_id,
            'site_address': 'Jl. Thamrin',
            'urgency_level': '2',
            'estimated_calendar_days': '3',
            'admin_notes': 'Catatan Baru',
            'item_description': ['Item Baru 1', 'Item Baru 2'],
            'item_qty': ['5', '10'],
            'item_uom': ['Pcs', 'Unit'],
            'technician_task_description': ['Tugas Baru Teknisi']
        }, follow_redirects=True)
        self.assertEqual(edit_resp.status_code, 200)

        with self.app.app_context():
            updated = db.session.get(DocumentPRJ, prj_id)
            self.assertEqual(updated.title, 'Sesudah Edit Berhasil')
            self.assertEqual(updated.urgency_level, 2)
            self.assertEqual(len(updated.items), 2)
            self.assertEqual(len(updated.technician_tasks), 1)

        # 2. Test Print PRJ: no template uploaded, so the page lists the variables.
        print_resp = self.client.get(f'/documents/print/prj/{prj_id}')
        self.assertEqual(print_resp.status_code, 200)
        self.assertIn(b'No print template for PRJ documents', print_resp.data)
        self.assertIn(b'doc.so_number', print_resp.data)

        # 3. Documents are never deleted (only Completed or Void), so there is no delete route.
        del_resp = self.client.post(f'/documents/prj/{prj_id}/delete')
        self.assertEqual(del_resp.status_code, 404)

        with self.app.app_context():
            self.assertIsNotNone(db.session.get(DocumentPRJ, prj_id))

    def test_master_projects_clean_ui_and_prj_terhubung(self):
        with self.app.app_context():
            admin_role = Role.query.filter_by(code='Office').one()
            admin = User(name='Admin Master', email='admin_mp@example.test', password='not-used', role_id=admin_role.id, menus=ALL_MENUS)
            client = ClientSite(company_name='Klien MP Test', address='Jl. Sudirman No 1')
            db.session.add_all([admin, client])
            db.session.flush()

            mp = MasterProject(name='Project MP Alpha', client_id=client.id, site_address=client.address, status='Online')
            db.session.add(mp)
            db.session.flush()

            prj = DocumentPRJ(
                document_type='prj',
                doc_no=1,
                date=date(2026, 3, 1),
                client_id=client.id,
                title='PRJ Terhubung Alpha',
                so_number='SO-MP-001',
                master_project_id=mp.id,
                status='Online'
            )
            db.session.add(prj)
            db.session.commit()
            admin_id = admin.id

        with self.client.session_transaction() as session:
            session['user_id'] = admin_id

        # Test active Master Projects page
        resp = self.client.get('/documents/master-projects')
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(b'apple-segmented-bar', resp.data)
        self.assertIn(b'Master Project', resp.data)
        self.assertIn(b'Linked PRJs (1):', resp.data)
        self.assertIn(b'001/PRJ/26', resp.data)
        self.assertIn(b'apple-prj-highlight-card', resp.data)

        # Test completed Master Projects page
        resp_completed = self.client.get('/documents/master-projects/completed')
        self.assertEqual(resp_completed.status_code, 200)
        self.assertNotIn(b'apple-segmented-bar', resp_completed.data)
        self.assertIn(b'Completed Master Project Data', resp_completed.data)
        self.assertIn(b'apple-prj-highlight-card', resp_completed.data)

    def test_master_project_closes_only_after_every_prj_is_final(self):
        with self.app.app_context():
            admin_role = Role.query.filter_by(code='Office').one()
            admin = User(name='Admin Cascade', email='admin_cascade@example.test', password='not-used', role_id=admin_role.id, menus=ALL_MENUS)
            client = ClientSite(company_name='Klien Cascade', address='Jl. Proyek')
            db.session.add_all([admin, client])
            db.session.flush()

            project = MasterProject(name='MP Cascade', client_id=client.id, status='Online')
            db.session.add(project)
            db.session.flush()
            prj = DocumentPRJ(
                doc_no=21, date=date(2026, 9, 1), client_id=client.id,
                title='PRJ Cascade', so_number='SO-CASCADE-001',
                master_project_id=project.id, status='Online',
            )
            db.session.add(prj)
            db.session.flush()
            xprj = DocumentXPRJ(
                doc_no=8, date=date(2026, 9, 2), client_id=client.id,
                prj_id=prj.id, status='Pending Approval',
            )
            db.session.add(xprj)
            db.session.commit()
            admin_id = admin.id
            project_id = project.id
            prj_id = prj.id
            xprj_id = xprj.id

        with self.client.session_transaction() as session:
            session['user_id'] = admin_id

        page = self.client.get('/documents/master-projects')
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'data-active-prjs=', page.data)
        self.assertIn(b'021/PRJ/26', page.data)

        # Closing is refused while an PRJ is active; nothing is completed automatically.
        rejected = self.client.post(f'/documents/master-projects/{project_id}', data={
            'name': 'MP Cascade', 'notes': '', 'status': 'Completed',
        }, follow_redirects=True)
        self.assertIn(b'cannot be closed yet', rejected.data)
        with self.app.app_context():
            self.assertEqual(db.session.get(MasterProject, project_id).status, 'Online')
            self.assertEqual(db.session.get(DocumentPRJ, prj_id).status, 'Online')

        # Finishing the PRJ on its own page (without ticking the project box) lets the project close.
        self.client.post(f'/documents/review/prj/{prj_id}', data={'status': 'Completed'})
        closed = self.client.post(f'/documents/master-projects/{project_id}', data={
            'name': 'MP Cascade', 'notes': '', 'status': 'Completed',
        }, follow_redirects=True)
        self.assertIn(b'Master Project updated', closed.data)
        with self.app.app_context():
            self.assertEqual(db.session.get(MasterProject, project_id).status, 'Completed')
            completed_prj = db.session.get(DocumentPRJ, prj_id)
            self.assertEqual(completed_prj.status, 'Completed')
            self.assertEqual(completed_prj.ho.status, 'Approved')
            self.assertEqual(completed_prj.ho.approved_by, admin_id)
            # XPRJ follows its own lifecycle and is not touched by its PRJ or the project.
            self.assertEqual(db.session.get(DocumentXPRJ, xprj_id).status, 'Pending Approval')

        completed_mp_page = self.client.get('/documents/master-projects/completed')
        self.assertIn(b'MP Cascade', completed_mp_page.data)
        completed_prj_page = self.client.get('/documents/prj?storage=1')
        self.assertIn(b'PRJ Cascade', completed_prj_page.data)

        # A closed project may be reopened; its final PRJ stays Selesai.
        self.client.post(f'/documents/master-projects/{project_id}', data={
            'name': 'MP Cascade', 'notes': '', 'status': 'Online',
        })
        with self.app.app_context():
            self.assertEqual(db.session.get(MasterProject, project_id).status, 'Online')
            self.assertEqual(db.session.get(DocumentPRJ, prj_id).status, 'Completed')

    def test_svc_xprj_detailed_view_and_completed_documents_no_edit(self):
        with self.app.app_context():
            admin_role = Role.query.filter_by(code='Office').one()
            admin = User(name='Admin Doc Test', email='admin_doc@example.test', password='not-used', role_id=admin_role.id, menus=ALL_MENUS)
            client = ClientSite(company_name='Client Test SVC', address='Jl. Gatot Subroto No 5', pic_name='Budi', pic_phone='08123456789')
            db.session.add_all([admin, client])
            db.session.flush()

            # 1. Active SVC
            active_svc = DocumentSVC(
                document_type='svc',
                doc_no=5,
                date=date(2026, 3, 1),
                client_id=client.id,
                customer_complaint='AC Rusak di lantai 2',
                task_description='Pengecekan freon dan kompresor',
                status='Online',
                estimated_calendar_days=2
            )
            # 2. Completed SVC
            completed_svc = DocumentSVC(
                document_type='svc',
                doc_no=6,
                date=date(2026, 3, 1),
                client_id=client.id,
                customer_complaint='Lampu putus',
                task_description='Ganti bohlam',
                status='Completed',
                estimated_calendar_days=1,
                actual_completion=date(2026, 3, 2)
            )
            # 3. Completed PRJ
            completed_prj = DocumentPRJ(
                document_type='prj',
                doc_no=7,
                date=date(2026, 3, 1),
                client_id=client.id,
                title='PRJ Selesai Test',
                so_number='SO-CMP-001',
                status='Completed',
                site_address=client.address
            )
            db.session.add_all([active_svc, completed_svc, completed_prj])
            db.session.commit()
            admin_id = admin.id
            active_svc_id = active_svc.id
            completed_svc_id = completed_svc.id
            completed_prj_id = completed_prj.id

        with self.client.session_transaction() as session:
            session['user_id'] = admin_id

        # 1. Active SVC Detail View
        resp_active_svc = self.client.get(f'/documents/view/svc/{active_svc_id}')
        self.assertEqual(resp_active_svc.status_code, 200)
        # Header has Back
        self.assertIn(b'Back', resp_active_svc.data)
        # Doc number is present
        self.assertIn(b'005/03/SVC/26', resp_active_svc.data)
        # No SVC DOCUMENT badge in header
        self.assertNotIn(b'document-header-identity', resp_active_svc.data)
        # Has Edit button for active doc
        self.assertIn(b'btnToggleEdit', resp_active_svc.data)
        self.assertIn(b'Change Status:', resp_active_svc.data)
        # Has client select dropdown in edit mode
        self.assertIn(b'name="client_id"', resp_active_svc.data)
        # Document Date, Target Completion, Document Status and Actual Schedule are removed from Operational Information
        self.assertNotIn(b'Document Date', resp_active_svc.data)
        self.assertNotIn(b'Target Completion', resp_active_svc.data)
        self.assertNotIn(b'Document Status', resp_active_svc.data)
        self.assertNotIn(b'Actual Schedule', resp_active_svc.data)

        print_svc = self.client.get(f'/documents/print/svc/{active_svc_id}')
        self.assertEqual(print_svc.status_code, 200)
        self.assertIn(b'No print template for SVC documents', print_svc.data)

        # 1b. Test switching client via edit POST
        with self.app.app_context():
            client2 = ClientSite(company_name='Client Baru 2', address='Jl. Sudirman Baru')
            db.session.add(client2)
            db.session.commit()
            client2_id = client2.id

        post_edit = self.client.post(f'/documents/edit/svc/{active_svc_id}', data={
            'customer_complaint': 'Keluhan Baru Diubah',
            'task_description': 'Tugas Baru Diubah',
            'client_id': str(client2_id),
            'estimated_calendar_days': '3'
        }, follow_redirects=True)
        self.assertEqual(post_edit.status_code, 200)
        self.assertIn(b'Client Baru 2', post_edit.data)

        with self.app.app_context():
            svc_db = db.session.get(DocumentSVC, active_svc_id)
            self.assertEqual(svc_db.client_id, client2_id)

        # 2. Completed SVC Detail View
        resp_completed_svc = self.client.get(f'/documents/view/svc/{completed_svc_id}')
        self.assertEqual(resp_completed_svc.status_code, 200)
        self.assertIn(b'Back', resp_completed_svc.data)
        self.assertIn(b'006/03/SVC/26', resp_completed_svc.data)
        # Back button links to storage=1
        self.assertIn(b'storage=1', resp_completed_svc.data)
        # NO edit button and NO edit form and NO inline status change
        self.assertNotIn(b'id="btnToggleEdit"', resp_completed_svc.data)
        self.assertNotIn(b'id="editModeContainer"', resp_completed_svc.data)
        self.assertNotIn(b'Change Status:', resp_completed_svc.data)

        # 3. Completed PRJ Detail View
        resp_completed_prj = self.client.get(f'/documents/view/prj/{completed_prj_id}')
        self.assertEqual(resp_completed_prj.status_code, 200)
        self.assertIn(b'Back', resp_completed_prj.data)
        self.assertIn(b'007/PRJ/26', resp_completed_prj.data)
        # Back button links to storage=1
        self.assertIn(b'storage=1', resp_completed_prj.data)
        # NO edit button and NO edit form and NO inline status change
        self.assertNotIn(b'id="btnToggleEdit"', resp_completed_prj.data)
        self.assertNotIn(b'id="editModeContainer"', resp_completed_prj.data)
        self.assertNotIn(b'Change Status:', resp_completed_prj.data)

    def test_ho_module_full_lifecycle(self):
        # 1. Setup client and Admin login
        with self.app.app_context():
            role = Role.query.filter_by(code='Office').first()
            admin_user = User(name='Admin HO Test', email='adm_ho@example.test', password='not-used', role_id=role.id, menus=ALL_MENUS)
            db.session.add(admin_user)
            client = ClientSite(company_name='PT Klien HO Testing', address='Jl. Industri HO No 1', pic_name='Budi', pic_phone='08123456789')
            db.session.add(client)
            db.session.commit()
            client_id = client.id
            admin_id = admin_user.id

        with self.client.session_transaction() as sess:
            sess['user_id'] = admin_id
            sess['role'] = 'Admin'

        # 2. Auto HO creation via DocumentService.create_prj
        from apps.services.document_service import DocumentService
        with self.app.app_context():
            success, prj, msg = DocumentService.create_prj(
                client_id=client_id,
                title='Pemasangan Sistem CCTV Pabrik',
                so_number='SO-2026-HO-01',
                doc_date=date(2026, 9, 17),
                estimated_days=3,
                master_project_id=None,
                custom_no='',
                urgency_level='1',
                notes='Catatan internal PRJ',
                items_data=[{'description': 'Kamera Dome 4MP HD', 'qty': 4, 'uom': 'Unit'}],
                tasks_data=[{'description': 'Pemasangan kabel UTP'}]
            )
            self.assertTrue(success)
            self.assertIsNotNone(prj.ho)
            self.assertEqual(prj.ho.status, 'Online')  # STRICT: Must be Online, never Draft
            self.assertEqual(len(prj.ho.items), 1)
            self.assertEqual(prj.ho.items[0].description, 'Kamera Dome 4MP HD')
            self.assertTrue(prj.ho.display_no.endswith('/HO/26'))

            prj_id = prj.id
            ho_id = prj.ho.id
            ho_display_no = prj.ho.display_no

        # 3. Print HO: no template uploaded, so the page lists the variables.
        resp_print = self.client.get(f'/documents/ho/{ho_id}/print')
        self.assertEqual(resp_print.status_code, 200)
        self.assertIn(b'No print template for HO documents', resp_print.data)
        self.assertIn(b'ho.items', resp_print.data)

        # 4. View PRJ contains HO Card
        resp_view_prj = self.client.get(f'/documents/view/prj/{prj_id}')
        self.assertEqual(resp_view_prj.status_code, 200)
        self.assertIn(ho_display_no.encode(), resp_view_prj.data)
        self.assertIn(b'Handover Document (HO)', resp_view_prj.data)

        # 5. Admin upload physical HO photo
        photo_bytes = BytesIO(b'fake jpeg photo content for signed HO')
        resp_upload = self.client.post(f'/documents/ho/{ho_id}/upload-photo', data={
            'ho_photos': (photo_bytes, 'ho_signed_paper.jpg')
        }, content_type='multipart/form-data', follow_redirects=True)
        self.assertEqual(resp_upload.status_code, 200)

        with self.app.app_context():
            ho_db = db.session.get(HO, ho_id)
            self.assertEqual(len(ho_db.photos), 1)

        # 6. Cannot approve PRJ without pending final report
        resp_acc = self.client.post(f'/documents/review/prj/{prj_id}', data={'action': 'approve'}, follow_redirects=True)
        self.assertIn(b'has no final report awaiting approval', resp_acc.data)

        # Add technician final report
        with self.app.app_context():
            rep = WorkReport(
                document_id=prj_id,
                uploaded_by=admin_id,
                report_type='final',
                status='Pending',
                report_text='Pekerjaan CCTV telah rampung 100% dan ditandatangani klien'
            )
            db.session.add(rep)
            db.session.commit()

        # 7. Approve PRJ -> Synchronously marks HO as Approved and sets completion_date
        resp_acc_success = self.client.post(f'/documents/review/prj/{prj_id}', data={'action': 'approve'}, follow_redirects=True)
        self.assertEqual(resp_acc_success.status_code, 200)

        with self.app.app_context():
            prj_db = db.session.get(DocumentPRJ, prj_id)
            ho_db = db.session.get(HO, ho_id)
            self.assertEqual(prj_db.status, 'Completed')
            self.assertEqual(ho_db.status, 'Approved')
            self.assertIsNotNone(ho_db.completion_date)
            self.assertIsNotNone(ho_db.approved_at)

        # 8. HO Tracker lists the completed HO
        resp_tracker = self.client.get('/documents/ho?storage=1')
        self.assertEqual(resp_tracker.status_code, 200)
        self.assertIn(ho_display_no.encode(), resp_tracker.data)
        self.assertIn(b'Approved', resp_tracker.data)


if __name__ == '__main__':
    unittest.main()
