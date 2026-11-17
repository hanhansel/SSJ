"""Regression tests for the October 2026 bug-fix round (K/T/S items)."""

import os
import tempfile
import unittest
from datetime import date
from io import BytesIO

from werkzeug.security import generate_password_hash

from run import create_app
from apps.models import ALL_MENUS, ROLE_OFFICE
from apps.models import (
    db, User, Role, ClientSite, DocumentSVC, DocumentPRJ, DocumentXPRJ, ScheduleBlock,
    WorkReport, HO, HOPhoto, MasterProject,
)
from apps.services.document_service import DocumentService


class BugFixTests(unittest.TestCase):
    def setUp(self):
        self.original_env = {key: os.environ.get(key) for key in ('DATABASE_URL', 'APP_ENV')}
        os.environ['DATABASE_URL'] = 'sqlite:///:memory:'
        os.environ['APP_ENV'] = 'testing'
        self.app = create_app()
        if not self.app.config['SQLALCHEMY_DATABASE_URI'].startswith('sqlite'):
            raise RuntimeError('Test harus memakai SQLite, bukan database server (ssj_db).')
        self.app.config['TESTING'] = True
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.upload_dir = tempfile.TemporaryDirectory()
        self.app.config['UPLOAD_FOLDER'] = self.upload_dir.name
        self.client = self.app.test_client()
        with self.app.app_context():
            db.create_all()

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.drop_all()
        for key, value in self.original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.upload_dir.cleanup()

    # ---------- helpers ----------
    def _user(self, role_code, name, email, password='not-used'):
        role = Role.query.filter_by(code=role_code).one()
        user = User(name=name, email=email, password=password, role_id=role.id, menus=ALL_MENUS if role_code == ROLE_OFFICE else ())
        db.session.add(user)
        db.session.flush()
        return user

    def _prj_with_ho(self, client, so='SO-1', doc_no=1):
        ok, prj, err = DocumentService.create_prj(
            client_id=client.id, title='PRJ Test', so_number=so, doc_date=date(2026, 10, 1),
            estimated_days=2, master_project_id=None, custom_no=str(doc_no), urgency_level='1',
            notes='', items_data=[{'description': 'Pasang CCTV', 'qty': 1, 'uom': 'Unit', 'display_order': 1}],
            tasks_data=[],
        )
        self.assertTrue(ok, err)
        return prj

    def _login(self, user_id):
        with self.client.session_transaction() as session:
            session['user_id'] = user_id

    # ---------- K3: HO photos are served ----------
    def test_ho_photo_is_served_to_admin(self):
        with self.app.app_context():
            admin = self._user('Office', 'Admin', 'admin@example.test')
            client = ClientSite(company_name='Klien', address='Jl. A')
            db.session.add(client)
            db.session.flush()
            prj = self._prj_with_ho(client)
            stored = 'documents/ho/test_photo.jpg'
            os.makedirs(os.path.join(self.upload_dir.name, 'documents', 'ho'), exist_ok=True)
            with open(os.path.join(self.upload_dir.name, stored), 'wb') as f:
                f.write(b'\xff\xd8fakejpeg')
            prj.ho.photos.append(HOPhoto(stored_path=stored, original_filename='a.jpg'))
            db.session.commit()
            admin_id = admin.id

        self._login(admin_id)
        response = self.client.get(f'/technician/media/{stored}')
        self.assertEqual(response.status_code, 200)

    # ---------- K4: GET edit page no longer crashes ----------
    def test_edit_doc_get_redirects_instead_of_500(self):
        with self.app.app_context():
            admin = self._user('Office', 'Admin', 'admin@example.test')
            client = ClientSite(company_name='Klien', address='Jl. A')
            db.session.add(client)
            db.session.flush()
            xprj = DocumentXPRJ(doc_no=5, date=date(2026, 10, 1), client_id=client.id, status='Online')
            db.session.add(xprj)
            db.session.commit()
            admin_id, xprj_id = admin.id, xprj.id

        self._login(admin_id)
        response = self.client.get(f'/documents/edit/xprj/{xprj_id}')
        self.assertEqual(response.status_code, 302)
        self.assertIn('edit=1', response.headers['Location'])

    # ---------- Documents are never deleted: Void keeps everything and is final ----------
    def test_void_prj_voids_ho_keeps_xprj_and_is_final(self):
        with self.app.app_context():
            admin = self._user('Office', 'Admin', 'admin@example.test')
            tech = self._user('Technician', 'Tek', 'tek@example.test')
            client = ClientSite(company_name='Klien', address='Jl. A')
            db.session.add(client)
            db.session.flush()
            prj = self._prj_with_ho(client)
            db.session.add(WorkReport(document_id=prj.id, uploaded_by=tech.id, report_type='daily', status='Daily'))
            child = DocumentXPRJ(doc_no=9, date=date(2026, 10, 1), client_id=client.id, prj_id=prj.id, status='Online')
            db.session.add(child)
            db.session.commit()
            admin_id, prj_id, child_id = admin.id, prj.id, child.id

        self._login(admin_id)
        self.assertEqual(self.client.post(f'/documents/prj/{prj_id}/delete').status_code, 404)

        self.client.post(f'/documents/review/prj/{prj_id}', data={'status': 'Void'})
        with self.app.app_context():
            prj = db.session.get(DocumentPRJ, prj_id)
            self.assertEqual(prj.status, 'Void')
            self.assertEqual(prj.ho.status, 'Void')
            self.assertEqual(WorkReport.query.filter_by(document_id=prj_id).count(), 1)
            # XPRJ is standalone: voiding its PRJ does not change it.
            child = db.session.get(DocumentXPRJ, child_id)
            self.assertEqual(child.status, 'Online')
            self.assertEqual(child.prj_id, prj_id)

        refused = self.client.post(f'/documents/review/prj/{prj_id}', data={'status': 'Online'}, follow_redirects=True)
        self.assertIn(b'which is final', refused.data)
        with self.app.app_context():
            self.assertEqual(db.session.get(DocumentPRJ, prj_id).status, 'Void')

    # ---------- ACC records the approver; Selesai is final ----------
    def test_approve_records_approver_and_selesai_is_final(self):
        with self.app.app_context():
            admin = self._user('Office', 'Admin', 'admin@example.test')
            tech = self._user('Technician', 'Tek', 'tek@example.test')
            client = ClientSite(company_name='Klien', address='Jl. A')
            db.session.add(client)
            db.session.flush()
            prj = self._prj_with_ho(client)
            db.session.add(WorkReport(document_id=prj.id, uploaded_by=tech.id, report_type='final', status='Pending'))
            db.session.commit()
            admin_id, prj_id = admin.id, prj.id

        self._login(admin_id)
        # ACC needs the signed HO photo.
        self.client.post(f'/documents/review/prj/{prj_id}', data={'action': 'approve'})
        with self.app.app_context():
            prj = db.session.get(DocumentPRJ, prj_id)
            self.assertEqual(prj.status, 'Online')
            prj.ho.photos.append(HOPhoto(stored_path='documents/ho/p.jpg', original_filename='p.jpg'))
            db.session.commit()

        self.client.post(f'/documents/review/prj/{prj_id}', data={'action': 'approve'})
        with self.app.app_context():
            prj = db.session.get(DocumentPRJ, prj_id)
            self.assertEqual(prj.status, 'Completed')
            self.assertEqual(prj.ho.status, 'Approved')
            self.assertEqual(prj.ho.approved_by, admin_id)
            self.assertEqual(WorkReport.query.filter_by(document_id=prj_id).one().status, 'Approved')

        for status in ('Online', 'Void'):
            self.client.post(f'/documents/review/prj/{prj_id}', data={'status': status})
        with self.app.app_context():
            prj = db.session.get(DocumentPRJ, prj_id)
            self.assertEqual(prj.status, 'Completed')
            self.assertEqual(prj.ho.status, 'Approved')

    # ---------- T3: technician cannot touch a locked HO ----------
    def test_technician_cannot_add_photo_to_locked_ho(self):
        with self.app.app_context():
            tech = self._user('Technician', 'Tek', 'tek@example.test')
            client = ClientSite(company_name='Klien', address='Jl. A')
            db.session.add(client)
            db.session.flush()
            prj = self._prj_with_ho(client)
            prj.ho.status = 'Approved'
            db.session.add(ScheduleBlock(user_id=tech.id, document_id=prj.id, start_date=date(2026, 10, 1)))
            db.session.commit()
            tech_id, prj_id = tech.id, prj.id

        self._login(tech_id)
        self.client.post('/technician/report/submit', data={
            'doc_type': 'prj', 'doc_id': str(prj_id), 'report_type': 'daily', 'report_title': 'Harian',
            'ho_photos': (BytesIO(b'jpeg'), 'ho.jpg'),
        }, content_type='multipart/form-data')
        with self.app.app_context():
            ho = db.session.get(DocumentPRJ, prj_id).ho
            self.assertEqual(ho.status, 'Approved')
            self.assertEqual(len(ho.photos), 0)

    # ---------- T7: LIKE wildcards no longer match accounts ----------
    def test_login_wildcard_does_not_match(self):
        with self.app.app_context():
            self._user('Office', 'Admin', 'admin@example.test', generate_password_hash('rahasia123'))
            db.session.commit()
        response = self.client.post('/login', data={'username': '%', 'password': 'rahasia123'})
        self.assertNotEqual(response.status_code, 302)
        ok = self.client.post('/login', data={'username': 'ADMIN@example.test', 'password': 'rahasia123'})
        self.assertEqual(ok.status_code, 302)

    # ---------- R6: first setup shows its validation error and keeps the typed values ----------
    def test_first_setup_validation_is_visible_and_internal_email_allowed(self):
        rejected = self.client.post('/first-setup', data={
            'username': 'admin.pusat', 'email': 'admin@ssj', 'password': 'pendek',
        })
        self.assertEqual(rejected.status_code, 400)
        self.assertIn(b'Password must be at least 8 characters', rejected.data)
        self.assertIn(b'value="admin.pusat"', rejected.data)
        self.assertIn(b'value="admin@ssj"', rejected.data)

        created = self.client.post('/first-setup', data={
            'username': 'admin.pusat', 'email': 'Admin@SSJ', 'password': 'rahasia123',
        })
        self.assertEqual(created.status_code, 302)
        with self.app.app_context():
            self.assertEqual(User.query.one().email, 'admin@ssj')

    def test_first_setup_shows_every_field_error_at_once(self):
        response = self.client.post('/first-setup', data={
            'username': '', 'email': 'bukan-email', 'password': 'pendek',
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn(b'Username is required', response.data)
        self.assertIn(b'Please enter a valid email address', response.data)
        self.assertIn(b'Password must be at least 8 characters', response.data)
        self.assertIn(b'value="bukan-email"', response.data)

    def test_first_setup_missing_role_keeps_typed_values(self):
        with self.app.app_context():
            Role.query.filter_by(code='Office').delete()
            db.session.commit()
        response = self.client.post('/first-setup', data={
            'username': 'admin.pusat', 'email': 'admin@ssj', 'password': 'rahasia123',
        })
        self.assertEqual(response.status_code, 503)
        self.assertIn(b'Default roles are not available yet', response.data)
        self.assertIn(b'value="admin.pusat"', response.data)
        self.assertIn(b'value="admin@ssj"', response.data)

    # ---------- S4: previous-month navigation no longer skips a month ----------
    def test_calendar_previous_month(self):
        from apps.core.dates import format_month_year
        from apps.models import wib_now

        with self.app.app_context():
            tech = self._user('Technician', 'Tek', 'tek@example.test')
            db.session.commit()
            tech_id = tech.id

        self._login(tech_id)
        today = wib_now().date()
        for offset in (-1, -2, 1, 12):
            index = today.year * 12 + (today.month - 1) + offset
            expected = format_month_year(index // 12, index % 12 + 1)
            response = self.client.get(f'/technician/dashboard?month_offset={offset}')
            self.assertEqual(response.status_code, 200)
            self.assertIn(expected.encode(), response.data, f'offset {offset}')

        # Non-numeric offset falls back to the current month instead of a 500.
        self.assertEqual(self.client.get('/technician/dashboard?month_offset=abc').status_code, 200)


    # ---------- Settings: HO number format + print template ----------
    def test_settings_show_ho_format_and_template_without_default_notice(self):
        with self.app.app_context():
            admin_id = self._user('Office', 'Admin', 'admin@example.test').id
            db.session.commit()
        self._login(admin_id)
        page = self.client.get('/system/settings')
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'name="ho_format"', page.data)
        self.assertIn(b'tplFile-ho', page.data)
        self.assertNotIn(b'Menggunakan template bawaan', page.data)

    def test_uploaded_ho_print_template_is_used(self):
        template_dir = tempfile.TemporaryDirectory()
        self.addCleanup(template_dir.cleanup)
        self.app.config['PRINT_TEMPLATE_FOLDER'] = template_dir.name
        with self.app.app_context():
            admin_id = self._user('Office', 'Admin', 'admin@example.test').id
            client = ClientSite(company_name='Klien', address='Jl. A')
            db.session.add(client)
            db.session.flush()
            ho_id = self._prj_with_ho(client).ho.id
            db.session.commit()
        self._login(admin_id)
        staged = self.client.post('/system/settings/print-template/ho/stage', data={
            'template': (BytesIO(b'<p>CUSTOM HO {{ ho.display_no }}</p>'), 'ho.html'),
        }, content_type='multipart/form-data')
        self.client.post('/system/settings/print-template/ho/apply', json={'token': staged.get_json()['token']})
        printed = self.client.get(f'/documents/ho/{ho_id}/print')
        self.assertIn(b'CUSTOM HO 001/HO/26', printed.data)

        self.client.post('/system/settings/print-template/ho/reset')
        printed = self.client.get(f'/documents/ho/{ho_id}/print')
        self.assertNotIn(b'CUSTOM HO', printed.data)

    # ---------- HO list pages: simplified like the other document lists ----------
    def test_ho_lists_have_no_tagline_or_status_filter(self):
        with self.app.app_context():
            admin_id = self._user('Office', 'Admin', 'admin@example.test').id
            client = ClientSite(company_name='Klien', address='Jl. A')
            db.session.add(client)
            db.session.flush()
            self._prj_with_ho(client)
            db.session.commit()
        self._login(admin_id)
        active = self.client.get('/documents/ho')
        storage = self.client.get('/documents/ho?storage=1')
        for page in (active, storage):
            self.assertEqual(page.status_code, 200)
            self.assertNotIn(b'HO document tracker', page.data)
            self.assertNotIn(b'Filter Status', page.data)
            self.assertNotIn(b'(Storage)', page.data)
        # A HO only exists through its PRJ: no create, unlink or renumber actions.
        for removed in (b'/unlink', b'editHoNumberModal', b'Create New Document'):
            self.assertNotIn(removed, active.data)

    # ---------- Numbering: HO = PRJ number, numbers are never reused ----------
    def test_ho_number_follows_prj_and_numbers_are_never_reused(self):
        with self.app.app_context():
            client = ClientSite(company_name='Klien', address='Jl. A')
            db.session.add(client)
            db.session.flush()
            fortieth = self._prj_with_ho(client, so='SO-40', doc_no=40)
            self.assertEqual(fortieth.display_no, '040/PRJ/26')
            self.assertEqual(fortieth.ho.display_no, '040/HO/26')
            self.assertEqual(fortieth.ho.date, fortieth.date)

            DocumentService.review_doc(fortieth, None, 'Void')
            # The voided number stays taken: the next PRJ is 41, and the replacement may reuse the SO.
            ok, replacement, err = DocumentService.create_prj(
                client_id=client.id, title='Pengganti', so_number='SO-40', doc_date=date(2026, 10, 2),
                estimated_days=1, master_project_id=None, custom_no='', urgency_level='1', notes='',
                items_data=[{'description': 'Item', 'qty': 1, 'uom': 'Unit', 'display_order': 1}], tasks_data=[],
            )
            self.assertTrue(ok, err)
            self.assertEqual(replacement.doc_no, 41)
            self.assertEqual(replacement.ho.display_no, '041/HO/26')

            # A custom number must be above the highest one already issued.
            for taken in ('40', '41', '12'):
                ok, _, err = DocumentService.generate_number_and_validate('prj', date(2026, 10, 3), taken)
                self.assertFalse(ok)
                self.assertIn('at least 42', err)
            ok, doc_no, _ = DocumentService.generate_number_and_validate('prj', date(2026, 10, 3), '60')
            self.assertTrue(ok)
            self.assertEqual(doc_no, 60)

    def test_every_prj_gets_exactly_one_ho_even_when_created_directly(self):
        with self.app.app_context():
            client = ClientSite(company_name='Klien', address='Jl. A')
            db.session.add(client)
            db.session.flush()
            prj = DocumentPRJ(doc_no=7, date=date(2026, 3, 1), client_id=client.id, title='Langsung',
                              so_number='SO-7', status='Completed')
            db.session.add(prj)
            db.session.commit()
            self.assertEqual(HO.query.filter_by(prj_id=prj.id).count(), 1)
            self.assertEqual(prj.ho.status, 'Approved')
            self.assertEqual(prj.ho.display_no, '007/HO/26')

    # ---------- PRJ detail: simplified HO section ----------
    def test_prj_detail_ho_section_is_simplified(self):
        with self.app.app_context():
            admin_id = self._user('Office', 'Admin', 'admin@example.test').id
            client = ClientSite(company_name='Klien', address='Jl. A')
            db.session.add(client)
            db.session.flush()
            prj_id = self._prj_with_ho(client).id
            db.session.commit()
        self._login(admin_id)
        page = self.client.get(f'/documents/view/prj/{prj_id}')
        self.assertEqual(page.status_code, 200)
        self.assertNotIn(b'Unlink HO', page.data)
        self.assertNotIn(b'Technicians must upload', page.data)
        self.assertEqual(page.data.count(b'data-bs-target="#adminHoPhotoModal"'), 1)
        self.assertIn(b'001/HO/26', page.data)
        for removed in (b'Physical Photo Uploaded', b'Physical Photo</span>', b'Document Date'):
            self.assertNotIn(removed, page.data)
        self.assertIn(b'HO Document Photos', page.data)
        self.assertIn(b'<option value="Completed" data-url=', page.data)
        self.assertIn(b'<option value="Void" data-url=', page.data)
        self.assertIn(b'statusConfirmModal', page.data)
        for missing in (b'No daily report from a technician yet', b'The final report has not been uploaded yet',
                        b'has not been uploaded yet'):
            self.assertIn(missing, page.data)
        for removed in (b'Create New HO', b'Link HO', b'linkHoModal', b'Not Linked'):
            self.assertNotIn(removed, page.data)

    # ---------- PRJ can be completed manually without report/photos ----------
    def test_prj_completed_from_status_dropdown_leaves_master_project_open(self):
        with self.app.app_context():
            admin_id = self._user('Office', 'Admin', 'admin@example.test').id
            client = ClientSite(company_name='Klien', address='Jl. A')
            db.session.add(client)
            db.session.flush()
            project = MasterProject(name='MP A', client_id=client.id)
            db.session.add(project)
            db.session.flush()
            prj = self._prj_with_ho(client)
            prj.master_project_id = project.id
            db.session.commit()
            prj_id, project_id = prj.id, project.id
        self._login(admin_id)
        # A stale form field from the old popup must not close the project either.
        self.client.post(f'/documents/review/prj/{prj_id}', data={
            'status': 'Completed', 'complete_master_project': '1',
        })
        with self.app.app_context():
            prj = db.session.get(DocumentPRJ, prj_id)
            self.assertEqual(prj.status, 'Completed')
            self.assertIsNotNone(prj.actual_completion)
            self.assertEqual(prj.ho.status, 'Approved')
            self.assertEqual(prj.ho.approved_by, admin_id)
            # Projects are closed only on the Master Project page.
            self.assertEqual(db.session.get(MasterProject, project_id).status, 'Online')

    def test_master_project_stays_open_when_other_prj_active(self):
        with self.app.app_context():
            admin_id = self._user('Office', 'Admin', 'admin@example.test').id
            client = ClientSite(company_name='Klien', address='Jl. A')
            db.session.add(client)
            db.session.flush()
            project = MasterProject(name='MP B', client_id=client.id)
            db.session.add(project)
            db.session.flush()
            first = self._prj_with_ho(client, so='SO-1', doc_no=1)
            second = self._prj_with_ho(client, so='SO-2', doc_no=2)
            first.master_project_id = second.master_project_id = project.id
            db.session.commit()
            first_id, project_id = first.id, project.id
        self._login(admin_id)
        self.client.post(f'/documents/review/prj/{first_id}', data={'status': 'Completed'})
        with self.app.app_context():
            self.assertEqual(db.session.get(DocumentPRJ, first_id).status, 'Completed')
            self.assertEqual(db.session.get(MasterProject, project_id).status, 'Online')

    def test_confirmation_lists_ho_and_never_closes_project(self):
        with self.app.app_context():
            client = ClientSite(company_name='Klien', address='Jl. A')
            db.session.add(client)
            db.session.flush()
            project = MasterProject(name='MP C', client_id=client.id, status='Online')
            db.session.add(project)
            db.session.flush()
            first = self._prj_with_ho(client, so='SO-1', doc_no=1)
            second = self._prj_with_ho(client, so='SO-2', doc_no=2)
            first.master_project_id = second.master_project_id = project.id
            db.session.commit()

            void_impacts = DocumentService.status_change_impacts(first, 'Void')
            self.assertEqual(void_impacts['cascade'], ['HO 001/HO/26 becomes Void as well.'])
            self.assertEqual(void_impacts['missing'], [])
            self.assertNotIn('master_project', void_impacts)

            DocumentService.review_doc(second, None, 'Void')
            done_impacts = DocumentService.status_change_impacts(first, 'Completed')
            self.assertNotIn('master_project', done_impacts)
            self.assertEqual(len(done_impacts['missing']), 3)

            # Finishing the last active PRJ leaves the project open; it is closed on its own page.
            DocumentService.review_doc(first, None, 'Void')
            self.assertEqual(db.session.get(MasterProject, project.id).status, 'Online')

    def test_xprj_parent_may_be_finished_prj_but_not_void(self):
        from apps.services.document_service import resolve_parent_prj
        with self.app.app_context():
            client = ClientSite(company_name='Klien', address='Jl. A')
            db.session.add(client)
            db.session.flush()
            finished = self._prj_with_ho(client, so='SO-1', doc_no=1)
            voided = self._prj_with_ho(client, so='SO-2', doc_no=2)
            DocumentService.review_doc(finished, None, 'Completed')
            DocumentService.review_doc(voided, None, 'Void')
            self.assertTrue(resolve_parent_prj(str(finished.id))[0])
            self.assertFalse(resolve_parent_prj(str(voided.id))[0])

    def test_list_acc_completes_document_and_returns_to_list(self):
        with self.app.app_context():
            admin_id = self._user('Office', 'Admin', 'admin@example.test').id
            client = ClientSite(company_name='Klien', address='Jl. A')
            db.session.add(client)
            db.session.flush()
            prj_id = self._prj_with_ho(client).id
            db.session.commit()
        self._login(admin_id)
        listing = self.client.get('/documents/prj')
        self.assertIn(b'openStatusConfirm(this)', listing.data)
        response = self.client.post(f'/documents/review/prj/{prj_id}', data={
            'status': 'Completed', 'redirect_to': '/documents/prj',
        })
        self.assertTrue(response.headers['Location'].endswith('/documents/prj'))
        with self.app.app_context():
            self.assertEqual(db.session.get(DocumentPRJ, prj_id).status, 'Completed')

    def test_technician_final_upload_puts_prj_and_ho_in_approval(self):
        with self.app.app_context():
            tech = self._user('Technician', 'Tek', 'tek@example.test')
            client = ClientSite(company_name='Klien', address='Jl. A')
            db.session.add(client)
            db.session.flush()
            prj = self._prj_with_ho(client)
            # The signed HO was already uploaded by an admin.
            prj.ho.photos.append(HOPhoto(stored_path='documents/ho/admin.jpg', original_filename='a.jpg'))
            db.session.add(ScheduleBlock(user_id=tech.id, document_id=prj.id, start_date=date(2026, 10, 1)))
            db.session.commit()
            tech_id, prj_id = tech.id, prj.id

        self._login(tech_id)
        self.client.post('/technician/report/submit', data={
            'doc_type': 'prj', 'doc_id': str(prj_id), 'report_type': 'final', 'report_title': 'Completed',
            'photos': (BytesIO(b'jpeg'), 'hasil.jpg'),
        }, content_type='multipart/form-data')
        with self.app.app_context():
            prj = db.session.get(DocumentPRJ, prj_id)
            self.assertEqual(prj.status, 'Pending Approval')
            self.assertEqual(prj.ho.status, 'Pending Approval')

    # ---------- Photos: only the date taken survives the upload ----------
    def test_report_photo_keeps_only_its_date_taken(self):
        from datetime import datetime
        from pathlib import Path
        from PIL import Image
        from apps.models import WorkReportPhoto
        with self.app.app_context():
            tech = self._user('Technician', 'Tek', 'tek@example.test')
            admin_id = self._user('Office', 'Admin', 'admin@example.test').id
            client = ClientSite(company_name='Klien', address='Jl. A')
            db.session.add(client)
            db.session.flush()
            prj = self._prj_with_ho(client)
            db.session.add(ScheduleBlock(user_id=tech.id, document_id=prj.id, start_date=date(2026, 10, 1)))
            db.session.commit()
            tech_id, prj_id = tech.id, prj.id

        exif = Image.Exif()
        exif.get_ifd(0x8769)[0x9003] = '2026:10:29 09:12:30'  # DateTimeOriginal
        exif.get_ifd(0x8825)[1] = 'S'                         # a GPS tag
        exif[0x0110] = 'Phone Model X'
        photo = BytesIO()
        Image.new('RGB', (64, 48), 'red').save(photo, 'JPEG', exif=exif)
        photo.seek(0)

        self._login(tech_id)
        self.client.post('/technician/report/submit', data={
            'doc_type': 'prj', 'doc_id': str(prj_id), 'report_type': 'daily', 'report_title': 'Progress',
            'photos': (photo, 'hasil.jpg'),
        }, content_type='multipart/form-data')

        with self.app.app_context():
            stored = WorkReportPhoto.query.one()
            self.assertEqual(stored.taken_at, datetime(2026, 10, 29, 9, 12, 30))
            with Image.open(Path(self.upload_dir.name) / stored.stored_path) as image:
                self.assertEqual(dict(image.getexif()), {})  # no GPS, no phone model, nothing
                self.assertIsNone(image.info.get('exif'))
        self._login(admin_id)
        page = self.client.get(f'/documents/view/prj/{prj_id}')
        self.assertIn('Taken 29 October 2026 09:12'.encode(), page.data)

    # ---------- HO print: opened through the print dialog ----------
    def test_ho_print_is_clean_and_opened_through_print_dialog(self):
        with self.app.app_context():
            admin_id = self._user('Office', 'Admin', 'admin@example.test').id
            client = ClientSite(company_name='Klien', address='Jl. Panjang ' * 20, pic_name='Pak PIC')
            db.session.add(client)
            db.session.flush()
            prj = self._prj_with_ho(client)
            db.session.commit()
            prj_id, ho_id = prj.id, prj.ho.id
        self._login(admin_id)

        printed = self.client.get(f'/documents/ho/{ho_id}/print')
        self.assertEqual(printed.status_code, 200)
        self.assertNotIn(b'no-print-toolbar', printed.data)
        self.assertNotIn(b'text-overflow', printed.data)
        self.assertNotIn(b'Printed Name', printed.data)
        self.assertNotIn(b'Ditest Oleh', printed.data)
        self.assertNotIn(b'Pak PIC', printed.data)
        self.assertIn(b'No print template for HO documents', printed.data)

        print_call = b'printDocument(this.href)'
        self.assertIn(print_call, self.client.get('/documents/ho').data)
        self.assertIn(print_call, self.client.get(f'/documents/view/prj/{prj_id}').data)

    def test_ho_print_excludes_technician_input(self):
        """Without a template the HO printout leaks no technician input: no signed photos, no notes."""
        with self.app.app_context():
            admin_id = self._user('Office', 'Admin', 'admin@example.test').id
            client = ClientSite(company_name='Klien', address='Jl. A')
            db.session.add(client)
            db.session.flush()
            prj = self._prj_with_ho(client)
            prj.ho.notes = 'Catatan dari teknisi'
            prj.ho.photos.append(HOPhoto(stored_path='ho/signed.jpg', original_filename='signed.jpg'))
            db.session.commit()
            ho_id = prj.ho.id
        self._login(admin_id)

        printed = self.client.get(f'/documents/ho/{ho_id}/print')
        self.assertEqual(printed.status_code, 200)
        self.assertIn(b'No print template for HO documents', printed.data)
        self.assertNotIn(b'Catatan dari teknisi', printed.data)
        self.assertNotIn(b'signed.jpg', printed.data)
        self.assertNotIn(b'LAMPIRAN FOTO', printed.data)

    # ---------- Scheduler: SVC priority follows the client type ----------
    def test_svc_backlog_priority_follows_contract_client(self):
        from apps.scheduling.routes import _build_dashboard_state
        with self.app.app_context():
            mc = ClientSite(company_name='Klien MC', address='Jl. A', client_type='contract')
            regular = ClientSite(company_name='Klien Biasa', address='Jl. B', client_type='On-Call')
            db.session.add_all([mc, regular])
            db.session.flush()
            # The regular SVC is older, so aging alone would put it above the MC one.
            db.session.add_all([
                DocumentSVC(document_type='svc', doc_no=1, date=date(2026, 1, 1), client_id=regular.id,
                            customer_complaint='K', task_description='T', status='Online'),
                DocumentSVC(document_type='svc', doc_no=2, date=date(2026, 9, 1), client_id=mc.id,
                            customer_complaint='K', task_description='T', status='Online'),
                DocumentXPRJ(document_type='xprj', doc_no=1, date=date(2026, 9, 15), client_id=regular.id,
                             customer_complaint='K', task_description='T', status='Online'),
            ])
            db.session.commit()

            backlog, _, _ = _build_dashboard_state(date(2026, 10, 1))

        order = [(item['doc_type'], item['client_name'], item['urgency']) for item in backlog]
        self.assertEqual(order, [
            ('xprj', 'Klien Biasa', 'normal'),
            ('svc', 'Klien MC', 'urgent'),
            ('svc', 'Klien Biasa', 'normal'),
        ])

if __name__ == '__main__':
    unittest.main()
