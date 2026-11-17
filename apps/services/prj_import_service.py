"""Import historical PRJs (and their HOs) from the PRJ/HO recap Excel.

The recap has columns A-N:
No. | HO Number | PRJ Number | Job | Customer Name | Address | SO Number |
Item Name | Qty | UOM | HO Status | Handover Date | Void Date | Notes.
There is one row per work item: every row of a PRJ repeats its PRJ Number (and HO),
wherever it is in the sheet, and the PRJ's own cells are taken from its first row.

Any cell may be empty; blank text is stored as ``-`` and a blank Customer Name joins
the placeholder client ``-``.  Only the PRJ Number is needed, as it identifies the PRJ.

Document numbers are imported as text exactly as written.  The recap has no PRJ
date, so the document date is 1 January of the year in the PRJ number: that year
is what keeps ``82/PRJ/22`` and ``82/PRJ/23`` in separate periods.
"""
import re
from datetime import date, datetime, time
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
from werkzeug.datastructures import FileStorage

from apps.models import (
    db, HO, ClientSite, Document, DocumentPRJ, PRJItem,
    STATUS_ONLINE, STATUS_DONE, STATUS_VOID, STATUS_HOLD, HO_APPROVED,
)
from apps.core.dates import format_date
from apps.core.registry import compute_doc_period, format_ho_number
from apps.services.import_service import (
    IMPORTED_STATUSES, NEW_CLIENT_NOT_ALLOWED, PLACEHOLDER_CELLS, ClientResolver, ImportResult, _match_key,
    chosen_client, clean_cell_text, count_preview_rows, missing_columns_error, resolve_columns, set_preview_client,
)

# Fields the recap does not have get the same defaults as a new PRJ in the generator.
DEFAULT_ESTIMATED_DAYS = 5
DEFAULT_URGENCY_LEVEL = 1  # Normal
EMPTY_TEXT = '-'
# The client of a PRJ whose Customer Name is blank: one shared client named "-".
NO_CLIENT_NAME = EMPTY_TEXT
# HO Status of a finished PRJ, and the Notes word that puts a PRJ on Hold.
HO_STATUS_APPROVED = 'Approved'
HOLD_KEYWORD = 'pending'

# Columns A-N of the import popup, in order: (field, header shown in the popup,
# accepted header names after normalisation), resolved by import_service.resolve_columns.
PRJ_IMPORT_COLUMNS = (
    ('row_no', 'No.', ('no', 'number')),
    ('ho_number_raw', 'HO Number', ('ho number', 'ho no')),
    ('prj_number_raw', 'PRJ Number', ('prj number', 'prj no')),
    ('title', 'Job', ('job', 'job title', 'title')),
    ('client_name', 'Customer Name', ('customer name', 'customer', 'client name', 'client')),
    ('address', 'Address', ('address', 'site address')),
    ('so_number', 'SO Number', ('so number', 'so no', 'reference number', 'reference')),
    ('item_description', 'Item Name', ('item name', 'item', 'item description')),
    ('item_qty', 'Qty', ('qty', 'quantity')),
    ('item_uom', 'UOM', ('uom', 'unit')),
    ('ho_status_raw', 'HO Status', ('ho status',)),
    ('handover_raw', 'Handover Date', ('handover date',)),
    ('void_raw', 'Void Date', ('void date',)),
    ('notes', 'Notes', ('notes', 'remarks')),
)
# The work item of a row; the other columns belong to the PRJ.
ITEM_FIELDS = ('item_description', 'item_qty', 'item_uom')
DEFAULT_ITEM_UOM = 'Unit'
# Fields a later row of the PRJ must not contradict (blank is fine), with their headers.
PRJ_ROW_FIELDS = tuple((field, header) for field, header, _ in PRJ_IMPORT_COLUMNS
                       if field != 'row_no' and field not in ITEM_FIELDS)

# English (and Indonesian) month names, matched on their first three letters.
MONTH_PREFIXES = {
    'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'mei': 5, 'may': 5, 'jun': 6, 'jul': 7,
    'agu': 8, 'agt': 8, 'aug': 8, 'sep': 9, 'okt': 10, 'oct': 10, 'nov': 11, 'des': 12, 'dec': 12,
}


def _two_digit_year(year: int) -> int:
    return 2000 + year if year < 100 else year


def parse_recap_date(raw: str) -> Optional[date]:
    """Parse ``23 Jun 26``, ``23 June 2026``, ``23/06/2026`` or an Excel date cell.

    Returns None for an empty cell or ``-``; raises ValueError when unreadable.
    """
    text = (raw or '').strip()
    if text in ('', EMPTY_TEXT):
        return None
    iso = re.match(r'^(\d{4})-(\d{1,2})-(\d{1,2})', text)  # Excel date cells read as text
    if iso:
        return date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
    numeric = re.match(r'^(\d{1,2})[/.-](\d{1,2})[/.-](\d{2}|\d{4})$', text)
    if numeric:
        return date(_two_digit_year(int(numeric.group(3))), int(numeric.group(2)), int(numeric.group(1)))
    named = re.match(r'^(\d{1,2})[\s-]+([A-Za-z]+)\.?[\s-]+(\d{2}|\d{4})$', text)
    if named:
        month = MONTH_PREFIXES.get(named.group(2)[:3].lower())
        if month:
            return date(_two_digit_year(int(named.group(3))), month, int(named.group(1)))
    raise ValueError(text)


def parse_prj_number(raw: str) -> Tuple[int, int]:
    """``82/PRJ/22`` -> (82, 2022): the sequence number and the year it belongs to."""
    parts = [part.strip() for part in raw.split('/')]
    if len(parts) < 3 or not parts[0].isdigit() or 'PRJ' not in (part.upper() for part in parts[1:-1]):
        raise ValueError(raw)
    year_text = parts[-1]
    if not (year_text.isdigit() and len(year_text) in (2, 4)) or int(parts[0]) < 1:
        raise ValueError(raw)
    return int(parts[0]), _two_digit_year(int(year_text))


def parse_item(description: str, qty_raw: str, uom: str, warnings: List[str],
               row_index: int) -> Optional[Dict[str, Any]]:
    """One work item from the Item Name, Qty and UOM cells of a row, or None when the row has no item."""
    if not description:
        if qty_raw or uom:
            warnings.append(f'Row {row_index}: Qty/UOM without an Item Name is ignored.')
        return None
    qty = 1
    if qty_raw:
        try:
            number = float(qty_raw.replace(',', '.'))
            if number < 1 or number != int(number):
                raise ValueError
            qty = int(number)
        except ValueError:
            warnings.append(f"Row {row_index}: Qty '{qty_raw}' is not a whole number; 1 is used.")
    return {'description': description, 'qty': qty, 'uom': uom or DEFAULT_ITEM_UOM}


UOM_MAX_LENGTH = 50  # PRJItem.uom


def clean_items(items: Any) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
    """(items, error): the work items sent back by the preview's item editor, checked again.

    A line without a description is dropped; Qty must be a whole number of at least 1,
    and a blank UOM is Unit.
    """
    if not isinstance(items, list):
        return None, 'Invalid item list.'
    cleaned = []
    for number, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            return None, 'Invalid item list.'
        description = str(item.get('description') or '').strip()
        if not description:
            continue
        try:
            qty = float(item.get('qty', 1))
            if isinstance(item.get('qty'), bool) or qty < 1 or qty != int(qty):
                raise ValueError
        except (TypeError, ValueError):
            return None, f'Item {number}: Qty must be a whole number of at least 1.'
        uom = str(item.get('uom') or '').strip()[:UOM_MAX_LENGTH] or DEFAULT_ITEM_UOM
        cleaned.append({'description': description, 'qty': int(qty), 'uom': uom})
    return cleaned, None


def resolve_import_status(ho_status_raw: str, void_raw: str, notes: str) -> str:
    """A Void Date makes it Void; else HO Status "Approved" is Completed; else "pending"
    anywhere in Notes is Hold; anything else is Online.

    Only the presence of a Void Date matters: the date itself is not stored.
    """
    if void_raw and void_raw != EMPTY_TEXT:
        return STATUS_VOID
    if ho_status_raw.strip().lower().startswith(HO_STATUS_APPROVED.lower()):
        return STATUS_DONE
    if HOLD_KEYWORD in (notes or '').lower():
        return STATUS_HOLD
    return STATUS_ONLINE


def client_name_or_placeholder(name: str) -> str:
    """The Customer Name, or the placeholder client's name when the cell is blank or a filler."""
    name = (name or '').strip()
    return NO_CLIENT_NAME if name in ('',) + PLACEHOLDER_CELLS else name


def client_address(name: str, address: str) -> str:
    """The address a new client gets: none for the placeholder client, which many sites share."""
    return '' if name == NO_CLIENT_NAME else address


class PRJImportService:

    @staticmethod
    def parse_excel(file: FileStorage, allow_new_clients: bool = True) -> Tuple[bool, Optional[Dict[str, Any]], str]:
        try:
            df = pd.read_excel(file, dtype=str)
        except Exception as e:
            return False, None, f"Could not read the Excel file: {str(e)}"

        # A recap without any item column still imports, without items.
        columns, error = resolve_columns(list(df.columns), PRJ_IMPORT_COLUMNS, may_be_missing=ITEM_FIELDS)
        if error:
            return False, None, error
        if columns['item_description'] is None and (columns['item_qty'] or columns['item_uom']):
            # Qty/UOM without a recognised Item Name header would drop every item without a word.
            index = next(i for i, (field, _, _) in enumerate(PRJ_IMPORT_COLUMNS) if field == 'item_description')
            return False, None, missing_columns_error([f"{chr(ord('A') + index)} ({PRJ_IMPORT_COLUMNS[index][1]})"])
        del columns['row_no']  # required in the sheet, but the running number is not imported

        # A client is reused when its name and address match (see ClientResolver.match_or_plan_prj_row).
        resolver = ClientResolver()
        existing_numbers = {
            (doc_no, doc_period) for doc_no, doc_period in
            db.session.query(Document.doc_no, Document.doc_period).filter(Document.document_type == 'prj')
        }
        seen_numbers = {}
        prjs_by_number = {}  # PRJ Number as written -> the PRJ its first row started
        current = None  # the PRJ the previous row belongs to

        result = {
            'rows': [], 'total_parsed': 0, 'total_skipped': 0,
            'total_errors': 0, 'total_warnings': 0,
        }

        for index, row_data in df.iterrows():
            row_index = index + 2  # Excel row is 1-indexed, and header is row 1
            row = {field: clean_cell_text(row_data[column], multiline=field == 'notes') if column is not None else ''
                   for field, column in columns.items()}
            if row['address'] in PLACEHOLDER_CELLS:
                row['address'] = ''  # "0" / "-" fill an empty cell; never print them as an address
            item_cells = [row.pop(field) for field in ITEM_FIELDS]

            prj = PRJImportService._prj_of_row(row, item_cells[0], current, prjs_by_number)
            if prj:
                PRJImportService._add_row(prj, row_index, row, item_cells)
                current = prj
                continue

            if not any(row[field] for field in ('prj_number_raw', 'ho_number_raw', 'title', 'client_name')) \
                    and not item_cells[0]:
                result['total_skipped'] += 1
                continue

            errors, warnings = [], []
            row['_source'] = dict(row)
            row.update({'row_index': row_index, 'row_end': row_index, 'doc_no': 0, 'doc_year': 0, 'doc_period': ''})
            item = parse_item(*item_cells, warnings, row_index)
            row['items'] = [item] if item else []

            if not row['prj_number_raw']:
                errors.append('PRJ Number is empty.')
            else:
                try:
                    row['doc_no'], row['doc_year'] = parse_prj_number(row['prj_number_raw'])
                    row['doc_period'] = compute_doc_period(date(row['doc_year'], 1, 1))
                except ValueError:
                    if 'XPRJ' in row['prj_number_raw'].upper():
                        errors.append('This number is an XPRJ; use the import on the SVC & XPRJ page.')
                    else:
                        errors.append(f"PRJ Number format not recognised: {row['prj_number_raw']}")

            row['title'] = row['title'] or EMPTY_TEXT
            row['so_number'] = row['so_number'] or EMPTY_TEXT

            row['client_name'] = client_name_or_placeholder(row['client_name'])
            # The client is stored at the end: a later row may still give a new client its address.
            row['_client'], _is_new, row['_client_warning'] = resolver.match_or_plan_prj_row(
                row['client_name'], client_address(row['client_name'], row['address']))

            row['status'] = resolve_import_status(row['ho_status_raw'], row['void_raw'], row['notes'])
            handover = None
            try:
                handover = parse_recap_date(row['handover_raw'])
            except ValueError:
                warnings.append(f"Handover Date could not be read ({row['handover_raw']}).")
            if row['status'] == STATUS_DONE and not handover and row['doc_year']:
                handover = date(row['doc_year'], 1, 1)
                warnings.append(f"Approved without a Handover Date; 1 January {row['doc_year']} is used.")
            row['handover_date'] = handover.isoformat() if handover else ''
            completion = row['handover_date'] if row['status'] == STATUS_DONE else ''
            row['completion_display'] = format_date(date.fromisoformat(completion)) if completion else ''

            if row['doc_no'] and row['doc_period']:
                key = (row['doc_no'], row['doc_period'])
                if key in existing_numbers:
                    errors.append(f"PRJ number {row['doc_no']} of period {row['doc_period']} already exists in the database.")
                elif key in seen_numbers:
                    # Rows that repeat the number exactly were merged above; this one is written differently.
                    errors.append(f"Same PRJ Number as row {seen_numbers[key]} in period {row['doc_period']}, "
                                  "but written differently. Write it the same way so the rows are merged.")
                else:
                    seen_numbers[key] = row_index

            row['validation_errors'] = errors
            row['validation_warnings'] = warnings
            if row['prj_number_raw']:
                prjs_by_number[_match_key(row['prj_number_raw'])] = row
            current = row
            result['rows'].append(row)
            result['total_parsed'] += 1

        for row in result['rows']:
            del row['_source']
            set_preview_client(row, row.pop('_client'), row.pop('_client_warning'), allow_new_clients)
        # Counted at the end: continuation rows may still add warnings to a PRJ.
        result['total_errors'], result['total_warnings'] = count_preview_rows(result['rows'])
        return True, result, ''

    @staticmethod
    def _prj_of_row(row: Dict[str, str], item_description: str, current: Optional[Dict[str, Any]],
                    prjs_by_number: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """The PRJ an earlier row started that this row adds an item to, or None for a new PRJ.

        A row repeating a PRJ Number joins that PRJ wherever it started in the sheet.  A row
        with a blank number (a merged or not retyped cell) joins the PRJ of the row above
        when it has an item and does not name another client or job."""
        number = _match_key(row['prj_number_raw'])
        if number:
            return prjs_by_number.get(number)
        if not (current and item_description):
            return None
        same_prj = all(not row[field] or _match_key(row[field]) == _match_key(current['_source'][field])
                       for field in ('client_name', 'title'))
        return current if same_prj else None

    @staticmethod
    def _add_row(current: Dict[str, Any], row_index: int, row: Dict[str, str], item_cells: List[str]) -> None:
        """Add a later row's item to its PRJ; its other cells must match the PRJ's first row."""
        item = parse_item(*item_cells, current['validation_warnings'], row_index)
        if item:
            current['items'].append(item)
        current['row_end'] = row_index
        differing = [header for field, header in PRJ_ROW_FIELDS
                     if row[field] and _match_key(row[field]) != _match_key(current['_source'][field])]
        if differing:
            current['validation_warnings'].append(
                f"Row {row_index}: column {', '.join(differing)} differs from row {current['row_index']}; "
                f"row {current['row_index']} is used.")

    @staticmethod
    def _validate_confirmed_row(row: Dict[str, Any]) -> Optional[str]:
        """Re-validate a staged row server-side and attach the parsed dates; never trust the preview alone."""
        try:
            doc_no, doc_year = int(row.get('doc_no') or 0), int(row.get('doc_year') or 0)
        except (TypeError, ValueError):
            return 'Invalid PRJ Number.'
        if doc_no < 1 or not 1900 <= doc_year <= 2999:
            return 'Invalid PRJ Number.'
        if row.get('status') not in IMPORTED_STATUSES:
            return 'Invalid status.'
        items, error = clean_items(row.get('items', []))  # edited in the preview's item editor
        if error:
            return error
        row['items'] = items
        row['client_name'] = client_name_or_placeholder(row.get('client_name'))
        try:
            row['_handover'] = date.fromisoformat(row['handover_date']) if row.get('handover_date') else None
        except ValueError:
            return 'Invalid date.'
        row['_date'] = date(doc_year, 1, 1)
        row['doc_period'] = compute_doc_period(row['_date'])
        row['title'] = (row.get('title') or '').strip() or EMPTY_TEXT
        row['so_number'] = (row.get('so_number') or '').strip() or EMPTY_TEXT
        return None

    @staticmethod
    def _build_prj(row: Dict[str, Any], client: ClientSite) -> DocumentPRJ:
        status = row['status']
        doc_date = row['_date']
        if status == STATUS_DONE and not row['_handover']:
            row['_handover'] = doc_date  # Approved without a Handover Date: 1 January, as the preview said
        completion = row['_handover'] if status == STATUS_DONE else None
        prj = DocumentPRJ(
            doc_no=int(row['doc_no']),
            doc_period=row['doc_period'],
            doc_number_text=row['prj_number_raw'],
            date=doc_date,
            client_id=client.id,
            title=row['title'],
            so_number=row['so_number'],
            site_address=row.get('address') or client.address or '',
            notes=row.get('notes', ''),
            urgency_level=DEFAULT_URGENCY_LEVEL,
            estimated_calendar_days=DEFAULT_ESTIMATED_DAYS,
            status=status,
            actual_completion=completion,
        )
        # The HO is built here so it keeps the recap's own number; the
        # before_flush listener only creates one for a PRJ that has none.
        ho = HO(
            prj=prj,
            ho_number_text=row.get('ho_number_raw') or format_ho_number(prj.doc_no, doc_date),
            date=doc_date,
            status=HO_APPROVED if status == STATUS_DONE else status,
            notes='',
        )
        for order, item in enumerate(row.get('items') or [], start=1):
            prj.items.append(PRJItem(description=item['description'], qty=item['qty'], uom=item['uom'],
                                     display_order=order))
        if status == STATUS_DONE and row['_handover']:
            # The Handover Date is when the HO was uploaded, approved and completed.
            handover_at = datetime.combine(row['_handover'], time())
            ho.uploaded_at = handover_at
            ho.approved_at = handover_at
            ho.completion_date = row['_handover']
        return prj

    @staticmethod
    def execute_import(confirmed_rows: List[Dict[str, Any]], allow_new_clients: bool = True) -> ImportResult:
        result = ImportResult()

        valid_rows = []
        for row in confirmed_rows:
            error = PRJImportService._validate_confirmed_row(row)
            if error:
                result.errors.append(f"Row {row.get('row_index')}: {error} Skipped.")
                result.skipped_count += 1
                continue
            valid_rows.append(row)

        # Step 1: each row's client: the one picked in the preview, else matched from the
        # recap again, creating the ones that do not exist yet.  Existing clients are left untouched.
        resolver = ClientResolver()
        new_clients = []
        kept_rows = []
        for row in valid_rows:
            client, error = chosen_client(row)
            if error:
                result.errors.append(f"Row {row.get('row_index')}: {error} Skipped.")
                result.skipped_count += 1
                continue
            if client is None:
                client, _is_new, _warning = resolver.match_or_plan_prj_row(
                    row['client_name'], client_address(row['client_name'], row.get('address', '')))
            if client.id is None and not allow_new_clients:
                # Checked again here: the preview only offers the row once an existing client is picked.
                result.errors.append(f"Row {row.get('row_index')}: {NEW_CLIENT_NOT_ALLOWED} Skipped.")
                result.skipped_count += 1
                continue
            row['_client'] = client
            kept_rows.append(row)
            if client.id is None and client not in new_clients:
                new_clients.append(client)
        valid_rows = kept_rows
        if new_clients:
            db.session.add_all(new_clients)
            db.session.commit()
            result.clients_created = len(new_clients)

        # Step 2: insert each PRJ with its HO.
        for row in valid_rows:
            try:
                client = row['_client']
                exists = Document.query.filter_by(
                    document_type='prj', doc_no=int(row['doc_no']), doc_period=row['doc_period'],
                ).first()
                if exists:
                    result.errors.append(f"Row {row.get('row_index')}: duplicate found in the database, skipped.")
                    result.skipped_count += 1
                    continue

                db.session.add(PRJImportService._build_prj(row, client))
                db.session.commit()
                result.imported_count += 1
            except Exception as e:
                db.session.rollback()
                result.errors.append(f"Row {row.get('row_index')}: system error ({str(e)}).")
                result.skipped_count += 1

        return result
