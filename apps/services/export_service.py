"""Excel exports of the SVC & XPRJ and PRJ lists, the client list and the Master Projects.

Each sheet starts with the columns of its import popup, under the same headers,
so an exported file can be imported again.  A document takes one row per
complaint line (SVC & XPRJ) or per work item (PRJ); its other cells repeat on
each of those rows, and each document gets its own grey band.  The import joins
the rows of one document again and ignores the extra columns that follow.
Links between documents (an XPRJ's parent PRJ, an PRJ's Master Project) are
shown but never imported.
"""
from datetime import date, datetime, timedelta
from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from apps.models import STATUS_ONLINE, STATUS_PENDING, STATUS_HOLD, STATUS_DONE, STATUS_VOID
from apps.services.import_service import SVC_IMPORT_COLUMNS, SVC_COMPLAINT_COLUMN, complaint_lines
from apps.services.prj_import_service import HO_STATUS_APPROVED, HOLD_KEYWORD, PRJ_IMPORT_COLUMNS

# Export popup choices: key -> (label, file name part).  Filtered on the document date.
EXPORT_PERIODS = {
    '60d': ('Last 60 days', '60Days'),
    'year': ('This year', 'ThisYear'),
    'all': ('All', 'All'),
}

# Status column of the SVC import: an active document is "Not Completed".
SVC_STATUS_WORDS = {
    STATUS_ONLINE: 'Not Completed',
    STATUS_PENDING: 'Not Completed',
    STATUS_HOLD: 'Hold',
    STATUS_DONE: 'Completed',
    STATUS_VOID: 'Void',
}
# Typed into the Visit Type column of every XPRJ row by the import format.
XPRJ_VISIT_TYPE = 'XPRJ'

SVC_EXTRA_HEADERS = (
    'Type', 'System Status', 'Work Address', 'Parent PRJ', 'Completion Date',
    'Estimated Days', 'Technician', 'Captain', 'Internal Notes', 'Task Description',
)
PRJ_EXTRA_HEADERS = (
    'PRJ Date', 'System Status', 'Master Project', 'Estimated Days', 'Urgency',
    'Technician', 'Captain', 'Technician Tasks',
)
# Columns A-J of the client import template (system.download_client_template).
CLIENT_IMPORT_HEADERS = (
    'No', 'Name', 'Address 1', 'Address 2', 'Address 3', 'PIC', 'PIC Phone', 'Customer Type', 'Lat', 'Long',
)
CLIENT_EXTRA_HEADERS = ('Document Count', 'Created Date')
MASTER_PROJECT_HEADERS = ('Project Code', 'Estimated Work Days', 'Actual Work Days', 'Project KPI')
# Long free text: wrapped in a wide column instead of one endless line.
WRAPPED_HEADERS = {
    'Internal Notes', 'Notes', 'Address', 'Address 1', 'Work Address',
    'Task Description', 'Technician Tasks',
}
DATE_FORMAT = 'DD/MM/YYYY'
HEADER_FILL = PatternFill('solid', fgColor='1E293B')
# One band per document, dark to light grey and round again; black text stays readable on each.
BAND_FILLS = tuple(PatternFill('solid', fgColor=color) for color in ('BFBFBF', 'CCCCCC', 'D9D9D9', 'E6E6E6', 'F2F2F2'))
_GRID = Side(style='thin', color='A6A6A6')
CELL_BORDER = Border(left=_GRID, right=_GRID, top=_GRID, bottom=_GRID)


def period_range(period, today):
    """(first, last) document date of an export period; None means unbounded."""
    if period == '60d':
        return today - timedelta(days=60), None
    if period == 'year':
        return date(today.year, 1, 1), date(today.year, 12, 31)
    return None, None


def _value(value):
    """Blank cells stay empty, so the import reads them as missing rather than as text."""
    if isinstance(value, str):
        value = value.strip()
    return value if value not in ('', '-', None) else None


def _technicians(doc):
    return _value(doc.assigned_technicians)


def _svc_xprj_rows(doc):
    """One row per complaint line; a document without a complaint still gets one row."""
    client = doc.client
    is_xprj = doc.document_type == 'xprj'
    parent = doc.prj_parent if is_xprj else None
    imported = {
        'SVC Number': doc.display_no,
        'Document Date': doc.date,
        'Customer Name': client.company_name if client else None,
        # The client's own address, so an import finds the same client again; the
        # document's site address follows under Work Address.
        'Address': client.address if client else None,
        'PIC': client.pic_name if client else None,
        'PIC Phone': client.pic_phone if client else None,
        'Reference Number': doc.reference,
        'Visit Type': XPRJ_VISIT_TYPE if is_xprj else (client.client_type if client else None),
        'Status': SVC_STATUS_WORDS.get(doc.status, doc.status),
    }
    extra = (
        'XPRJ' if is_xprj else 'SVC',
        doc.status,
        doc.site_address or (client.address if client else None),
        parent.display_no if parent else None,
        doc.actual_completion if doc.status in (STATUS_DONE, STATUS_VOID) else None,
        doc.estimated_calendar_days,
        _technicians(doc),
        doc.captain_name,
        doc.notes,
        doc.task_description,
    )
    head = [imported[field] for field, _, _ in SVC_IMPORT_COLUMNS]
    return [head + [line] + list(extra) for line in complaint_lines(doc.customer_complaint) or [None]]


def _prj_rows(number, prj):
    """One row per work item, with its Qty and UOM; a PRJ without items still gets one row."""
    client = prj.client
    ho = prj.ho
    items = sorted(prj.items, key=lambda item: item.display_order or 0)
    tasks = sorted(prj.technician_tasks, key=lambda task: task.display_order or 0)
    handover = None
    if prj.status == STATUS_DONE:
        handover = (ho.completion_date if ho else None) or prj.actual_completion
    imported = {
        'row_no': number,
        'ho_number_raw': ho.display_no if ho else None,
        'prj_number_raw': prj.display_no,
        'title': prj.title,
        'client_name': client.company_name if client else None,
        'address': prj.site_address or (client.address if client else None),
        'so_number': prj.so_number,
        # The import reads Completed from "Approved", Void from a Void Date and Hold
        # from "Pending" in Notes; System Status further on shows the status itself.
        'ho_status_raw': HO_STATUS_APPROVED if prj.status == STATUS_DONE else None,
        'handover_raw': handover,
        # Only the presence of a date marks the row Void on import; PRJs imported
        # as Void have no completion date, so their own date stands in.
        'void_raw': (prj.actual_completion or prj.date) if prj.status == STATUS_VOID else None,
        'notes': _prj_notes(prj),
    }
    extra = (
        prj.date,
        prj.status,
        prj.master_project.name if prj.master_project else None,
        prj.estimated_calendar_days,
        'Urgent' if prj.urgency_level == 2 else 'Normal',
        _technicians(prj),
        prj.captain_name,
        '\n'.join(task.description for task in tasks),
    )
    item_cells = ([{'item_description': item.description, 'item_qty': item.qty, 'item_uom': item.uom}
                   for item in items] or [{'item_description': None, 'item_qty': None, 'item_uom': None}])
    return [[{**imported, **cells}[field] for field, _, _ in PRJ_IMPORT_COLUMNS] + list(extra)
            for cells in item_cells]


def _prj_notes(prj):
    """Notes of a PRJ, starting with "Pending" when it is on Hold so it imports as Hold again."""
    notes = prj.notes or ''
    if prj.status != STATUS_HOLD or HOLD_KEYWORD in notes.lower():
        return notes or None
    return 'Pending' + (f'\n{notes}' if notes else '')


def _workbook(sheet_title, headers, documents):
    """``documents`` gives the rows of each document; each document is one grey band."""
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = sheet_title
    sheet.append(list(headers))
    for index, rows in enumerate(documents):
        fill = BAND_FILLS[index % len(BAND_FILLS)]
        for row in rows:
            sheet.append([_value(value) for value in row])
            for cell in sheet[sheet.max_row]:
                cell.fill = fill
                cell.border = CELL_BORDER

    for cell in sheet[1]:
        cell.font = Font(bold=True, color='FFFFFF')
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(vertical='center')
        cell.border = CELL_BORDER
    sheet.freeze_panes = 'A2'
    sheet.auto_filter.ref = sheet.dimensions

    for index, header in enumerate(headers, start=1):
        letter = get_column_letter(index)
        cells = sheet[letter][1:]
        if header in WRAPPED_HEADERS:
            width = 50
            for cell in cells:
                cell.alignment = Alignment(wrap_text=True, vertical='top')
        else:
            width = max([len(header)] + [len(str(cell.value)) for cell in cells if cell.value is not None]) + 2
        for cell in cells:
            if isinstance(cell.value, (date, datetime)):
                cell.number_format = DATE_FORMAT
        sheet.column_dimensions[letter].width = min(width, 50)

    output = BytesIO()
    workbook.save(output)
    output.seek(0)
    return output


def svc_xprj_workbook(docs):
    """SVC & XPRJ sheet: the nine import columns and Customer Complaint, then the details the import does not read."""
    headers = ([header for _, header, _ in SVC_IMPORT_COLUMNS] + [SVC_COMPLAINT_COLUMN[1]]
               + list(SVC_EXTRA_HEADERS))
    return _workbook('SVC & XPRJ Documents', headers, (_svc_xprj_rows(doc) for doc in docs))


def prj_workbook(prjs):
    """PRJ sheet: the fourteen recap import columns, one row per item, then the details the import does not read."""
    headers = [header for _, header, _ in PRJ_IMPORT_COLUMNS] + list(PRJ_EXTRA_HEADERS)
    return _workbook('PRJ Documents', headers,
                     (_prj_rows(number, prj) for number, prj in enumerate(prjs, start=1)))


def clients_workbook(clients, document_counts):
    """Client sheet: columns A-J of the client import template, then the details the import does not read.

    The whole address goes in Address 1; the import joins Address 1-3 again.
    ``document_counts`` maps a client id to its number of documents.
    """
    rows = (
        [[number, client.company_name, client.address, None, None, client.pic_name, client.pic_phone,
          client.client_type, client.latitude, client.longitude,
          document_counts.get(client.id, 0), client.created_at.date() if client.created_at else None]]
        for number, client in enumerate(clients, start=1)
    )
    return _workbook('Client Data', CLIENT_IMPORT_HEADERS + CLIENT_EXTRA_HEADERS, rows)


def master_projects_workbook(project_kpis):
    """One row per Master Project: the totals of its KPI, then the number of each non-Void
    PRJ, oldest first, under PRJ #1, PRJ #2, ...  There are as many PRJ columns as the
    project with the most PRJs needs.  A project without PRJs leaves its KPI cells empty.

    ``project_kpis`` pairs each project with its ProjectService.project_kpi, whose
    ``prjs`` already leaves out Void PRJs.  XPRJ documents are not listed.
    """
    prj_columns = max([len(kpi['prjs']) for _, kpi in project_kpis] + [1])
    headers = MASTER_PROJECT_HEADERS + tuple(f'PRJ #{number}' for number in range(1, prj_columns + 1))
    rows = []
    for project, kpi in project_kpis:
        totals = [kpi['total_est'], kpi['total_real'], kpi['status']] if kpi['status'] else [None, None, None]
        rows.append([[project.name] + totals + [prj['number'] for prj in kpi['prjs']]])
    return _workbook('Master Project', headers, rows)
