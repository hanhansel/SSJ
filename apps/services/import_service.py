import re
from datetime import datetime, date
import pandas as pd
from typing import List, Optional, Tuple, Dict, Any
from werkzeug.datastructures import FileStorage

from apps.models import (
    db, Document, DocumentSVC, DocumentXPRJ, ClientSite,
    STATUS_ONLINE, STATUS_DONE, STATUS_VOID, STATUS_HOLD,
)
from apps.core.registry import compute_doc_period, format_document_number

IMPORTABLE_DOC_TYPES = ('svc', 'xprj')
IMPORTED_STATUSES = (STATUS_ONLINE, STATUS_DONE, STATUS_VOID, STATUS_HOLD)
# Clients belong to Data Storage: an importer without it may only use existing clients.
NEW_CLIENT_NOT_ALLOWED = ('The client is not registered yet. Your account cannot add new clients; choose an '
                          'existing client, or ask an account with Data Storage access to add it in Client Data.')

# Values of the Status column, lowercased with spaces collapsed.  Completed and
# Void go straight to storage, Hold to the Hold filter; a blank cell and
# Not Completed stay Online.
STATUS_VALUES = {
    'void/cancelled': STATUS_VOID, 'void': STATUS_VOID, 'cancelled': STATUS_VOID,
    'completed': STATUS_DONE,
    'hold': STATUS_HOLD,
    '': STATUS_ONLINE, 'not completed': STATUS_ONLINE,
}

# Visit Type values that are a client type.  An XPRJ row's client is matched
# without its type (see ClientResolver), and a blank cell gives no type.
CLIENT_TYPES = {
    'on-call': 'On-Call',
    'contract': 'Contract',
    'survey': 'Survey',
}
TYPELESS_VISITS = ('', 'xprj')
# Filler typed into empty client cells; read as blank so it never becomes a PIC or type.
PLACEHOLDER_CELLS = ('-', '0')

# Columns A-I of the import popup, in order: (field, header shown in the popup,
# accepted header names after normalisation).  The field is the canonical header
# the rest of the import reads.
SVC_IMPORT_COLUMNS = (
    ('SVC Number', 'SVC Number', ('svc number', 'svc no', 'document number', 'document no')),
    ('Document Date', 'Document Date', ('document date', 'date')),
    ('Customer Name', 'Customer Name', ('customer name', 'customer', 'client name', 'client')),
    ('Address', 'Address', ('address', 'customer address', 'site address')),
    ('PIC', 'PIC', ('pic', 'contact person')),
    ('PIC Phone', 'PIC Phone', ('pic phone', 'pic phone number', 'pic number')),
    ('Reference Number', 'Reference Number', ('reference number', 'reference no', 'reference', 'ref no', 'ref')),
    ('Visit Type', 'Visit Type', ('visit type',)),
    ('Status', 'Status', ('status', 'job status')),
)
# Optional column J: one complaint per row.  The next rows of the same document
# repeat its SVC Number or leave it blank (see DocumentImportService.parse_excel).
SVC_COMPLAINT_COLUMN = ('customer_complaint', 'Customer Complaint', ('customer complaint', 'complaint'))


def normalize_header(header) -> str:
    return re.sub(r'[^a-z0-9]+', ' ', str(header).lower()).strip()


def missing_columns_error(missing: List[str]) -> str:
    return (f"Wrong Excel format. These columns were not found: {', '.join(missing)}. "
            "Make sure the first row holds the column headers of the format.")


def resolve_columns(columns: List[Any], spec, optional=(), may_be_missing=()) -> Tuple[Dict[str, Any], str]:
    """Map each field of ``spec`` and ``optional`` to the sheet column whose header has one of its names.

    Every column of ``spec`` (the ones listed in the import popup) is required, except the
    fields named in ``may_be_missing``: returns (columns by field, '') or ({}, error message
    naming the missing columns by their letter in ``spec``).  A missing ``optional`` or
    ``may_be_missing`` column maps to None.
    """
    headers = {normalize_header(column): column for column in columns}
    resolved, missing = {}, []
    for index, (field, header, names) in enumerate(spec):
        column = next((headers[name] for name in names if name in headers), None)
        if column is None and field not in may_be_missing:
            missing.append(f"{chr(ord('A') + index)} ({header})")
        resolved[field] = column
    if missing:
        return {}, missing_columns_error(missing)
    for field, _header, names in optional:
        resolved[field] = next((headers[name] for name in names if name in headers), None)
    return resolved, ''


def complaint_lines(text) -> List[str]:
    """The non-empty lines of a complaint, each on its own export row."""
    return [line.strip() for line in (text or '').splitlines() if line.strip()]


def resolve_status_cell(raw: str) -> Optional[str]:
    """Map a Status cell to a document status, or None when the value is not recognised."""
    key = re.sub(r'\s*/\s*', '/', ' '.join((raw or '').lower().split()))
    return STATUS_VALUES.get(key)


def _match_key(text) -> str:
    return ' '.join((text or '').lower().split())


def _name_key(text) -> str:
    """A company or PIC name without case, spaces or punctuation: "PT. X" = "PT X" = "ptx"."""
    return re.sub(r'[\W_]+', '', clean_cell_text(text or '').lower())


def _phone_key(phone) -> str:
    """Digits of a phone number starting with 0, so formatting differences do not count.

    A leading 62 (+62) is written as 0, and a number without its leading 0 (Excel drops it
    from a number cell) gets it back: "+62 812-3", "0812 3" and "8123" are the same number.
    """
    digits = re.sub(r'\D', '', phone or '')
    if digits.startswith('62'):
        digits = digits[2:]
    return digits if not digits or digits.startswith('0') else '0' + digits


def resolve_client_type(visit_type: str) -> Tuple[Optional[str], bool]:
    """(client type, recognised) for a Visit Type cell.

    Blank and XPRJ give no type; an unknown value gives no type either and is reported.
    """
    key = _match_key(visit_type)
    if key in TYPELESS_VISITS:
        return None, True
    if key in CLIENT_TYPES:
        return CLIENT_TYPES[key], True
    return None, False


# Excel escapes control characters as _xHHHH_; a line break typed in a cell often
# comes back as a literal "_x000D_" instead of a real carriage return.
EXCEL_ESCAPE = re.compile(r'_x([0-9A-Fa-f]{4})_')


def clean_cell_text(value, multiline: bool = False) -> str:
    """Clean text of one Excel cell.

    Line breaks and repeated spaces collapse into single spaces; a multi-line
    field (Notes) keeps one tidy line per break instead.
    """
    if pd.isna(value):
        return ''
    text = EXCEL_ESCAPE.sub(lambda match: chr(int(match.group(1), 16)), str(value))
    if text.strip().lower() == 'nan':
        return ''
    lines = [' '.join(line.split()) for line in re.split(r'[\r\n]+', text)]
    lines = [line for line in lines if line]
    if multiline:
        return '\n'.join(lines)
    return re.sub(r'\s+,', ',', ' '.join(lines))


def normalize_phone(value) -> str:
    """A phone cell as digits only: '0812-3456-7890' -> '081234567890'.

    A cell Excel stored as a number may come back as '81234567890.0'; the '.0' is dropped
    before the non-digits are removed.  (Excel itself drops a leading 0 of a number cell.)
    """
    return re.sub(r'\D', '', re.sub(r'\.0+$', '', clean_cell_text(value)))


# Words written in several ways in an address: "Street" is "St", "Road" is "Rd", and
# "No."/"Number" before a house number is left out, so "Rose Street No. 5" and "Rose St 5"
# are the same address.
ADDRESS_WORDS = {'street': 'st', 'road': 'rd', 'avenue': 'ave', 'no': '', 'number': ''}


def _address_key(address) -> str:
    """An address ignoring case, punctuation, line breaks, Excel escapes like _x000D_ and ADDRESS_WORDS."""
    words = re.sub(r'[^a-z0-9]+', ' ', clean_cell_text(address or '').lower()).split()
    return ' '.join(word for word in (ADDRESS_WORDS.get(word, word) for word in words) if word)


# A client created from an XPRJ row has this type: its documents are always XPRJ.
XPRJ_CLIENT_TYPE = 'XPRJ'


def _same_when_both_given(key_a: str, key_b: str) -> bool:
    """A field compared only when both sides have it: a blank cell never makes another client."""
    return not key_a or not key_b or key_a == key_b


# Shown on a row whose client does not exist yet; the preview lets the user pick an existing one.
NEW_CLIENT_WARNING = 'Will be added as a new client.'


class ClientResolver:
    """Find the client of an imported row in the one client table, or plan a new one.

    Every import first needs the same company name (``_name_key``: case, spaces and
    punctuation do not count).  Then:

    * SVC: the same type (a blank Visit Type is not compared), PIC and PIC phone;
    * XPRJ: the same PIC and PIC phone, any type (a new client gets type XPRJ);
    * PRJ (the recap has no type or PIC): the same address (``_address_key``).

    A PIC, phone or address is compared only when both the row and the client have one.
    When several clients match, the one created first is used, with a warning; the
    preview lets the user pick another existing client for any row.  A planned client
    is not added to the session; later rows that match it reuse it.
    """

    def __init__(self):
        self.clients = ClientSite.query.order_by(ClientSite.id).all()

    def _named(self, name: str) -> List[ClientSite]:
        key = _name_key(name)
        return [client for client in self.clients if _name_key(client.company_name) == key]

    def _plan(self, name: str, **values) -> ClientSite:
        client = ClientSite(company_name=name.strip(), **{field: value or None for field, value in values.items()})
        self.clients.append(client)
        return client

    @staticmethod
    def _first(matches: List[ClientSite]) -> Tuple[ClientSite, Optional[str]]:
        client = matches[0]
        if len(matches) == 1:
            return client, None
        return client, f'{len(matches)} clients match; using {client_label(client)}. Change it if needed.'

    def match_or_plan_svc_row(self, doc_type: str, name: str, client_type: Optional[str], pic_name: str,
                              pic_phone: str, address: str) -> Tuple[ClientSite, bool, Optional[str]]:
        """(client, is_new, warning) of an SVC or XPRJ row; ``is_new`` means the client does not exist yet."""
        is_xprj = doc_type == 'xprj'
        type_key = '' if is_xprj else _match_key(client_type)
        pic_key, phone_key = _name_key(pic_name), _phone_key(pic_phone)
        matches = [
            client for client in self._named(name)
            if (not type_key or _match_key(client.client_type) == type_key)
            and _same_when_both_given(pic_key, _name_key(client.pic_name))
            and _same_when_both_given(phone_key, _phone_key(client.pic_phone))
        ]
        if not matches:
            client = self._plan(name, client_type=XPRJ_CLIENT_TYPE if is_xprj else client_type,
                                pic_name=pic_name, pic_phone=pic_phone, address=address)
            return client, True, None
        client, warning = self._first(matches)
        return client, client.id is None, warning

    def match_or_plan_prj_row(self, name: str, address: str) -> Tuple[ClientSite, bool, Optional[str]]:
        """(client, is_new, warning) of an PRJ row: the same name, and the same address when both have one.

        Among several matches the one at exactly this address goes first.  A planned client
        without an address takes the first one a later row of the same name gives.
        """
        address_key = _address_key(address)
        matches = [client for client in self._named(name)
                   if _same_when_both_given(address_key, _address_key(client.address))]
        if not matches:
            return self._plan(name, address=address), True, None
        here = [client for client in matches if address_key and _address_key(client.address) == address_key]
        client, warning = self._first(here or matches)
        if client.id is None and not client.address and address:
            client.address = address
        return client, client.id is None, warning


def client_label(client: ClientSite) -> str:
    """'X Ltd · Contract · Alex (0812…) · 1 Main St …': the fields that tell same-named clients apart."""
    pic = client.pic_name or ''
    if client.pic_phone:
        pic = f'{pic} ({client.pic_phone})' if pic else client.pic_phone
    parts = (client.company_name, client.client_type, pic, client.address)
    return ' · '.join(part for part in parts if part)


def picker_clients() -> List[Dict[str, Any]]:
    """Every client for the preview's client picker, by name."""
    return [{'id': client.id, 'label': client_label(client), 'name': client.company_name,
             'type': client.client_type or '', 'pic': client.pic_name or '', 'phone': client.pic_phone or '',
             'address': client.address or ''}
            for client in ClientSite.query.order_by(ClientSite.company_name, ClientSite.id)]


def chosen_client(row: Dict[str, Any]) -> Tuple[Optional[ClientSite], Optional[str]]:
    """(client, error) the user picked for a confirmed row, or (None, None) to resolve it from the Excel."""
    choice = str(row.get('client_choice') or '').strip()
    if not choice:
        return None, None
    client = db.session.get(ClientSite, int(choice)) if choice.isdigit() else None
    if client is None:
        return None, 'The chosen client was not found.'
    return client, None


def set_preview_client(row: Dict[str, Any], client: ClientSite, warning: Optional[str],
                       allow_new_clients: bool) -> None:
    """Store a row's matched client for the preview.

    ``client_notes`` (warnings) and ``client_error`` belong to this matched client: the
    preview drops them once the user picks another existing client for the row.  A new
    client the account may not create blocks the row until an existing one is picked.
    """
    is_new = client.id is None
    row['client_id'] = client.id
    row['client_label'] = client_label(client)
    row['is_new_client'] = is_new
    row['client_error'] = NEW_CLIENT_NOT_ALLOWED if is_new and not allow_new_clients else ''
    row['client_notes'] = [note for note in (warning, NEW_CLIENT_WARNING if is_new and allow_new_clients else None)
                           if note]


def count_preview_rows(rows: List[Dict[str, Any]]) -> Tuple[int, int]:
    """(rows with an error, rows with a warning) as the preview first shows them."""
    errors = sum(bool(row['validation_errors'] or row.get('client_error')) for row in rows)
    warnings = sum(bool(row['validation_warnings'] or row.get('client_notes')) for row in rows)
    return errors, warnings


class ParsedRow:
    def __init__(self, row_index: int, data: dict):
        self.row_index = row_index
        self.doc_type: str = ''
        self.doc_no: int = 0
        self.doc_number_raw: str = ''
        self.doc_date: Optional[date] = None
        self.doc_period: str = ''
        
        self.client_name: str = data.get('Customer Name', '')
        self.address: str = data.get('Address', '')
        self.pic_name: str = data.get('PIC', '')
        self.pic_phone: str = normalize_phone(data.get('PIC Phone', ''))
        self.reference: str = data.get('Reference Number', '')
        self.visit_type: str = data.get('Visit Type', '')
        self.client_type, self.client_type_known = resolve_client_type(self.visit_type)
        self.status_raw: str = data.get('Status', '')
        self.status: str = STATUS_ONLINE
        # A document may span several sheet rows, one complaint each.
        self.source = data
        self.row_end: int = row_index
        self.complaints: List[str] = []

        # The matched client (see set_preview_client): None id = a new client.
        self.client_id: Optional[int] = None
        self.client_label: str = ''
        self.is_new_client: bool = False
        self.client_error: str = ''
        self.client_notes: List[str] = []
        self.is_duplicate: bool = False
        self.validation_errors: List[str] = []
        self.validation_warnings: List[str] = []


class ParseResult:
    def __init__(self):
        self.rows: List[ParsedRow] = []
        self.total_parsed: int = 0
        self.total_skipped: int = 0
        self.total_errors: int = 0
        self.total_warnings: int = 0

    def to_dict(self):
        return {
            'rows': [
                {
                    'row_index': r.row_index,
                    'doc_type': r.doc_type,
                    'doc_no': r.doc_no,
                    'doc_number_raw': r.doc_number_raw,
                    'doc_date': r.doc_date.strftime('%Y-%m-%d') if r.doc_date else '',
                    'doc_period': r.doc_period,
                    'client_name': r.client_name,
                    'address': r.address,
                    'pic_name': r.pic_name,
                    'pic_phone': r.pic_phone,
                    'reference': r.reference,
                    'visit_type': r.visit_type,
                    'status_raw': r.status_raw,
                    'status': r.status,
                    'row_end': r.row_end,
                    'customer_complaint': '\n'.join(r.complaints),
                    'client_id': r.client_id,
                    'client_label': r.client_label,
                    'is_new_client': r.is_new_client,
                    'client_error': r.client_error,
                    'client_notes': r.client_notes,
                    'is_duplicate': r.is_duplicate,
                    'validation_errors': r.validation_errors,
                    'validation_warnings': r.validation_warnings
                } for r in self.rows
            ],
            'total_parsed': self.total_parsed,
            'total_skipped': self.total_skipped,
            'total_errors': self.total_errors,
            'total_warnings': self.total_warnings,
        }


class ImportResult:
    def __init__(self):
        self.imported_count: int = 0
        self.clients_created: int = 0
        self.skipped_count: int = 0
        self.errors: List[str] = []


class DocumentImportService:

    @staticmethod
    def parse_excel(file: FileStorage, allow_new_clients: bool = True) -> Tuple[bool, Optional[ParseResult], str]:
        try:
            df = pd.read_excel(file, dtype=str)
        except Exception as e:
            return False, None, f"Could not read the Excel file: {str(e)}"

        # Columns A-I are required; headers are matched by name (e.g. "Job Status" = Status).
        # Column J (Customer Complaint) is optional.
        columns, error = resolve_columns(list(df.columns), SVC_IMPORT_COLUMNS, optional=(SVC_COMPLAINT_COLUMN,))
        if error:
            return False, None, error
        complaint_column = columns.pop(SVC_COMPLAINT_COLUMN[0])
        complaint_cells = df[complaint_column] if complaint_column is not None else None
        df = pd.DataFrame({field: df[column] for field, column in columns.items()})

        result = ParseResult()

        # Cache for the duplicate check
        # Map: (doc_type, doc_no, doc_period) -> True
        existing_docs_query = db.session.query(Document.document_type, Document.doc_no, Document.doc_period).all()
        existing_docs = { (dt, dn, dp): True for dt, dn, dp in existing_docs_query }

        doc_number_regex = r'^(\d+)/\d+/(SVC|XPRJ)/(\d+)$'
        current = None  # the document the previous rows belong to
        first_row_of_number = {}

        for index, row_data in df.iterrows():
            row_index = index + 2  # Excel row is 1-indexed, and header is row 1
            data_dict = {k: str(v).strip() if pd.notna(v) else '' for k, v in row_data.items()}
            data_dict['Address'] = clean_cell_text(row_data['Address'])
            for field in ('Address', 'PIC', 'PIC Phone', 'Reference Number', 'Visit Type'):
                if data_dict[field] in PLACEHOLDER_CELLS:
                    data_dict[field] = ''
            complaints = complaint_lines(
                clean_cell_text(complaint_cells[index], multiline=True) if complaint_cells is not None else '')

            if current and DocumentImportService._continues(current, data_dict, complaints):
                DocumentImportService._add_row(current, row_index, data_dict, complaints)
                continue

            # Skip empty rows completely
            if pd.isna(row_data['SVC Number']) and pd.isna(row_data['Document Date']):
                result.total_skipped += 1
                continue

            parsed = ParsedRow(row_index, data_dict)
            parsed.complaints = complaints
            current = parsed
            number_key = _match_key(data_dict['SVC Number'])
            if number_key in first_row_of_number:
                parsed.validation_errors.append(
                    f"Same document number as row {first_row_of_number[number_key]}. "
                    "The rows of one document must be next to each other.")
            elif number_key:
                first_row_of_number[number_key] = row_index

            # Parse the SVC Number
            raw_no = data_dict.get('SVC Number', '')
            parsed.doc_number_raw = raw_no
            if not raw_no:
                parsed.validation_errors.append("Document number is empty.")
            else:
                match = re.match(doc_number_regex, raw_no, re.IGNORECASE)
                if match:
                    parsed.doc_no = int(match.group(1))
                    tipe_str = match.group(2).upper()
                    parsed.doc_type = 'svc' if tipe_str == 'SVC' else 'xprj'
                else:
                    # Fall back to detecting the type when the format differs slightly
                    if 'SVC' in raw_no.upper():
                        parsed.doc_type = 'svc'
                    elif 'XPRJ' in raw_no.upper():
                        parsed.doc_type = 'xprj'
                    else:
                        parsed.validation_errors.append("Document type not valid or not recognised.")

                    # Take the leading digits as doc_no
                    nums = re.findall(r'^\d+', raw_no)
                    if nums:
                        parsed.doc_no = int(nums[0])
                    else:
                        parsed.validation_errors.append("Could not detect the document sequence number.")

            # Parse the date
            raw_date = data_dict.get('Document Date', '')
            if not raw_date:
                parsed.validation_errors.append("Document Date is empty.")
            else:
                try:
                    # Standard DD/MM/YYYY from Excel
                    if ' ' in raw_date:
                        raw_date = raw_date.split()[0]  # drop any time
                        
                    # Handle multiple formats
                    if '-' in raw_date:
                        if len(raw_date.split('-')[0]) == 4:
                            parsed.doc_date = datetime.strptime(raw_date, '%Y-%m-%d').date()
                        else:
                            parsed.doc_date = datetime.strptime(raw_date, '%d-%m-%Y').date()
                    elif '/' in raw_date:
                        parsed.doc_date = datetime.strptime(raw_date, '%d/%m/%Y').date()
                    else:
                        parsed.validation_errors.append(f"Invalid date format: {raw_date}")

                except Exception:
                    parsed.validation_errors.append(f"Could not read the date: {raw_date}")

            if parsed.doc_date:
                parsed.doc_period = compute_doc_period(parsed.doc_date)

            # Client checks (the client itself is matched once every row is read)
            if not parsed.client_name:
                parsed.validation_errors.append("Customer Name is empty.")
            if not parsed.client_type_known:
                parsed.validation_warnings.append(
                    f"Visit Type '{parsed.visit_type}' is not recognised; the client type is left blank.")

            # Check Duplicate
            if parsed.doc_type and parsed.doc_no and parsed.doc_period:
                key = (parsed.doc_type, parsed.doc_no, parsed.doc_period)
                if key in existing_docs:
                    parsed.is_duplicate = True
                    parsed.validation_warnings.append(f"The document already exists in the database (Type: {parsed.doc_type.upper()}, No: {parsed.doc_no}, Period: {parsed.doc_period})")

            # Cross-check XPRJ consistency
            if parsed.visit_type and 'XPRJ' in parsed.visit_type.upper() and parsed.doc_type == 'svc':
                parsed.validation_warnings.append("Visit Type is 'XPRJ' but the document was detected as SVC.")

            status = resolve_status_cell(parsed.status_raw)
            if status is None:
                parsed.validation_warnings.append(
                    f"Status '{parsed.status_raw}' is not recognised; the document is imported as Online.")
            parsed.status = status or STATUS_ONLINE
            if parsed.doc_type == 'svc' and not parsed.visit_type and parsed.status in (STATUS_ONLINE, STATUS_HOLD):
                parsed.validation_warnings.append(
                    "Visit Type is empty although the document is not completed; the client type cannot be determined.")

            result.rows.append(parsed)
            result.total_parsed += 1

        DocumentImportService._resolve_preview_clients(result, allow_new_clients)
        result.total_errors, result.total_warnings = count_preview_rows(
            [{'validation_errors': r.validation_errors, 'validation_warnings': r.validation_warnings,
              'client_error': r.client_error, 'client_notes': r.client_notes} for r in result.rows])
        return True, result, ''

    @staticmethod
    def _continues(current: ParsedRow, data: Dict[str, str], complaints: List[str]) -> bool:
        """True when a row adds to the document above it: it repeats its SVC Number, or it leaves
        the number blank (a merged or not retyped cell) but has a complaint for the same client."""
        number = _match_key(data['SVC Number'])
        if number:
            return number == _match_key(current.source['SVC Number'])
        same_client = not data['Customer Name'] or _match_key(data['Customer Name']) == _match_key(current.client_name)
        return bool(complaints) and same_client

    @staticmethod
    def _add_row(current: ParsedRow, row_index: int, data: Dict[str, str], complaints: List[str]) -> None:
        """Add a continuation row's complaints; its other cells must match the document's first row."""
        current.complaints.extend(complaints)
        current.row_end = row_index
        differing = [header for field, header, _ in SVC_IMPORT_COLUMNS
                     if data[field] and _match_key(data[field]) != _match_key(current.source[field])]
        if differing:
            current.validation_warnings.append(
                f"Row {row_index}: column {', '.join(differing)} differs from row {current.row_index}; "
                f"row {current.row_index} is used.")

    @staticmethod
    def _resolve_preview_clients(result: ParseResult, allow_new_clients: bool) -> None:
        """Show which client each row joins; the preview may pick another existing client."""
        resolver = ClientResolver()
        for row in result.rows:
            if not row.client_name:
                continue
            client, _is_new, warning = resolver.match_or_plan_svc_row(
                row.doc_type, row.client_name, row.client_type, row.pic_name, row.pic_phone, row.address)
            preview = {}
            set_preview_client(preview, client, warning, allow_new_clients)
            for field in ('client_id', 'client_label', 'is_new_client', 'client_error', 'client_notes'):
                setattr(row, field, preview[field])


    @staticmethod
    def _validate_confirmed_row(row: Dict[str, Any]) -> Tuple[Optional[str], Optional[date]]:
        """Re-validate a staged row server-side; never trust the preview checkbox alone.

        Returns (error_message, parsed_date). Fields fixed through inline edit
        (client name, date) are checked again with their new values.
        """
        if row.get('doc_type') not in IMPORTABLE_DOC_TYPES:
            return 'Document type not recognised (must be SVC or XPRJ).', None
        try:
            if int(row.get('doc_no') or 0) < 1:
                raise ValueError
        except (TypeError, ValueError):
            return 'Invalid document sequence number.', None
        if row.get('status') not in IMPORTED_STATUSES:
            return 'Invalid status.', None
        if not (row.get('client_name') or '').strip():
            return 'Customer Name is empty.', None
        try:
            doc_date = datetime.strptime(row.get('doc_date') or '', '%Y-%m-%d').date()
        except ValueError:
            return 'Invalid date (format YYYY-MM-DD).', None
        return None, doc_date

    @staticmethod
    def execute_import(confirmed_rows: List[Dict[str, Any]], allow_new_clients: bool = True) -> ImportResult:
        result = ImportResult()

        valid_rows = []
        for row in confirmed_rows:
            error, doc_date = DocumentImportService._validate_confirmed_row(row)
            if error:
                result.errors.append(f"Row {row.get('row_index')}: {error} Skipped.")
                result.skipped_count += 1
                continue
            row['_date'] = doc_date
            # Date may have been edited inline, so the reset-period key is recomputed.
            row['doc_period'] = compute_doc_period(doc_date)
            valid_rows.append(row)
        confirmed_rows = valid_rows

        # Step 1: each row's client: the one picked in the preview, else matched from the
        # Excel again, creating the ones that do not exist yet.  Existing clients are left untouched.
        resolver = ClientResolver()
        new_clients = []
        kept_rows = []
        for row in confirmed_rows:
            client, error = chosen_client(row)
            if error:
                result.errors.append(f"Row {row.get('row_index')}: {error} Skipped.")
                result.skipped_count += 1
                continue
            if client is None:
                client, _is_new, _warning = resolver.match_or_plan_svc_row(
                    row['doc_type'], row['client_name'], resolve_client_type(row.get('visit_type', ''))[0],
                    row.get('pic_name', ''), row.get('pic_phone', ''), row.get('address', ''))
            if client.id is None and not allow_new_clients:
                # Checked again here: the preview only offers the row once an existing client is picked.
                result.errors.append(f"Row {row.get('row_index')}: {NEW_CLIENT_NOT_ALLOWED} Skipped.")
                result.skipped_count += 1
                continue
            row['_client'] = client
            kept_rows.append(row)
            if client.id is None and client not in new_clients:
                new_clients.append(client)
        confirmed_rows = kept_rows
        if new_clients:
            db.session.add_all(new_clients)
            db.session.commit()
            result.clients_created = len(new_clients)

        # Step 2: Insert Documents
        for row in confirmed_rows:
            try:
                client = row['_client']
                doc_type = row['doc_type']
                doc_date = row['_date']
                doc_no = int(row['doc_no'])
                doc_period = row['doc_period']
                
                # Re-check db for absolute safety
                exists = Document.query.filter_by(
                    document_type=doc_type, 
                    doc_no=doc_no, 
                    doc_period=doc_period
                ).first()
                
                if exists:
                    # User confirmed to import anyway, but DB constraint will fail, so we update it?
                    # No, the requirement says we detect and maybe warn, but if it exists, it's a hard error on DB insert.
                    # Or we just skip.
                    result.errors.append(f"Row {row.get('row_index')}: duplicate found in the database, skipped.")
                    result.skipped_count += 1
                    continue

                # Imported documents keep the number exactly as written in the Excel file.
                doc_number_text = (row.get('doc_number_raw') or '').strip() or format_document_number(doc_type, doc_no, doc_date)
                # The Excel has no completion date, so a finished document is taken
                # as completed on its own date; storage lists filter on this field.
                completion = doc_date if row['status'] in (STATUS_DONE, STATUS_VOID) else None

                if doc_type == 'svc':
                    doc = DocumentSVC(
                        doc_no=doc_no,
                        doc_period=doc_period,
                        doc_number_text=doc_number_text,
                        date=doc_date,
                        client_id=client.id,
                        site_address=row.get('address') or client.address or '',
                        reference=row.get('reference') or None,
                        customer_complaint=row.get('customer_complaint') or None,
                        task_description=None,    # Intentional blank as per req
                        estimated_calendar_days=2,
                        status=row['status'],
                        actual_completion=completion
                    )
                else:
                    doc = DocumentXPRJ(
                        doc_no=doc_no,
                        doc_period=doc_period,
                        doc_number_text=doc_number_text,
                        date=doc_date,
                        client_id=client.id,
                        site_address=row.get('address') or client.address or '',
                        reference=row.get('reference') or None,
                        customer_complaint=row.get('customer_complaint') or None,
                        task_description=None,
                        estimated_calendar_days=2,
                        prj_id=None, # Null by default as per req
                        status=row['status'],
                        actual_completion=completion
                    )
                    
                db.session.add(doc)
                db.session.commit()
                result.imported_count += 1
                
            except Exception as e:
                db.session.rollback()
                result.errors.append(f"Row {row.get('row_index')}: system error ({str(e)}).")
                result.skipped_count += 1

        return result
