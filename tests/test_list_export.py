"""Excel export of the SVC & XPRJ and PRJ lists: follows the list filters and can be imported again.

A document takes one row per complaint line or work item, and the import joins those rows again.
"""

import os
import unittest
from datetime import date, datetime
from io import BytesIO
from unittest import mock

from openpyxl import Workbook, load_workbook
from werkzeug.datastructures import FileStorage

from run import create_app
from apps.models import ALL_MENUS, ROLE_OFFICE
from apps.core.registry import compute_doc_period
from apps.models import (
    db, User, Role, ClientSite, Document, DocumentSVC, DocumentXPRJ, DocumentPRJ, MasterProject, PRJItem, ScheduleBlock,
    STATUS_ONLINE, STATUS_PENDING, STATUS_HOLD, STATUS_DONE, STATUS_VOID,
)
from apps.services.export_service import BAND_FILLS
from apps.services.import_service import NEW_CLIENT_WARNING, SVC_IMPORT_COLUMNS, DocumentImportService
from apps.services.project_service import ProjectService
from apps.services.prj_import_service import PRJ_IMPORT_COLUMNS, PRJImportService

DAY = date(2026, 10, 5)
TODAY = datetime(2026, 11, 2, 9, 0)  # DAY is 28 days earlier
COMPLAINTS = 'AC tidak dingin\nAir menetes\nRemote rusak'


def _svc_no(no, label):
    return f'{no:03d}/10/{label}/26'


def _prj_no(no):
    return f'{no:03d}/PRJ/26'


class ListExportTests(unittest.TestCase):
    def setUp(self):
        self.original_env = {key: os.environ.get(key) for key in ('DATABASE_URL', 'APP_ENV')}
        os.environ['DATABASE_URL'] = 'sqlite:///:memory:'
        os.environ['APP_ENV'] = 'testing'
        self.app = self._new_app()
        self.client = self.app.test_client()
        with self.app.app_context():
            admin_id = self._user('Office', 'admin@example.test')
            self.tech_id = self._user('Technician', 'andi@example.test')
            alpha = ClientSite(company_name='PT Alpha', address='Jl. Alpha 1', client_type='Contract',
                               pic_name='Budi', pic_phone='081234567890')
            beta = ClientSite(company_name='PT Beta', address='Jl. Beta 2', client_type='On-Call',
                              pic_name='Sari', pic_phone='0215550000')
            db.session.add_all([alpha, beta])
            db.session.flush()

            period = compute_doc_period(DAY)
            svc_online = self._svc(DocumentSVC, 1, alpha, STATUS_ONLINE, period, notes='Catatan satu',
                                   reference='PO-1', customer_complaint=COMPLAINTS)
            self._svc(DocumentXPRJ, 2, beta, STATUS_PENDING, period)
            self._svc(DocumentSVC, 3, alpha, STATUS_HOLD, period)
            self._svc(DocumentSVC, 4, beta, STATUS_DONE, period, actual_completion=date(2026, 10, 20))
            self._svc(DocumentXPRJ, 5, alpha, STATUS_VOID, period, actual_completion=date(2026, 10, 21))

            prj_online = self._prj(1, alpha, STATUS_ONLINE, period)
            prj_online.items.extend([
                PRJItem(description='Tarik kabel', qty=2, uom='Meter', display_order=1),
                PRJItem(description='Pasang AP', qty=1, uom='Unit', display_order=2),
            ])
            prj_done = self._prj(2, beta, STATUS_DONE, period, actual_completion=date(2026, 10, 25))
            self._prj(3, alpha, STATUS_VOID, period)  # imported Void: no completion date
            self._prj(4, beta, STATUS_HOLD, period)
            db.session.flush()
            prj_done.ho.completion_date = date(2026, 10, 25)

            db.session.add_all([
                ScheduleBlock(user_id=self.tech_id, document_id=svc_online.id, start_date=DAY, days_duration=1),
                ScheduleBlock(user_id=self.tech_id, document_id=prj_online.id, start_date=DAY, days_duration=1),
            ])
            db.session.commit()
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

    @staticmethod
    def _new_app():
        """An app on its own empty in-memory database."""
        app = create_app()
        if not app.config['SQLALCHEMY_DATABASE_URI'].startswith('sqlite'):
            raise RuntimeError('Test harus memakai SQLite, bukan database server (ssj_db).')
        app.config['TESTING'] = True
        app.config['WTF_CSRF_ENABLED'] = False
        return app

    def _user(self, role_code, email):
        role = Role.query.filter_by(code=role_code).one()
        user = User(name=email.split('@')[0], email=email, password='x', role_id=role.id, menus=ALL_MENUS if role_code == ROLE_OFFICE else ())
        db.session.add(user)
        db.session.commit()
        return user.id

    def _svc(self, model, no, client, status, period, **fields):
        label = 'XPRJ' if model is DocumentXPRJ else 'SVC'
        fields.setdefault('customer_complaint', 'Keluhan')
        fields.setdefault('date', DAY)
        doc = model(doc_no=no, doc_period=period, doc_number_text=_svc_no(no, label),
                    client_id=client.id, status=status, task_description='Tugas', **fields)
        db.session.add(doc)
        return doc

    def _prj(self, no, client, status, period, **fields):
        fields.setdefault('date', DAY)
        prj = DocumentPRJ(doc_no=no, doc_period=period, doc_number_text=_prj_no(no),
                          client_id=client.id, title=f'Proyek {no}', so_number=f'SO-{no}', status=status, **fields)
        db.session.add(prj)
        return prj

    def _sheet(self, url):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200, url)
        self.assertIn('spreadsheetml', response.mimetype)
        return load_workbook(BytesIO(response.data)).active

    def _column(self, url, index):
        return {row[index] for row in self._sheet(url).iter_rows(min_row=2, values_only=True)}

    def test_svc_xprj_export_follows_the_list_filters(self):
        expected = {
            '': {_svc_no(1, 'SVC'), _svc_no(2, 'XPRJ')},
            '?status=hold': {_svc_no(3, 'SVC')},
            '?status=pending_approval': {_svc_no(2, 'XPRJ')},
            '?type=svc': {_svc_no(1, 'SVC')},
            '?q=beta': {_svc_no(2, 'XPRJ')},
            f'?tech_id={self.tech_id}': {_svc_no(1, 'SVC')},
            '?storage=1': {_svc_no(4, 'SVC'), _svc_no(5, 'XPRJ')},
            '?storage=1&type=xprj': {_svc_no(5, 'XPRJ')},
        }
        for query, numbers in expected.items():
            self.assertEqual(self._column(f'/documents/export{query}', 0), numbers, query)

    def test_prj_export_follows_the_list_filters(self):
        expected = {
            '': {_prj_no(1)},
            '?status=Hold': {_prj_no(4)},
            '?storage=1': {_prj_no(2), _prj_no(3)},
            '?storage=1&q=SO-3': {_prj_no(3)},
        }
        for query, numbers in expected.items():
            self.assertEqual(self._column(f'/documents/prj/export{query}', 2), numbers, query)

    def test_svc_xprj_export_imports_again_into_a_new_database(self):
        files = [self.client.get(f'/documents/export{query}').data
                 for query in ('', '?status=hold', '?storage=1')]

        fresh = self._new_app()
        with fresh.app_context():
            for data in files:
                ok, parsed, error = DocumentImportService.parse_excel(FileStorage(BytesIO(data), filename='export.xlsx'))
                self.assertTrue(ok, error)
                # The only warning of a fresh database: the clients are new there.
                self.assertEqual(parsed.total_errors, 0, [r.validation_errors for r in parsed.rows])
                self.assertLessEqual({note for r in parsed.rows for note in r.validation_warnings + r.client_notes},
                                     {NEW_CLIENT_WARNING})
                result = DocumentImportService.execute_import(parsed.to_dict()['rows'])
                self.assertEqual(result.errors, [])

            imported = {doc.doc_number_text: doc for doc in Document.query.all()}
            self.assertEqual({number: (doc.document_type, doc.status, doc.date) for number, doc in imported.items()}, {
                _svc_no(1, 'SVC'): ('svc', STATUS_ONLINE, DAY),
                _svc_no(2, 'XPRJ'): ('xprj', STATUS_ONLINE, DAY),  # Pending Approval imports as Not Completed
                _svc_no(3, 'SVC'): ('svc', STATUS_HOLD, DAY),
                _svc_no(4, 'SVC'): ('svc', STATUS_DONE, DAY),
                _svc_no(5, 'XPRJ'): ('xprj', STATUS_VOID, DAY),
            })
            online = imported[_svc_no(1, 'SVC')]
            self.assertEqual((online.client.company_name, online.client.address, online.client.pic_name,
                              online.client.pic_phone, online.client.client_type),
                             ('PT Alpha', 'Jl. Alpha 1', 'Budi', '081234567890', 'Contract'))
            self.assertEqual((online.reference, online.site_address), ('PO-1', 'Jl. Alpha 1'))
            self.assertEqual(online.customer_complaint, COMPLAINTS)
            self.assertEqual(imported[_svc_no(2, 'XPRJ')].customer_complaint, 'Keluhan')
            db.session.remove()

    def test_prj_export_imports_again_into_a_new_database(self):
        files = [self.client.get(f'/documents/prj/export{query}').data
                 for query in ('', '?status=Hold', '?storage=1')]

        fresh = self._new_app()
        with fresh.app_context():
            for data in files:
                ok, parsed, error = PRJImportService.parse_excel(FileStorage(BytesIO(data), filename='export.xlsx'))
                self.assertTrue(ok, error)
                # The only warning of a fresh database: a client is new there.
                self.assertEqual(parsed['total_errors'], 0, parsed['rows'])
                self.assertLessEqual({note for r in parsed['rows'] for note in r['validation_warnings'] + r['client_notes']},
                                     {NEW_CLIENT_WARNING}, parsed['rows'])
                result = PRJImportService.execute_import(parsed['rows'])
                self.assertEqual(result.errors, [])

            imported = {prj.doc_number_text: prj for prj in DocumentPRJ.query.all()}
            self.assertEqual({number: (prj.status, prj.title, prj.so_number, prj.ho.status)
                              for number, prj in imported.items()}, {
                _prj_no(1): (STATUS_ONLINE, 'Proyek 1', 'SO-1', STATUS_ONLINE),
                _prj_no(2): (STATUS_DONE, 'Proyek 2', 'SO-2', 'Approved'),
                _prj_no(3): (STATUS_VOID, 'Proyek 3', 'SO-3', STATUS_VOID),
                _prj_no(4): (STATUS_HOLD, 'Proyek 4', 'SO-4', STATUS_HOLD),
            })
            self.assertEqual(imported[_prj_no(2)].actual_completion, date(2026, 10, 25))
            items = sorted(imported[_prj_no(1)].items, key=lambda item: item.display_order)
            self.assertEqual([(item.description, item.qty, item.uom, item.display_order) for item in items],
                             [('Tarik kabel', 2, 'Meter', 1), ('Pasang AP', 1, 'Unit', 2)])
            self.assertEqual(imported[_prj_no(2)].items, [])
            db.session.remove()

    def test_cells_keep_dates_phone_numbers_and_item_lists(self):
        sheet = self._sheet('/documents/export?type=svc')
        headers = [cell.value for cell in sheet[1]]
        self.assertEqual(headers[:10], ['SVC Number', 'Document Date', 'Customer Name', 'Address', 'PIC', 'PIC Phone',
                                        'Reference Number', 'Visit Type', 'Status', 'Customer Complaint'])
        self.assertEqual(headers[:9], [header for _, header, _ in SVC_IMPORT_COLUMNS])
        self.assertIn('Internal Notes', headers[10:])  # shown, but no longer imported
        self.assertNotIn('Final Report', headers)  # text only: the report lives with its photos in SSJ
        self.assertIsInstance(sheet['B2'].value, datetime)
        self.assertEqual(sheet['B2'].number_format, 'DD/MM/YYYY')
        self.assertEqual(sheet['F2'].value, '081234567890')
        self.assertEqual(sheet.cell(2, headers.index('Technician') + 1).value, 'andi')

        sheet = self._sheet('/documents/prj/export')
        headers = [cell.value for cell in sheet[1]]
        self.assertEqual(headers[:14], [header for _, header, _ in PRJ_IMPORT_COLUMNS])
        self.assertEqual(headers[6:10], ['SO Number', 'Item Name', 'Qty', 'UOM'])
        rows = list(sheet.iter_rows(min_row=2, values_only=True))
        self.assertEqual([row[:3] + row[7:10] for row in rows], [
            (1, '001/HO/26', _prj_no(1), 'Tarik kabel', 2, 'Meter'),
            (1, '001/HO/26', _prj_no(1), 'Pasang AP', 1, 'Unit'),
        ])

    def test_one_row_per_complaint_with_a_grey_band_per_document(self):
        sheet = self._sheet('/documents/export')
        rows = list(sheet.iter_rows(min_row=2))
        # Same date: the newer document (XPRJ 2) comes first.
        self.assertEqual([(row[0].value, row[9].value) for row in rows], [
            (_svc_no(2, 'XPRJ'), 'Keluhan'),
            (_svc_no(1, 'SVC'), 'AC tidak dingin'),
            (_svc_no(1, 'SVC'), 'Air menetes'),
            (_svc_no(1, 'SVC'), 'Remote rusak'),
        ])
        # Every cell of a document shares one fill; the next document takes the next grey.
        colors = [fill.fgColor.rgb for fill in BAND_FILLS]
        self.assertEqual([{cell.fill.fgColor.rgb for cell in row} for row in rows],
                         [{colors[0]}, {colors[1]}, {colors[1]}, {colors[1]}])
        self.assertTrue(sheet.auto_filter.ref)

    def test_export_period_counts_back_from_today_on_the_document_date(self):
        with self.app.app_context():
            beta = ClientSite.query.filter_by(company_name='PT Beta').one()
            self._svc(DocumentSVC, 6, beta, STATUS_ONLINE, compute_doc_period(date(2026, 8, 1)), date=date(2026, 8, 1))
            self._svc(DocumentSVC, 7, beta, STATUS_ONLINE, compute_doc_period(date(2025, 12, 20)),
                      date=date(2025, 12, 20))
            self._prj(6, beta, STATUS_ONLINE, compute_doc_period(date(2025, 12, 20)), date=date(2025, 12, 20))
            db.session.commit()
        expected = {
            '60d': {_svc_no(1, 'SVC'), _svc_no(2, 'XPRJ')},
            'year': {_svc_no(1, 'SVC'), _svc_no(2, 'XPRJ'), _svc_no(6, 'SVC')},
            'all': {_svc_no(1, 'SVC'), _svc_no(2, 'XPRJ'), _svc_no(6, 'SVC'), _svc_no(7, 'SVC')},
        }
        with mock.patch('apps.documents.routes.wib_now', return_value=TODAY):
            for period, numbers in expected.items():
                self.assertEqual(self._column(f'/documents/export?period={period}', 0), numbers, period)
            self.assertEqual(self._column('/documents/prj/export?period=year', 2), {_prj_no(1)})
            self.assertEqual(self._column('/documents/prj/export?period=all', 2), {_prj_no(1), _prj_no(6)})
            response = self.client.get('/documents/export?period=60d')
        self.assertIn('SVC_XPRJ_Data_Active_60Days_', response.headers['Content-Disposition'])

    def test_list_pages_open_the_export_period_popup_with_the_filters_on_screen(self):
        expected = {
            '/documents/list?type=svc&q=alpha&page=2': '/documents/export?type=svc&amp;q=alpha',
            '/documents/list?storage=1': '/documents/export?storage=1',
            '/documents/prj?status=Hold': '/documents/prj/export?status=Hold',
            '/documents/prj?storage=1': '/documents/prj/export?storage=1',
        }
        for page, link in expected.items():
            body = self.client.get(page).get_data(as_text=True)
            self.assertIn('data-bs-target="#exportPeriodModal"', body, page)
            for period, label in (('60d', 'Last 60 days'), ('year', 'This year'), ('all', 'All')):
                self.assertIn(f'href="{link}&amp;period={period}"', body, page)
                self.assertIn(label, body, page)

    def test_master_project_export_lists_every_project_with_its_prjs(self):
        with self.app.app_context():
            alpha = ClientSite.query.filter_by(company_name='PT Alpha').one()
            big = MasterProject(name='Gedung Besar', client_id=alpha.id, status='Hold')
            small = MasterProject(name='Gedung Kecil', client_id=alpha.id, status='Completed')
            empty = MasterProject(name='Gedung Kosong', client_id=alpha.id, status='Online')
            db.session.add_all([big, small, empty])
            db.session.flush()
            # PRJ 1 Online (scheduled), 2 Completed, 3 Void (left out), 4 Hold.
            for prj in DocumentPRJ.query.all():
                prj.master_project_id = big.id
            self._prj(5, alpha, STATUS_DONE, compute_doc_period(DAY), master_project_id=small.id)
            db.session.commit()
            expected_kpis = {p.name: ProjectService.project_kpi(p) for p in (big, small)}

        for page in ('/documents/master-projects', '/documents/master-projects/completed'):
            self.assertIn('/documents/master-projects/export', self.client.get(page).get_data(as_text=True))
        response = self.client.get('/documents/master-projects/export')
        self.assertIn('Master_Project_Data_', response.headers['Content-Disposition'])
        sheet = self._sheet('/documents/master-projects/export')
        # As many PRJ columns as the project with the most PRJs.
        self.assertEqual([cell.value for cell in sheet[1]], [
            'Project Code', 'Estimated Work Days', 'Actual Work Days', 'Project KPI', 'PRJ #1', 'PRJ #2', 'PRJ #3'])
        big_kpi, small_kpi = expected_kpis['Gedung Besar'], expected_kpis['Gedung Kecil']
        self.assertEqual(list(sheet.iter_rows(min_row=2, values_only=True)), [
            ('Gedung Besar', big_kpi['total_est'], big_kpi['total_real'], 'Need Data Input',
             _prj_no(1), _prj_no(2), _prj_no(4)),
            ('Gedung Kecil', small_kpi['total_est'], small_kpi['total_real'], small_kpi['status'],
             _prj_no(5), None, None),
            ('Gedung Kosong', None, None, None, None, None, None),
        ])

    def test_client_export_lists_every_client_and_imports_again(self):
        with self.app.app_context():
            db.session.add(ClientSite(company_name='PT Gamma', client_type='XPRJ', latitude=-6.2, longitude=106.8))
            db.session.commit()
        self.assertIn('/system/clients/export', self.client.get('/system/clients').get_data(as_text=True))
        response = self.client.get('/system/clients/export')
        self.assertIn('Client_Data_', response.headers['Content-Disposition'])
        sheet = self._sheet('/system/clients/export')
        self.assertEqual([cell.value for cell in sheet[1]], [
            'No', 'Name', 'Address 1', 'Address 2', 'Address 3', 'PIC', 'PIC Phone', 'Customer Type', 'Lat', 'Long',
            'Document Count', 'Created Date'])
        self.assertEqual([row[:11] for row in sheet.iter_rows(min_row=2, values_only=True)], [
            (1, 'PT Alpha', 'Jl. Alpha 1', None, None, 'Budi', '081234567890', 'Contract', None, None, 5),
            (2, 'PT Beta', 'Jl. Beta 2', None, None, 'Sari', '0215550000', 'On-Call', None, None, 4),
            (3, 'PT Gamma', None, None, None, None, None, 'XPRJ', -6.2, 106.8, 0),
        ])
        self.assertIsInstance(sheet['L2'].value, datetime)

        fresh = self._new_app()
        with fresh.app_context():
            user_id = self._user('Office', 'baru@example.test')
        fresh_client = fresh.test_client()
        with fresh_client.session_transaction() as session:
            session['user_id'] = user_id
        fresh_client.post('/system/clients/import', data={'file': (BytesIO(response.data), 'klien.xlsx')},
                          content_type='multipart/form-data')
        with fresh.app_context():
            self.assertEqual(sorted(tuple(value or None for value in (
                c.company_name, c.address, c.pic_name, c.pic_phone, c.client_type, c.latitude, c.longitude))
                for c in ClientSite.query), [
                ('PT Alpha', 'Jl. Alpha 1', 'Budi', '081234567890', 'Contract', None, None),
                ('PT Beta', 'Jl. Beta 2', 'Sari', '0215550000', 'On-Call', None, None),
                ('PT Gamma', None, None, None, 'XPRJ', -6.2, 106.8),
            ])
            db.session.remove()


def _xlsx(headers, rows):
    workbook = Workbook()
    workbook.active.append(headers)
    for row in rows:
        workbook.active.append(row)
    output = BytesIO()
    workbook.save(output)
    output.seek(0)
    return FileStorage(output, filename='import.xlsx')


class MultiRowImportTests(unittest.TestCase):
    """Rows of one document follow each other: same number, or a blank number with a complaint or item."""

    SVC_HEADERS = [header for _, header, _ in SVC_IMPORT_COLUMNS] + ['Customer Complaint']
    # Columns are read by their header, so the items may come last.
    PRJ_HEADERS = ['No.', 'HO No.', 'PRJ No.', 'Job', 'Customer Name', 'Address', 'Reference Number',
                   'HO Status', 'Handover Date', 'Void Date', 'Notes', 'Item Description', 'Qty', 'UOM']

    def setUp(self):
        self.original_env = {key: os.environ.get(key) for key in ('DATABASE_URL', 'APP_ENV')}
        os.environ['DATABASE_URL'] = 'sqlite:///:memory:'
        os.environ['APP_ENV'] = 'testing'
        self.app = ListExportTests._new_app()
        self.context = self.app.app_context()
        self.context.push()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()
        for key, value in self.original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    @staticmethod
    def _svc(number, complaint, name='PT Alpha', address='Jl. Alpha 1'):
        return [number, '05/10/2026', name, address, 'Budi', None, None, 'On-Call', None, complaint]

    def test_svc_rows_with_the_same_or_a_blank_number_join_one_document(self):
        first, second = _svc_no(1, 'SVC'), _svc_no(2, 'SVC')
        ok, parsed, error = DocumentImportService.parse_excel(_xlsx(self.SVC_HEADERS, [
            self._svc(first, 'AC tidak dingin'),
            self._svc(first, 'Air menetes'),
            [None] * 9 + ['Remote rusak'],               # merged or not retyped: still document 1
            self._svc(second, None, name='PT Beta'),     # no complaint at all
            self._svc(second, 'Lampu mati\nSaklar rusak', name='PT Beta'),  # old style: lines in one cell
            [],
            self._svc(first, 'Muncul lagi'),            # not next to its other rows
        ]))
        self.assertTrue(ok, error)
        rows = parsed.to_dict()['rows']
        self.assertEqual([(r['doc_number_raw'], r['row_index'], r['row_end'], r['customer_complaint']) for r in rows], [
            (first, 2, 4, 'AC tidak dingin\nAir menetes\nRemote rusak'),
            (second, 5, 6, 'Lampu mati\nSaklar rusak'),
            (first, 8, 8, 'Muncul lagi'),
        ])
        self.assertEqual(rows[0]['validation_errors'], [])
        self.assertIn('Same document number as row 2', rows[2]['validation_errors'][0])
        self.assertEqual((parsed.total_parsed, parsed.total_errors), (3, 1))

        result = DocumentImportService.execute_import(rows[:2])
        self.assertEqual(result.errors, [])
        stored = {doc.doc_number_text: doc.customer_complaint for doc in Document.query.all()}
        self.assertEqual(stored, {first: 'AC tidak dingin\nAir menetes\nRemote rusak',
                                  second: 'Lampu mati\nSaklar rusak'})

    def test_svc_continuation_row_with_other_data_warns_and_keeps_the_first_row(self):
        number = _svc_no(1, 'SVC')
        ok, parsed, error = DocumentImportService.parse_excel(_xlsx(self.SVC_HEADERS, [
            self._svc(number, 'Satu'),
            self._svc(number, 'Dua', address='Jl. Lain 9'),
            [None, '06/10/2026', 'PT Lain'] + [None] * 6 + ['Tiga'],  # blank number, other client: a new (broken) row
        ]))
        self.assertTrue(ok, error)
        first, other = parsed.rows
        self.assertEqual((first.address, first.complaints), ('Jl. Alpha 1', ['Satu', 'Dua']))
        self.assertEqual(first.validation_warnings, ['Row 3: column Address differs from row 2; row 2 is used.'])
        self.assertIn('Document number is empty.', other.validation_errors)

    def test_prj_rows_carry_one_item_each(self):
        def prj(number, item, qty, uom, title='Instalasi CCTV'):
            return [None, None, number, title, 'PT Alpha', 'Jl. Alpha 1', 'SO-1', None, None, None, None,
                    item, qty, uom]
        first, second = _prj_no(1), _prj_no(2)
        ok, parsed, error = PRJImportService.parse_excel(_xlsx(self.PRJ_HEADERS, [
            prj(first, 'Tarik kabel', 2, 'Meter'),
            [None] * 11 + ['Pasang AP', None, None],     # blank number: same PRJ, Qty 1 and UOM Unit by default
            prj(first, 'Setting NVR', '1,5', 'Set'),     # not a whole number
            prj(second, None, None, None, title='Survey'),
            prj(first, 'Lagi', 1, 'Unit'),               # not next to its other rows: still the same PRJ
            prj('001/prj/2026', 'Beda tulis', 1, 'Unit'),  # same PRJ written differently: an error
        ]))
        self.assertTrue(ok, error)
        rows = parsed['rows']
        self.assertEqual([(r['prj_number_raw'], r['row_index'], r['row_end']) for r in rows],
                         [(first, 2, 6), (second, 5, 5), ('001/prj/2026', 7, 7)])
        self.assertEqual(rows[0]['items'], [
            {'description': 'Tarik kabel', 'qty': 2, 'uom': 'Meter'},
            {'description': 'Pasang AP', 'qty': 1, 'uom': 'Unit'},
            {'description': 'Setting NVR', 'qty': 1, 'uom': 'Set'},
            {'description': 'Lagi', 'qty': 1, 'uom': 'Unit'},
        ])
        self.assertEqual(rows[0]['validation_warnings'], ["Row 4: Qty '1,5' is not a whole number; 1 is used."])
        self.assertIn('but written differently', rows[2]['validation_errors'][0])
        # PT Alpha is new: every row says so, besides row 4's Qty warning.
        self.assertEqual([r['client_notes'] for r in rows], [[NEW_CLIENT_WARNING]] * 3)
        self.assertEqual((parsed['total_parsed'], parsed['total_warnings'], parsed['total_errors']), (3, 3, 1))

        result = PRJImportService.execute_import(rows[:2])
        self.assertEqual(result.errors, [])
        prj_one = DocumentPRJ.query.filter_by(doc_number_text=first).one()
        self.assertEqual([(i.description, i.qty, i.uom) for i in sorted(prj_one.items, key=lambda i: i.display_order)],
                         [('Tarik kabel', 2, 'Meter'), ('Pasang AP', 1, 'Unit'), ('Setting NVR', 1, 'Set'),
                          ('Lagi', 1, 'Unit')])

    def test_old_files_without_the_new_columns_still_import(self):
        ok, parsed, error = DocumentImportService.parse_excel(_xlsx(self.SVC_HEADERS[:9], [
            self._svc(_svc_no(1, 'SVC'), None)[:9],
        ]))
        self.assertTrue(ok, error)
        self.assertEqual(parsed.rows[0].complaints, [])
        ok, parsed, error = PRJImportService.parse_excel(_xlsx(self.PRJ_HEADERS[:11], [
            [None, None, _prj_no(1), 'Survey', 'PT Alpha', 'Jl. Alpha 1', None, None, None, None, None],
        ]))
        self.assertTrue(ok, error)
        self.assertEqual(parsed['rows'][0]['items'], [])


if __name__ == '__main__':
    unittest.main()
