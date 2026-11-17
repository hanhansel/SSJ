"""Hold status: a paused document hidden from the scheduler and technicians."""

import os
import re
import unittest
from datetime import timedelta

from run import create_app
from apps.models import ALL_MENUS, ROLE_OFFICE
from apps.models import (
    db, User, Role, ClientSite, DocumentSVC, DocumentPRJ, DocumentXPRJ, ScheduleBlock, WorkReport, MasterProject,
    wib_now,
)
from apps.services.document_service import DocumentService
from apps.services.project_service import ProjectService


class HoldStatusTests(unittest.TestCase):
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
            self.admin_id = self._user('Office', 'admin@example.test')
            self.tech_id = self._user('Technician', 'tech@example.test')
            client = ClientSite(company_name='PT Klien', address='Jl. A')
            db.session.add(client)
            db.session.commit()
            self.client_id = client.id

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.drop_all()
        for key, value in self.original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    # ---------- helpers ----------
    def _user(self, role_code, email):
        role = Role.query.filter_by(code=role_code).one()
        user = User(name=email.split('@')[0], email=email, password='x', role_id=role.id, menus=ALL_MENUS if role_code == ROLE_OFFICE else ())
        db.session.add(user)
        db.session.commit()
        return user.id

    def _login(self, user_id):
        with self.client.session_transaction() as session:
            session['user_id'] = user_id

    def _svc(self, doc_no=1, status='Online'):
        svc = DocumentSVC(doc_no=doc_no, doc_period=str(self.today.year), doc_number_text=f'{doc_no}/SVC',
                          date=self.today, client_id=self.client_id, status=status)
        db.session.add(svc)
        db.session.commit()
        return svc

    def _prj(self, master_project_id=None):
        ok, prj, err = DocumentService.create_prj(
            client_id=self.client_id, title='Pasang', so_number='SO-1', doc_date=self.today, estimated_days=2,
            master_project_id=master_project_id, custom_no='', urgency_level='1', notes='',
            items_data=[{'description': 'Pasang CCTV', 'qty': 1, 'uom': 'Unit', 'display_order': 1}],
            tasks_data=[],
        )
        self.assertTrue(ok, err)
        return prj

    def _block(self, doc, start_offset, days):
        block = ScheduleBlock(user_id=self.tech_id, document_id=doc.id,
                              start_date=self.today + timedelta(days=start_offset), days_duration=days)
        db.session.add(block)
        db.session.commit()
        return block.id

    def _backlog_ids(self):
        state = self.client.get(f'/scheduling/api/dashboard_state?start_date={self.today.isoformat()}').get_json()
        return {item['id'] for item in state['backlog']}

    # ---------- tests ----------
    def test_hold_trims_schedule_and_hides_document_until_resumed(self):
        with self.app.app_context():
            svc = self._svc()
            svc_id = svc.id
            past = self._block(svc, -5, 2)      # worked days: kept
            running = self._block(svc, -1, 3)   # yesterday..tomorrow: cut to yesterday
            starting = self._block(svc, 0, 1)   # today: removed
            future = self._block(svc, 2, 1)     # removed
            impacts = DocumentService.status_change_impacts(svc, 'Hold')
            self.assertIn('3 technician schedule blocks from today on are deleted', impacts['cascade'][0])

        self._login(self.admin_id)
        self.client.post(f'/documents/review/svc/{svc_id}', data={'status': 'Hold'})
        with self.app.app_context():
            self.assertEqual(db.session.get(DocumentSVC, svc_id).status, 'Hold')
            self.assertEqual(db.session.get(ScheduleBlock, past).days_duration, 2)
            self.assertEqual(db.session.get(ScheduleBlock, running).days_duration, 1)
            self.assertIsNone(db.session.get(ScheduleBlock, starting))
            self.assertIsNone(db.session.get(ScheduleBlock, future))

        # Scheduler: not in the backlog and cannot be (re)scheduled.
        self.assertNotIn(svc_id, self._backlog_ids())
        assign = self.client.post('/scheduling/api/assign', json={
            'user_id': self.tech_id, 'doc_type': 'svc', 'doc_id': svc_id, 'start_date': self.today.isoformat()})
        self.assertEqual(assign.status_code, 409)
        suggest = self.client.post('/scheduling/api/suggest', json={
            'start_date': self.today.isoformat(), 'documents': [{'doc_type': 'svc', 'id': svc_id}]})
        self.assertEqual(suggest.status_code, 409)
        edit = self.client.post('/scheduling/api/update_task', json={'block_id': past, 'days_duration': 9})
        self.assertEqual(edit.status_code, 409)

        # Active list hides it; the Hold filter shows it.  Rows are checked by their
        # link, since flash messages also mention the document number.
        admin_link, tech_link = f'/documents/view/svc/{svc_id}', f'/technician/task/svc/{svc_id}'
        self.assertNotIn(admin_link, self.client.get('/documents/list').data.decode())
        self.assertIn(admin_link, self.client.get('/documents/list?status=hold').data.decode())

        # Technician: hidden everywhere, and a report cannot slip it into Pending Approval.
        self._login(self.tech_id)
        self.assertNotIn(tech_link, self.client.get('/technician/documents').data.decode())
        past_day = (self.today - timedelta(days=5)).isoformat()
        self.assertNotIn(tech_link, self.client.get(f'/technician/calendar-tasks?date={past_day}').data.decode())
        self.assertEqual(self.client.get(tech_link).status_code, 302)
        self.client.post('/technician/report/submit', data={
            'doc_type': 'svc', 'doc_id': svc_id, 'report_type': 'final', 'report_title': 'Completed',
        })
        with self.app.app_context():
            self.assertEqual(db.session.get(DocumentSVC, svc_id).status, 'Hold')
            self.assertEqual(WorkReport.query.count(), 0)

        # Resume: back in the backlog and visible to the technician again.
        self._login(self.admin_id)
        self.client.post(f'/documents/review/svc/{svc_id}', data={'status': 'Online'})
        self.assertIn(svc_id, self._backlog_ids())
        self._login(self.tech_id)
        self.assertIn(tech_link, self.client.get('/technician/documents').data.decode())

    def test_hold_transition_rules(self):
        with self.app.app_context():
            pending = self._svc(doc_no=1, status='Pending Approval')
            ok, _, _ = DocumentService.review_doc(pending, '', 'Hold')
            self.assertFalse(ok)
            self.assertEqual(pending.status, 'Pending Approval')

            held = self._svc(doc_no=2, status='Hold')
            for target in ('Completed', 'Pending Approval'):
                ok, _, _ = DocumentService.review_doc(held, '', target)
                self.assertFalse(ok, target)
                self.assertEqual(held.status, 'Hold')

            past = self._block(held, -3, 1)
            ok, _, _ = DocumentService.review_doc(held, '', 'Void')
            self.assertTrue(ok)
            self.assertEqual(held.status, 'Void')
            self.assertIsNone(db.session.get(ScheduleBlock, past))

    def test_prj_hold_moves_ho_but_not_xprj_and_keeps_master_project_open(self):
        with self.app.app_context():
            project = MasterProject(name='Proyek', client_id=self.client_id, status='Online')
            db.session.add(project)
            db.session.commit()
            prj = self._prj(master_project_id=project.id)
            xprj = DocumentXPRJ(doc_no=1, doc_period=str(self.today.year), date=self.today,
                                client_id=self.client_id, prj_id=prj.id, status='Online')
            db.session.add(xprj)
            db.session.commit()

            impacts = DocumentService.status_change_impacts(prj, 'Hold')
            self.assertIn(f'HO {prj.ho.display_no} becomes Hold as well.', impacts['cascade'])

            ok, message, _ = DocumentService.review_doc(prj, '', 'Hold')
            self.assertTrue(ok, message)
            self.assertEqual((prj.status, prj.ho.status, xprj.status), ('Hold', 'Hold', 'Online'))

            ok, message = ProjectService.update_master_project(project, 'Proyek', 'Completed', '')
            self.assertFalse(ok)
            self.assertIn(prj.display_no, message)

            ok, _, _ = DocumentService.review_doc(prj, '', 'Online')
            self.assertTrue(ok)
            self.assertEqual((prj.status, prj.ho.status), ('Online', 'Online'))

    def _unscheduled_count(self):
        page = self.client.get('/system/dashboard').data.decode()
        return int(re.search(r'Not Scheduled</div>\s*<div class="kpi-value">(\d+)', page).group(1))

    def test_master_project_hold_pauses_its_prjs_without_changing_their_status(self):
        with self.app.app_context():
            project = MasterProject(name='Proyek Jeda', client_id=self.client_id, status='Online')
            db.session.add(project)
            db.session.commit()
            project_id = project.id
            online = self._prj(master_project_id=project.id)
            unscheduled = self._prj(master_project_id=project.id)
            held = self._prj(master_project_id=project.id)
            DocumentService.review_doc(held, '', 'Hold')
            pending = self._prj(master_project_id=project.id)
            pending.status = 'Pending Approval'
            xprj = DocumentXPRJ(doc_no=1, doc_period=str(self.today.year), date=self.today,
                                client_id=self.client_id, prj_id=online.id, status='Online')
            db.session.add(xprj)
            db.session.commit()
            past = self._block(online, -5, 2)
            running = self._block(online, -1, 3)
            future = self._block(online, 2, 1)
            ids = {name: doc.id for name, doc in
                   (('online', online), ('unscheduled', unscheduled), ('held', held), ('pending', pending), ('xprj', xprj))}
            self.assertEqual(ProjectService.upcoming_block_count(project), 2)

        self._login(self.admin_id)
        unscheduled_before = self._unscheduled_count()
        page = self.client.get('/documents/master-projects').data.decode()
        self.assertIn('data-upcoming-blocks="2"', page)
        self.client.post(f'/documents/master-projects/{project_id}', data={'name': 'Proyek Jeda', 'status': 'Hold'})

        with self.app.app_context():
            self.assertEqual(db.session.get(MasterProject, project_id).status, 'Hold')
            # No PRJ status changes: not even the HO, and the PRJ held on its own stays Hold.
            prj = db.session.get(DocumentPRJ, ids['online'])
            self.assertEqual((prj.status, prj.ho.status), ('Online', 'Online'))
            self.assertEqual(db.session.get(DocumentPRJ, ids['held']).status, 'Hold')
            self.assertEqual(db.session.get(DocumentPRJ, ids['pending']).status, 'Pending Approval')
            # The schedule from today on is removed; worked days stay.
            self.assertEqual(db.session.get(ScheduleBlock, past).days_duration, 2)
            self.assertEqual(db.session.get(ScheduleBlock, running).days_duration, 1)
            self.assertIsNone(db.session.get(ScheduleBlock, future))

        # Scheduler: the PRJs leave the backlog and cannot be scheduled; the XPRJ stays.
        backlog = self._backlog_ids()
        self.assertNotIn(ids['online'], backlog)
        self.assertNotIn(ids['unscheduled'], backlog)
        self.assertIn(ids['xprj'], backlog)
        self.assertEqual(self._unscheduled_count(), unscheduled_before - 1)
        assign = self.client.post('/scheduling/api/assign', json={
            'user_id': self.tech_id, 'doc_type': 'prj', 'doc_id': ids['online'], 'start_date': self.today.isoformat()})
        self.assertEqual(assign.status_code, 409)
        self.assertIn('Master Project', assign.get_json()['message'])
        suggest = self.client.post('/scheduling/api/suggest', json={
            'start_date': self.today.isoformat(), 'documents': [{'doc_type': 'prj', 'id': ids['online']}]})
        self.assertEqual(suggest.status_code, 409)
        edit = self.client.post('/scheduling/api/update_task', json={'block_id': past, 'days_duration': 9})
        self.assertEqual(edit.status_code, 409)
        # Old blocks stay on the calendar as history and may still be removed.
        state = self.client.get(f'/scheduling/api/dashboard_state?start_date={(self.today - timedelta(days=5)).isoformat()}').get_json()
        self.assertIn(past, {block['id'] for block in state['schedules']})
        self.assertEqual(self.client.post('/scheduling/api/remove_task', json={'block_id': running}).status_code, 200)

        # A pending PRJ can still be finished.
        with self.app.app_context():
            ok, message, _ = DocumentService.review_doc(db.session.get(DocumentPRJ, ids['pending']), '', 'Completed')
            self.assertTrue(ok, message)

        # Technician: hidden, past days included.
        prj_link, xprj_link = f'/technician/task/prj/{ids["online"]}', f'/technician/task/xprj/{ids["xprj"]}'
        self._login(self.tech_id)
        self.assertNotIn(prj_link, self.client.get('/technician/documents').data.decode())
        past_day = (self.today - timedelta(days=5)).isoformat()
        self.assertNotIn(prj_link, self.client.get(f'/technician/calendar-tasks?date={past_day}').data.decode())
        self.assertEqual(self.client.get(prj_link).status_code, 302)

        # Online again: back in the backlog and the technician app; the PRJ held on its own stays out.
        self._login(self.admin_id)
        self.client.post(f'/documents/master-projects/{project_id}', data={'name': 'Proyek Jeda', 'status': 'Online'})
        backlog = self._backlog_ids()
        self.assertIn(ids['online'], backlog)
        self.assertNotIn(ids['held'], backlog)
        self._login(self.tech_id)
        self.assertIn(prj_link, self.client.get('/technician/documents').data.decode())

    def test_master_project_hold_rules_for_closing_and_moving_prjs(self):
        with self.app.app_context():
            paused = MasterProject(name='Proyek Jeda', client_id=self.client_id, status='Hold')
            other = MasterProject(name='Proyek Lain', client_id=self.client_id, status='Online')
            db.session.add_all([paused, other])
            db.session.commit()
            prj = self._prj(master_project_id=other.id)
            ok, error, _ = DocumentService.create_prj(
                client_id=self.client_id, title='X', so_number='SO-2', doc_date=self.today, estimated_days=1,
                master_project_id=paused.id, custom_no='', urgency_level='1', notes='',
                items_data=[{'description': 'X', 'qty': 1, 'uom': 'Unit', 'display_order': 1}], tasks_data=[])
            self.assertFalse(ok)

            def move(project_id):
                return DocumentService.edit_prj(prj, self.client_id, prj.title, prj.so_number, prj.site_address, 2,
                                                project_id, '1', '', [{'description': 'X', 'qty': 1, 'uom': 'Unit',
                                                                       'display_order': 1}], [])
            ok, error = move(paused.id)
            self.assertFalse(ok)
            self.assertIn('Online', error)
            self.assertEqual(prj.master_project_id, other.id)

            # An PRJ keeps its own project when that project goes on Hold.
            ProjectService.update_master_project(other, 'Proyek Lain', 'Hold', '')
            ok, error = move(other.id)
            self.assertTrue(ok, error)

            # Hold -> Selesai: allowed once every PRJ is Selesai or Void.
            ok, error = ProjectService.update_master_project(other, 'Proyek Lain', 'Completed', '')
            self.assertFalse(ok)
            DocumentService.review_doc(prj, '', 'Void')
            ok, error = ProjectService.update_master_project(other, 'Proyek Lain', 'Completed', '')
            self.assertTrue(ok, error)
            self.assertFalse(ProjectService.update_master_project(other, 'Proyek Lain', 'Aneh', '')[0])

    def test_master_project_pages_filter_on_status_and_kpi(self):
        with self.app.app_context():
            paused = MasterProject(name='Proyek Jeda', client_id=self.client_id, status='Online')
            running = MasterProject(name='Proyek Jalan', client_id=self.client_id, status='Online')
            empty = MasterProject(name='Proyek Kosong', client_id=self.client_id, status='Online')
            closed = MasterProject(name='Proyek Tutup', client_id=self.client_id, status='Completed')
            db.session.add_all([paused, running, empty, closed])
            db.session.commit()
            prj = self._prj(master_project_id=running.id)
            self._block(prj, 0, 2)  # realised 2 of 2 estimated days: Good
            self._prj(master_project_id=paused.id)  # no schedule: Need Data Input
            paused.status = 'Hold'
            db.session.commit()

        self._login(self.admin_id)

        def names(url):
            page = self.client.get(url).data.decode()
            return {name for name in ('Proyek Jeda', 'Proyek Jalan', 'Proyek Kosong', 'Proyek Tutup')
                    if f'</i>{name}</strong>' in page}

        self.assertEqual(names('/documents/master-projects'), {'Proyek Jeda', 'Proyek Jalan', 'Proyek Kosong'})
        self.assertEqual(names('/documents/master-projects?status=Hold'), {'Proyek Jeda'})
        self.assertEqual(names('/documents/master-projects?status=Online'), {'Proyek Jalan', 'Proyek Kosong'})
        self.assertEqual(names('/documents/master-projects?kpi=good'), {'Proyek Jalan'})
        self.assertEqual(names('/documents/master-projects?kpi=need'), {'Proyek Jeda'})
        self.assertEqual(names('/documents/master-projects?kpi=none'), {'Proyek Kosong'})
        self.assertEqual(names('/documents/master-projects?status=Online&kpi=need'), set())
        self.assertEqual(names('/documents/master-projects/completed'), {'Proyek Tutup'})
        self.assertEqual(names('/documents/master-projects/completed?kpi=none'), {'Proyek Tutup'})
        self.assertEqual(names('/documents/master-projects/completed?kpi=good'), set())
        page = self.client.get('/documents/master-projects').data.decode()
        self.assertIn('status-badge status-onhold', page)
        self.assertIn('<option value="Hold"', page)
        # Only Online projects can take a new PRJ.
        generator = self.client.get('/documents/generator?type=prj').data.decode()
        self.assertIn('Proyek Jalan', generator)
        self.assertNotIn('Proyek Jeda', generator)

    def test_prj_list_has_approval_filter_like_svc(self):
        with self.app.app_context():
            online, pending = self._prj(), self._prj()
            pending.status = pending.ho.status = 'Pending Approval'
            db.session.commit()
            online_link, pending_link = f'/documents/view/prj/{online.id}', f'/documents/view/prj/{pending.id}'
        self._login(self.admin_id)
        everything = self.client.get('/documents/prj').data.decode()
        self.assertIn('Needs Approval', everything)
        self.assertIn(online_link, everything)
        self.assertIn(pending_link, everything)
        approval = self.client.get('/documents/prj?status=Pending Approval').data.decode()
        self.assertIn(pending_link, approval)
        self.assertNotIn(online_link, approval)
        # An unknown filter value falls back to the whole active list.
        self.assertIn(online_link, self.client.get('/documents/prj?status=Completed').data.decode())

    def test_detail_acc_button_works_in_any_condition_except_hold(self):
        with self.app.app_context():
            bare, held = self._svc(1, 'Online').id, self._svc(2, 'Hold').id
            prj_id = self._prj().id  # no reports and no signed HO photo
        self._login(self.admin_id)

        bare_page = self.client.get(f'/documents/view/svc/{bare}').data.decode()
        self.assertRegex(bare_page, r'class="btn btn-sm btn-mac-success btn-apple-pill px-3"\s*onclick="openStatusConfirm')
        self.assertIn('The final report has not been uploaded yet.', bare_page)  # popup warns, does not block
        self.assertNotIn('data-action="approve"', bare_page)
        prj_page = self.client.get(f'/documents/view/prj/{prj_id}').data.decode()
        self.assertRegex(prj_page, r'btn-mac-success btn-apple-pill px-3"\s*onclick="openStatusConfirm')
        held_page = self.client.get(f'/documents/view/svc/{held}').data.decode()
        self.assertIn('A held document must be set back to Online before it can be approved', held_page)

        # What the button posts: status=Completed, accepted without any report or HO photo.
        self.client.post(f'/documents/review/svc/{bare}', data={'status': 'Completed'})
        self.client.post(f'/documents/review/prj/{prj_id}', data={'status': 'Completed'})
        with self.app.app_context():
            self.assertEqual(db.session.get(DocumentSVC, bare).status, 'Completed')
            done = db.session.get(DocumentPRJ, prj_id)
            self.assertEqual((done.status, done.ho.status), ('Completed', 'Approved'))

    def test_status_dropdown_follows_the_hold_rules(self):
        with self.app.app_context():
            online, pending, held = (self._svc(1, 'Online').id, self._svc(2, 'Pending Approval').id,
                                     self._svc(3, 'Hold').id)
        self._login(self.admin_id)
        online_page = self.client.get(f'/documents/view/svc/{online}').data.decode()
        self.assertIn('<option value="Hold" data-url', online_page)
        pending_page = self.client.get(f'/documents/view/svc/{pending}').data.decode()
        self.assertNotIn('<option value="Hold"', pending_page)
        held_page = self.client.get(f'/documents/view/svc/{held}').data.decode()
        self.assertIn('<option value="Hold" selected>', held_page)
        self.assertIn('<option value="Online">', held_page)
        self.assertNotIn('<option value="Completed"', held_page)
        self.assertIn('<option value="Void"', held_page)


if __name__ == '__main__':
    unittest.main()
