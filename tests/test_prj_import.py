"""Import PRJ & HO from the recap Excel, plus the raw-number rule of the SVC/XPRJ import."""

import json
import os
import tempfile
import unittest
from datetime import date, datetime
from io import BytesIO

import pandas as pd

from run import create_app
from apps.models import ALL_MENUS, ROLE_OFFICE
from apps.models import db, User, Role, ClientSite, DocumentPRJ, DocumentSVC, DocumentXPRJ
from apps.core.registry import format_document_number, get_next_sequence_number
from apps.services.import_service import clean_cell_text as _cell_text
from apps.services.prj_import_service import parse_recap_date, parse_prj_number

# Headers of the recap, with mixed casing: headers are matched without case.
RECAP_HEADERS = ['No.', 'HO Number', 'PRJ Number', 'job', 'Customer name', 'address', 'SO Number',
                 'Item Name', 'Qty', 'UOM', 'HO Status', 'Handover Date', 'Void Date', 'Notes']
# The same recap with alternative header names, and the item columns moved after Notes.
OLD_RECAP_HEADERS = ['No.', 'HO No.', 'PRJ No.', 'Job Title', 'Client Name', 'Site Address', 'Reference Number',
                     'HO Status', 'Handover Date', 'Void Date', 'Remarks',
                     'Item Description', 'Quantity', 'Unit']


def recap_row(no, ho, prj, title, client, address, so, status=None, handover=None, void=None, notes=None,
              item=None, qty=None, uom=None):
    """One row of the PRJ recap, columns A-N, from its cells in the order of the older recap."""
    return [no, ho, prj, title, client, address, so, item, qty, uom, status, handover, void, notes]


def recap_file(rows):
    """The recap with each row given as recap_row's arguments."""
    return excel_file(RECAP_HEADERS, [recap_row(*row) for row in rows])


SVC_HEADERS = ['SVC Number', 'Document Date', 'Customer Name', 'Address', 'PIC', 'PIC Phone',
               'Reference Number', 'Visit Type', 'Status']


def svc_row(day, number, name, address=None, pic=None, phone=None, ref=None, visit=None, status=None):
    """One row of the SVC/XPRJ import, columns A-I."""
    return [number, day, name, address, pic, phone, ref, visit, status]


def excel_file(headers, rows):
    output = BytesIO()
    pd.DataFrame(rows, columns=headers).to_excel(output, index=False)
    output.seek(0)
    return output


class PRJImportTests(unittest.TestCase):
    def setUp(self):
        self.original_env = {key: os.environ.get(key) for key in ('DATABASE_URL', 'APP_ENV', 'SSJ_DATA_ROOT')}
        self.data_root = tempfile.TemporaryDirectory()
        os.environ['DATABASE_URL'] = 'sqlite:///:memory:'
        os.environ['APP_ENV'] = 'testing'
        os.environ['SSJ_DATA_ROOT'] = self.data_root.name  # staging dir lives under the data root
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
            db.session.add_all([admin, ClientSite(company_name='PT Lama', address='Alamat Lama')])
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

    def _recap(self):
        return recap_file([
            [1, '82/HO/22', '82/PRJ/22', 'Instalasi CCTV', 'pt lama', 'Jl. Site A\r\nJakarta', 'SO-001',
             'Approved', datetime(2022, 6, 23), None, 'Approved dengan catatan: kabel kurang'],
            [2, '82/HO/23', '82/PRJ/23', 'Instalasi Fire Alarm', 'PT Baru', 'Jl. Baru', None,
             None, None, None, None],
            # Tanggal Void wins over "Approved"; HO number differs from the PRJ number.
            [3, '128/HO/26', '130/PRJ/26', None, 'pt baru', 'Jl. Lain', 'SO-130',
             'Approved', '23 Jun 26', '5 Agu 26', None],
            [4, None, '246/09/XPRJ/26', 'Bukan PRJ', 'PT Lama', None, 'SO-X', None, None, None, None],
            [None, None, None, None, None, None, None, None, None, None, None],
        ])

    def _upload(self, file):
        return self.client.post('/documents/prj/import/upload', data={'excel_file': (file, 'rekap.xlsx')},
                                content_type='multipart/form-data')

    def test_parsers(self):
        self.assertEqual(parse_prj_number('82/PRJ/22'), (82, 2022))
        self.assertEqual(parse_prj_number('130/PRJ/2026'), (130, 2026))
        with self.assertRaises(ValueError):
            parse_prj_number('246/09/XPRJ/26')
        self.assertEqual(parse_recap_date('23 Jun 26'), date(2026, 6, 23))
        self.assertEqual(parse_recap_date('5 Agustus 2026'), date(2026, 8, 5))
        self.assertEqual(parse_recap_date('2022-06-23 00:00:00'), date(2022, 6, 23))
        self.assertEqual(parse_recap_date('23/06/2026'), date(2026, 6, 23))
        self.assertIsNone(parse_recap_date('-'))
        with self.assertRaises(ValueError):
            parse_recap_date('besok')

    def test_client_keys_ignore_spacing_punctuation_and_phone_prefix(self):
        from apps.services.import_service import _name_key, _phone_key
        self.assertEqual({_name_key(name) for name in ('PT. X', 'PT X', 'pt  x', 'ptx', ' P.T X ')}, {'ptx'})
        self.assertNotEqual(_name_key('Pak Budi'), _name_key('Budi'))  # titles are not stripped
        self.assertEqual({_phone_key(phone) for phone in ('+62 812-3456', '62 8123456', '0812 3456', '8123456')},
                         {'08123456'})
        self.assertEqual(_phone_key(''), '')

    def test_cell_text_removes_excel_line_breaks(self):
        raw = 'JL. Anggur 1 No. 2 , Rawa Buaya, Cengkareng _x000D_\nJakarta Barat,  DKI Jakarta_x000D_'
        self.assertEqual(_cell_text(raw), 'JL. Anggur 1 No. 2, Rawa Buaya, Cengkareng Jakarta Barat, DKI Jakarta')
        self.assertEqual(_cell_text('Baris satu _x000D_\n\n  baris   dua ', multiline=True), 'Baris satu\nbaris dua')
        self.assertEqual(_cell_text(float('nan')), '')

    def test_list_page_offers_create_and_import(self):
        page = self.client.get('/documents/prj')
        self.assertIn(b'Create / Import', page.data)
        self.assertIn(b'prjImportModal', page.data)
        self.assertIn(b'Handover Date', page.data)

    def test_recap_import_creates_prj_and_ho_with_status_from_recap(self):
        response = self._upload(self._recap())
        self.assertEqual(response.status_code, 302)
        self.assertIn('/documents/prj/import/preview', response.headers['Location'])

        preview = self.client.get('/documents/prj/import/preview')
        self.assertEqual(preview.status_code, 200)
        self.assertIn(b'82/PRJ/23', preview.data)
        self.assertIn('XPRJ; use the import'.encode(), preview.data)
        # "pt lama" at another address than PT Lama's is offered as a new client.
        self.assertIn('Will be added as a new client.'.encode(), preview.data)
        with self.app.app_context():
            lama_id = ClientSite.query.filter_by(company_name='PT Lama').one().id

        # Rows 2-4 are valid; row 5 (XPRJ) cannot be imported even when submitted.
        # Row 2 is pointed at the existing PT Lama in the preview's client picker.
        response = self.client.post('/documents/prj/import/confirm', data={
            'selected_rows': ['2', '3', '4', '5'],
            'title_4': 'Judul Diperbaiki',
            'client_choice_2': str(lama_id),
        })
        self.assertEqual(response.status_code, 302)
        self.assertIn('/documents/prj', response.headers['Location'])

        with self.app.app_context():
            self.assertEqual(DocumentPRJ.query.count(), 3)
            self.assertEqual(DocumentXPRJ.query.count(), 0)

            done = DocumentPRJ.query.filter_by(doc_no=82, doc_period='2022').one()
            self.assertEqual(done.display_no, '82/PRJ/22')
            self.assertEqual(done.date, date(2022, 1, 1))
            self.assertEqual(done.status, 'Completed')
            self.assertEqual(done.actual_completion, date(2022, 6, 23))
            # The picked client, left unchanged; the PRJ keeps the recap's address.
            self.assertEqual((done.client.company_name, done.client.address), ('PT Lama', 'Alamat Lama'))
            self.assertEqual(done.site_address, 'Jl. Site A Jakarta')
            self.assertEqual(done.notes, 'Approved dengan catatan: kabel kurang')
            self.assertEqual((done.estimated_calendar_days, done.urgency_level), (5, 1))
            self.assertEqual(done.items, [])
            self.assertEqual(done.ho.display_no, '82/HO/22')
            self.assertEqual(done.ho.status, 'Approved')
            self.assertEqual(done.ho.approved_at, datetime(2022, 6, 23))
            self.assertEqual(done.ho.uploaded_at, datetime(2022, 6, 23))
            self.assertEqual(done.ho.completion_date, date(2022, 6, 23))

            online = DocumentPRJ.query.filter_by(doc_no=82, doc_period='2023').one()
            self.assertEqual(online.status, 'Online')
            self.assertEqual(online.so_number, '-')
            self.assertIsNone(online.actual_completion)
            self.assertEqual(online.ho.status, 'Online')
            self.assertEqual(online.ho.display_no, '82/HO/23')
            online_ho_id = online.ho.id

            void = DocumentPRJ.query.filter_by(doc_no=130).one()
            self.assertEqual(void.status, 'Void')
            self.assertEqual(void.title, 'Judul Diperbaiki')
            self.assertIsNone(void.actual_completion)  # Tanggal Void only decides the status
            self.assertEqual(void.ho.status, 'Void')
            self.assertEqual(void.ho.display_no, '128/HO/26')
            self.assertIsNone(void.ho.approved_at)

            # "PT Baru" (Jl. Baru) and "pt baru" (Jl. Lain): the same name at two addresses is two new clients.
            self.assertEqual(ClientSite.query.count(), 3)
            self.assertNotEqual(online.client, void.client)
            self.assertEqual((online.client.company_name, online.client.address), ('PT Baru', 'Jl. Baru'))
            self.assertEqual((void.client.company_name, void.client.address), ('pt baru', 'Jl. Lain'))
            self.assertEqual(void.site_address, 'Jl. Lain')

            # A new PRJ continues after the highest imported number of its year.
            self.assertEqual(get_next_sequence_number('prj', date(2026, 10, 1)), 131)

        # Imported PRJs have no items yet; their pages and printouts still render.
        for prj in (done, online, void):
            self.assertEqual(self.client.get(f'/documents/view/prj/{prj.id}').status_code, 200)
            self.assertEqual(self.client.get(f'/documents/print/prj/{prj.id}').status_code, 200)
        self.assertEqual(self.client.get(f'/documents/ho/{online_ho_id}/print').status_code, 200)
        self.assertEqual(self.client.get('/documents/prj?storage=1').status_code, 200)
        self.assertEqual(self.client.get('/documents/ho?storage=1').status_code, 200)

    def test_already_imported_numbers_are_errors_in_preview(self):
        self._upload(self._recap())
        self.client.post('/documents/prj/import/confirm', data={'selected_rows': ['2', '3', '4']})

        self._upload(self._recap())
        preview = self.client.get('/documents/prj/import/preview')
        self.assertIn('of period 2022 already exists in the database'.encode(), preview.data)
        self.client.post('/documents/prj/import/confirm', data={'selected_rows': ['2', '3', '4']})
        with self.app.app_context():
            self.assertEqual(DocumentPRJ.query.count(), 3)

    def test_svc_import_keeps_number_text_from_excel(self):
        self._svc_upload([
            svc_row('15/09/2026', '7/09/SVC/26', 'PT Lama', visit='On-Call'),
            svc_row('16/09/2026', '246/09/XPRJ/26', 'PT Lama', visit='XPRJ'),
        ])
        self.client.post('/documents/import/confirm', data={'selected_rows': ['2', '3']})
        with self.app.app_context():
            self.assertEqual(DocumentSVC.query.one().display_no, '7/09/SVC/26')
            self.assertEqual(DocumentXPRJ.query.one().display_no, '246/09/XPRJ/26')

    def test_svc_import_status_column_sorts_into_online_and_storage(self):
        file = excel_file(SVC_HEADERS, [
            svc_row('01/09/2026', '1/09/SVC/26', 'PT Lama', status='Void/Cancelled'),
            svc_row('02/09/2026', '2/09/XPRJ/26', 'PT Lama', status='Completed'),
            svc_row('03/09/2026', '3/09/SVC/26', 'PT Lama', status='Not Completed'),
            svc_row('04/09/2026', '4/09/SVC/26', 'PT Lama', status='Hold'),
            svc_row('05/09/2026', '5/09/SVC/26', 'PT Lama', ref='PO-55'),
            svc_row('06/09/2026', '6/09/SVC/26', 'PT Lama', status='Unknown'),
        ])
        self.client.post('/documents/import/upload', data={'excel_file': (file, 'svc.xlsx')},
                         content_type='multipart/form-data')
        preview = self.client.get('/documents/import/preview')
        self.assertIn("Status &#39;Unknown&#39; is not recognised".encode(), preview.data)

        self.client.post('/documents/import/confirm', data={'selected_rows': ['2', '3', '4', '5', '6', '7']})
        with self.app.app_context():
            statuses = {doc.doc_no: doc.status for doc in DocumentSVC.query.all() + DocumentXPRJ.query.all()}
            self.assertEqual(statuses, {1: 'Void', 2: 'Completed', 3: 'Online', 4: 'Hold', 5: 'Online', 6: 'Online'})
            self.assertEqual(DocumentSVC.query.filter_by(doc_no=5).one().reference, 'PO-55')
            void_id = DocumentSVC.query.filter_by(doc_no=1).one().id
            done_id = DocumentXPRJ.query.filter_by(doc_no=2).one().id
            # No completion date in the Excel: finished documents complete on their own date.
            completions = {doc.doc_no: doc.actual_completion for doc in DocumentSVC.query.all() + DocumentXPRJ.query.all()}
            self.assertEqual(completions, {1: date(2026, 9, 1), 2: date(2026, 9, 2), 3: None, 4: None, 5: None, 6: None})

        active = self.client.get('/documents/list').data.decode()
        storage = self.client.get('/documents/list?storage=1').data.decode()
        # The storage date filter works on that completion date.
        filtered = self.client.get('/documents/list?storage=1&start_date=2026-09-02&end_date=2026-09-30').data.decode()
        self.assertIn('2/09/XPRJ/26', filtered)
        self.assertNotIn('1/09/SVC/26', filtered)
        for number in ('1/09/SVC/26', '2/09/XPRJ/26'):
            self.assertIn(number, storage)
            self.assertNotIn(number, active)
        for number in ('3/09/SVC/26', '5/09/SVC/26'):
            self.assertIn(number, active)
            self.assertNotIn(number, storage)
        # Hold is listed only under its own filter.
        held = self.client.get('/documents/list?status=hold').data.decode()
        self.assertNotIn('4/09/SVC/26', active)
        self.assertNotIn('4/09/SVC/26', storage)
        self.assertIn('4/09/SVC/26', held)
        self.assertNotIn('3/09/SVC/26', held)
        self.assertEqual(self.client.get(f'/documents/view/svc/{void_id}').status_code, 200)
        self.assertEqual(self.client.get(f'/documents/view/xprj/{done_id}').status_code, 200)

    def _svc_upload(self, rows):
        self.client.post('/documents/import/upload', data={'excel_file': (excel_file(SVC_HEADERS, rows), 'svc.xlsx')},
                         content_type='multipart/form-data')

    def test_import_without_penyimpanan_uses_only_existing_clients(self):
        with self.app.app_context():
            role = Role.query.filter_by(code='Office').one()
            clerk = User(name='Dok', email='dok@example.test', password='x', role_id=role.id, menus=('documents',))
            db.session.add(clerk)
            db.session.commit()
            clerk_id = clerk.id
        with self.client.session_transaction() as session:
            session['user_id'] = clerk_id

        self._svc_upload([
            svc_row('01/09/2026', '1/09/SVC/26', 'PT Lama', 'Alamat Lama', status='Completed'),
            svc_row('02/09/2026', '2/09/SVC/26', 'PT Asing', 'Jl. Asing', status='Completed'),
            svc_row('03/09/2026', '3/09/SVC/26', 'PT Lama', 'Alamat Lama', status='Completed'),
        ])
        preview = self.client.get('/documents/import/preview').data.decode()
        self.assertIn('The client is not registered yet', preview)
        self.assertNotIn('name="client_name_', preview)  # the client is picked, never typed
        # The PT Asing row is refused on confirm as well while no existing client is picked.
        self.client.post('/documents/import/confirm', data={'selected_rows': ['2', '3', '4']})
        with self.app.app_context():
            self.assertEqual(sorted(doc.doc_no for doc in DocumentSVC.query.all()), [1, 3])
            self.assertEqual(ClientSite.query.count(), 1)
            lama_id = ClientSite.query.one().id

        self._upload(recap_file([
            [1, '1/HO/26', '1/PRJ/26', 'Kerja A', 'PT Lama', 'Alamat Lama', None, None, None, None, None],
            [2, '2/HO/26', '2/PRJ/26', 'Kerja B', 'PT Baru', 'Jl. Baru', None, None, None, None, None],
            [3, '3/HO/26', '3/PRJ/26', 'Kerja C', 'PT Lama', 'Alamat Lama', None, None, None, None, None],
        ]))
        self.assertIn('The client is not registered yet', self.client.get('/documents/prj/import/preview').data.decode())
        # Picking an existing client for the PT Baru row lets it import.
        self.client.post('/documents/prj/import/confirm', data={'selected_rows': ['2', '3', '4'],
                                                                'client_choice_3': str(lama_id)})
        with self.app.app_context():
            self.assertEqual(sorted(prj.doc_no for prj in DocumentPRJ.query.all()), [1, 2, 3])
            self.assertEqual({prj.client_id for prj in DocumentPRJ.query}, {lama_id})
            self.assertEqual(ClientSite.query.count(), 1)

    def test_import_rejects_a_sheet_missing_any_popup_column(self):
        file = excel_file(['Document Date', 'SVC Number', 'Customer Name', 'PIC', 'PIC Phone', 'Status'],
                          [['01/09/2026', '1/09/SVC/26', 'PT Lama', None, None, 'Completed']])
        page = self.client.post('/documents/import/upload', data={'excel_file': (file, 'svc.xlsx')},
                                content_type='multipart/form-data', follow_redirects=True).data.decode()
        self.assertIn('These columns were not found: D (Address), G (Reference Number), H (Visit Type)', page)
        self.assertEqual(self.client.get('/documents/import/preview').status_code, 302)  # nothing staged

        for left_out, message in (('Void Date', 'M (Void Date)'),
                                  # Qty/UOM without a known Item Name header would drop every item silently.
                                  ('Item Name', 'H (Item Name)')):
            headers = [header for header in RECAP_HEADERS if header != left_out]
            page = self.client.post('/documents/prj/import/upload',
                                    data={'excel_file': (excel_file(headers, [[1] + [None] * 12]), 'rekap.xlsx')},
                                    content_type='multipart/form-data', follow_redirects=True).data.decode()
            self.assertIn(f'These columns were not found: {message}.', page)

    def test_svc_import_accepts_an_alternative_status_header(self):
        # The status column may be headed "Job Status"; filler "0" in client cells.
        headers = SVC_HEADERS[:-1] + ['Job Status']
        file = excel_file(headers, [
            svc_row('01/09/2026', '1/09/SVC/26', 'PT Lama', '0', '0', '0', '0', '0', 'Completed'),
            svc_row('02/09/2026', '2/09/SVC/26', 'PT Lama', visit='On-Call', status='Void/Cancelled'),
            svc_row('03/09/2026', '3/09/SVC/26', 'PT Lama', visit='On-Call', status='Hold'),
        ])
        self.client.post('/documents/import/upload', data={'excel_file': (file, 'svc.xlsx')},
                         content_type='multipart/form-data')
        preview = self.client.get('/documents/import/preview').data.decode()
        self.assertNotIn('not recognised', preview)  # "0" is read as a blank cell
        self.client.post('/documents/import/confirm', data={'selected_rows': ['2', '3', '4']})
        with self.app.app_context():
            statuses = {doc.doc_no: doc.status for doc in DocumentSVC.query.all()}
            self.assertEqual(statuses, {1: 'Completed', 2: 'Void', 3: 'Hold'})
            done = DocumentSVC.query.filter_by(doc_no=1).one()
            self.assertEqual((done.client.company_name, done.client.pic_name), ('PT Lama', None))
            self.assertIsNone(done.reference)

    def test_svc_import_matches_clients_on_name_type_and_pic(self):
        with self.app.app_context():
            db.session.add(ClientSite(company_name='PT X', client_type='Contract',
                                      pic_name='Budi', pic_phone='08121111', address='Jl. Master No. 1'))
            db.session.commit()
            master_id = ClientSite.query.filter_by(company_name='PT X').one().id

        mc = 'Contract'
        self._svc_upload([
            # Name without punctuation/spaces/case, +62 as 0; the address is never compared.
            svc_row('02/09/2026', '2/09/SVC/26', 'PT. X', 'Jalan Lain 9', 'Budi', '+62 812-1111', 'REF-2', mc),
            # A blank PIC phone is not compared.
            svc_row('03/09/2026', '3/09/SVC/26', 'pt  x', None, ' budi ', visit='contract'),
            # Another PIC is a new client (picked back to PT X below).
            svc_row('04/09/2026', '4/09/SVC/26', 'PT X', 'Jl. Master No. 1', 'Andi', visit=mc),
            # Another type is a new client.
            svc_row('05/09/2026', '5/09/SVC/26', 'PT X', 'Jl. Master No. 1', 'Budi', visit='SURVEY'),
            # XPRJ: any type.
            svc_row('06/09/2026', '6/09/XPRJ/26', 'PT X', 'Jl. Lain', 'Budi', '08121111', visit='XPRJ'),
            svc_row('07/09/2026', '7/09/XPRJ/26', 'PT X', None, 'Rina', visit='XPRJ'),
            # A blank Visit Type is not compared: PT X (Contract) and the new Survey one match.
            svc_row('08/09/2026', '8/09/SVC/26', 'PT X', 'Jl. Master No. 1', 'Budi', status='Completed'),
            # Another PIC phone is a new client.
            svc_row('09/09/2026', '9/09/SVC/26', 'PT X', None, 'Budi', '0899', visit=mc),
            svc_row('10/09/2026', '10/09/SVC/26', 'PT Q'),
        ])
        preview = self.client.get('/documents/import/preview').data.decode()
        self.assertIn('PT X · Survey · Budi · Jl. Master No. 1', preview)
        self.assertIn('2 clients match; using PT X · Contract · Budi (08121111) · '
                      'Jl. Master No. 1. Change it if needed.', preview)
        self.assertIn('Will be added as a new client.', preview)
        self.assertIn('Visit Type is empty although the document is not completed', preview)
        self.assertNotIn('Address is empty', preview)
        self.assertNotIn('New Client Will Be Created', preview)  # the new-client box is gone
        self.assertIn('class="btn btn-sm btn-light client-pick-btn', preview)

        self.client.post('/documents/import/confirm', data={
            'selected_rows': [str(i) for i in range(2, 11)], 'client_choice_4': str(master_id)})
        with self.app.app_context():
            def doc(model, doc_no):
                return model.query.filter_by(doc_no=doc_no).one()

            master = db.session.get(ClientSite, master_id)
            for doc_no in (2, 3, 4, 8):
                self.assertEqual(doc(DocumentSVC, doc_no).client, master, doc_no)
            self.assertEqual(doc(DocumentXPRJ, 6).client, master)
            # The document keeps the Excel's own address and reference.
            self.assertEqual((doc(DocumentSVC, 2).site_address, doc(DocumentSVC, 2).reference), ('Jalan Lain 9', 'REF-2'))
            survey = doc(DocumentSVC, 5).client
            self.assertEqual((survey.client_type, survey.pic_name, survey.address), ('Survey', 'Budi', 'Jl. Master No. 1'))
            self.assertEqual((doc(DocumentXPRJ, 7).client.client_type, doc(DocumentXPRJ, 7).client.pic_name),
                             ('XPRJ', 'Rina'))
            other_phone = doc(DocumentSVC, 9).client
            self.assertEqual((other_phone.client_type, other_phone.pic_name, other_phone.pic_phone), (mc, 'Budi', '0899'))
            # PT Lama (setUp) + master + Survey, Rina (XPRJ), Budi 0899, PT Q; Andi was picked as PT X.
            self.assertEqual(ClientSite.query.count(), 6)
            self.assertEqual(ClientSite.query.filter_by(pic_name='Andi').count(), 0)
            self.assertEqual((master.pic_name, master.pic_phone, master.address),
                             ('Budi', '08121111', 'Jl. Master No. 1'))  # existing client untouched

    def test_prj_import_hold_and_client_matched_on_name(self):
        with self.app.app_context():
            db.session.add_all([ClientSite(company_name='PT Kembar', client_type='Contract',
                                           address='Jl. A No. 1', pic_name='Ani'),
                                ClientSite(company_name='PT Kembar', client_type='On-Call', address='Jl. B No. 2')])
            db.session.commit()
            first = ClientSite.query.filter_by(address='Jl. A No. 1').one().id
            site_b = ClientSite.query.filter_by(address='Jl. B No. 2').one().id
        self._upload(recap_file([
            # Same name twice: the address picks one; punctuation, case and line breaks do not count.
            # "Pending" anywhere in Notes puts the PRJ on Hold.
            [1, '10/HO/26', '10/PRJ/26', 'Pasang', 'PT Kembar', 'jl. b,\r\nno 2', None, None, None, None,
             'Barang PENDING dari vendor'],
            # Another address than both: a new client.
            [2, None, '11/PRJ/26', 'Pasang', 'pt kembar', 'Jl. C', None, None, None, None, None],
            # No address: all three match (with row 11's new one), so the client created first, with a warning.
            [3, None, '12/PRJ/26', 'Pasang', 'PT. Kembar', None, None, None, None, None, None],
        ]))
        preview = self.client.get('/documents/prj/import/preview').data.decode()
        # Counted on the check icon's tooltip (the picker's row data repeats the text).
        self.assertEqual(preview.count('title="3 clients match'), 1)
        self.assertEqual(preview.count('title="Will be added as a new client.'), 1)
        self.assertIn('PT Kembar · Contract · Ani · Jl. A No. 1', preview)
        self.client.post('/documents/prj/import/confirm', data={'selected_rows': ['2', '3', '4']})
        with self.app.app_context():
            held = DocumentPRJ.query.filter_by(doc_no=10).one()
            self.assertEqual((held.status, held.ho.status), ('Hold', 'Hold'))
            self.assertEqual(held.client_id, site_b)
            held_id = held.id
            at_c = DocumentPRJ.query.filter_by(doc_no=11).one()
            self.assertNotIn(at_c.client_id, (first, site_b))
            self.assertEqual((at_c.client.address, at_c.site_address), ('Jl. C', 'Jl. C'))
            self.assertEqual(DocumentPRJ.query.filter_by(doc_no=12).one().client_id, first)
            self.assertEqual(ClientSite.query.filter(ClientSite.company_name.ilike('pt kembar')).count(), 3)
        self.assertIn('10/PRJ/26', self.client.get('/documents/prj?status=Hold').data.decode())
        self.assertNotIn('10/PRJ/26', self.client.get('/documents/prj').data.decode())
        self.assertIn('<option value="Hold" selected>', self.client.get(f'/documents/view/prj/{held_id}').data.decode())
        self.assertIn('10/HO/26', self.client.get('/documents/ho?status=Hold').data.decode())
        self.assertNotIn('10/HO/26', self.client.get('/documents/ho').data.decode())

    def test_prj_import_reads_filler_address_as_blank(self):
        self._upload(recap_file([
            [1, '20/HO/26', '20/PRJ/26', 'Pasang', 'PT Nol', '0', None, None, None, None, None],
            [2, '21/HO/26', '21/PRJ/26', 'Pasang', 'PT Lama', '-', None, None, None, None, None],
        ]))
        self.client.post('/documents/prj/import/confirm', data={'selected_rows': ['2', '3']})
        with self.app.app_context():
            self.assertEqual(DocumentPRJ.query.filter_by(doc_no=20).one().site_address, '')
            self.assertIsNone(ClientSite.query.filter_by(company_name='PT Nol').one().address)
            # "PT Lama" is the existing client; with no address of its own the PRJ takes the client's.
            self.assertEqual(ClientSite.query.filter_by(company_name='PT Lama').count(), 1)
            self.assertEqual(DocumentPRJ.query.filter_by(doc_no=21).one().site_address, 'Alamat Lama')

    def test_prj_import_items_are_edited_in_the_preview(self):
        no_status = (None, None, None, None)  # HO Status, Handover Date, Void Date, Notes
        self._upload(recap_file([
            [1, None, '40/PRJ/26', 'Pasang', 'PT Lama', 'Alamat Lama', None, *no_status, 'Kamera', '2', 'Unit'],
            [2, None, '40/PRJ/26', 'Pasang', 'PT Lama', 'Alamat Lama', None, *no_status, 'Kabel', '10', 'Meter'],
            [3, None, '41/PRJ/26', 'Pasang', 'PT Lama', 'Alamat Lama', None, *no_status, 'NVR', '1', 'Set'],
        ]))
        preview = self.client.get('/documents/prj/import/preview').data.decode()
        self.assertIn('2 items', preview)  # a count with an edit button, not the whole list
        self.assertNotIn('Kamera &mdash;', preview)
        self.assertIn('name="items_2"', preview)

        edited = [{'description': ' Kamera Dome ', 'qty': '3', 'uom': ''},
                  {'description': '', 'qty': 1, 'uom': 'Unit'},          # blank line: dropped
                  {'description': 'Tambahan', 'qty': 1, 'uom': 'Titik'}]
        self.client.post('/documents/prj/import/confirm', data={
            'selected_rows': ['2', '4'],
            'items_2': json.dumps(edited),
            'items_4': json.dumps([{'description': 'NVR', 'qty': 1.5, 'uom': 'Set'}]),  # not a whole number
        })
        with self.app.app_context():
            prj = DocumentPRJ.query.filter_by(doc_no=40).one()
            self.assertEqual([(i.description, i.qty, i.uom) for i in sorted(prj.items, key=lambda i: i.display_order)],
                             [('Kamera Dome', 3, 'Unit'), ('Tambahan', 1, 'Titik')])
            self.assertIsNone(DocumentPRJ.query.filter_by(doc_no=41).first())

    def test_prj_import_new_client_takes_the_first_address_given(self):
        self._upload(recap_file([
            [1, None, '30/PRJ/26', 'Pasang', 'PT Isi', None, None, None, None, None, None],
            [2, None, '31/PRJ/26', 'Pasang', 'pt isi', 'Jl. Isi 1', None, None, None, None, None],
            [3, None, '32/PRJ/26', 'Pasang', 'PT ISI', 'Jl. Lain 2', None, None, None, None, None],
        ]))
        preview = self.client.get('/documents/prj/import/preview').data.decode()
        self.assertEqual(preview.count('>NEW<'), 3)  # every row of a client that does not exist yet
        self.client.post('/documents/prj/import/confirm', data={'selected_rows': ['2', '3', '4']})
        with self.app.app_context():
            # Rows 30 and 31 share one new client, which takes row 31's address; row 32's other address is another.
            first = DocumentPRJ.query.filter_by(doc_no=30).one().client
            self.assertEqual((first.company_name, first.address), ('PT Isi', 'Jl. Isi 1'))
            self.assertEqual(DocumentPRJ.query.filter_by(doc_no=31).one().client, first)
            other = DocumentPRJ.query.filter_by(doc_no=32).one().client
            self.assertEqual((other.company_name, other.address), ('PT ISI', 'Jl. Lain 2'))
            self.assertEqual(ClientSite.query.filter(ClientSite.company_name.ilike('pt isi')).count(), 2)

    def test_prj_import_rows_per_item_blank_cells_and_status_rules(self):
        address = 'Jl, Kebayoran Lama\r\nBarat Jakarta Selatan - DKI Jakarta'
        nvr = 'UNV NVR 16 Channel - 4 HDD, Inc Bandwidth 160 Mbps'
        self._upload(recap_file([
            # Row 2: an item with no PRJ to belong to is an error, not silently dropped.
            [1, None, None, None, None, None, None, None, None, None, None, 'Yatim', '1', 'Unit'],
            # Row 3: blank Job, "Pending" in Notes.
            [1, '52/HO/23', '52/PRJ/23', None, 'Pak X', address, '108/SO/23',
             None, None, None, 'Pending', nvr, '1', 'SET'],
            [2, '53/HO/23', '53/PRJ/23', 'CCTV', 'PT Lama', None, 'SO-53',
             'Approved', '10/03/2023', None, None, 'Kamera', '4', 'Unit'],
            # Rows 5-6: PRJ 52 again further down, then a row with only an item: both join PRJ 52.
            [3, '52/HO/23', '52/PRJ/23', None, 'Pak X', address, '108/SO/23',
             None, None, None, 'Pending', 'HDD 4TB', '4', 'PCS'],
            [None, None, None, None, None, None, None, None, None, None, None, 'Kabel', '10', 'Meter'],
            # Row 7: blank client; Approved wins over "pending" and, without a date, completes on 1 January.
            [4, '54/HO/23', '54/PRJ/23', None, None, None, None,
             'Approved', None, None, 'dulu pending'],
            # Row 8: "Hold" in HO Status is not read; the address stays on the PRJ only.
            [5, None, '55/PRJ/23', 'Servis', '-', 'Jl. Satu', None, 'Hold'],
        ]))
        preview = self.client.get('/documents/prj/import/preview').data.decode()
        self.assertIn('PRJ Number is empty.', preview)
        self.assertIn('Row 3&ndash;6', preview)
        self.assertIn('Approved without a Handover Date; 1 January 2023 is used.', preview)

        self.client.post('/documents/prj/import/confirm', data={'selected_rows': ['2', '3', '4', '7', '8']})
        with self.app.app_context():
            self.assertEqual(DocumentPRJ.query.count(), 4)
            def prj(doc_no):
                return DocumentPRJ.query.filter_by(doc_no=doc_no).one()

            def items(doc):
                return [(item.description, item.qty, item.uom)
                        for item in sorted(doc.items, key=lambda item: item.display_order)]

            held = prj(52)
            self.assertEqual((held.status, held.ho.status), ('Hold', 'Hold'))
            self.assertEqual((held.title, held.so_number, held.notes), ('-', '108/SO/23', 'Pending'))
            self.assertEqual(held.client.company_name, 'Pak X')
            self.assertEqual(held.site_address, 'Jl, Kebayoran Lama Barat Jakarta Selatan - DKI Jakarta')
            self.assertEqual(held.ho.display_no, '52/HO/23')
            self.assertEqual(items(held), [(nvr, 1, 'SET'), ('HDD 4TB', 4, 'PCS'), ('Kabel', 10, 'Meter')])

            done = prj(53)
            self.assertEqual((done.status, done.actual_completion), ('Completed', date(2023, 3, 10)))
            self.assertEqual(items(done), [('Kamera', 4, 'Unit')])

            no_date = prj(54)
            self.assertEqual((no_date.status, no_date.actual_completion), ('Completed', date(2023, 1, 1)))
            self.assertEqual(no_date.ho.approved_at, datetime(2023, 1, 1))
            self.assertEqual(items(no_date), [])

            online = prj(55)
            self.assertEqual(online.status, 'Online')
            self.assertEqual(online.site_address, 'Jl. Satu')
            # Blank and "-" clients share one placeholder client "-" without an address.
            placeholder = ClientSite.query.filter_by(company_name='-').one()
            self.assertIsNone(placeholder.address)
            self.assertEqual({no_date.client_id, online.client_id}, {placeholder.id})

    def test_prj_import_accepts_alternative_headers_with_the_items_last(self):
        self._upload(excel_file(OLD_RECAP_HEADERS, [
            [1, '60/HO/26', '60/PRJ/26', 'Kerja', 'PT Lama', None, 'REF-60',
             None, None, None, None, 'Item A', '2', 'Unit'],
        ]))
        self.client.post('/documents/prj/import/confirm', data={'selected_rows': ['2']})
        with self.app.app_context():
            prj = DocumentPRJ.query.one()
            self.assertEqual(prj.so_number, 'REF-60')
            self.assertEqual([(item.description, item.qty) for item in prj.items], [('Item A', 2)])

    def test_default_xprj_format_has_month(self):
        with self.app.app_context():
            self.assertEqual(format_document_number('xprj', 246, date(2026, 9, 1)), '246/09/XPRJ/26')


if __name__ == '__main__':
    unittest.main()
