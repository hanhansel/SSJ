import os
import re
import uuid
from datetime import datetime
from werkzeug.utils import secure_filename
from apps.models import db, DocumentSVC, DocumentXPRJ, DocumentPRJ, SystemSetting, wib_now

# Full registry: doc_type -> (Model, label, color_badge)
DOC_REGISTRY = {
    'svc':  (DocumentSVC,  'SVC',   '#3b82f6'),   # Blue
    'xprj': (DocumentXPRJ, 'XPRJ', '#f59e0b'),   # Amber/Orange
    'prj':  (DocumentPRJ,  'PRJ',   '#10b981'),   # Green (Endoskeleton)
}

ROMAN_MONTHS = {
    1: 'I', 2: 'II', 3: 'III', 4: 'IV', 5: 'V', 6: 'VI',
    7: 'VII', 8: 'VIII', 9: 'IX', 10: 'X', 11: 'XI', 12: 'XII'
}


def get_doc_model(doc_type):
    """Return Model class for doc_type, or None."""
    entry = DOC_REGISTRY.get(doc_type)
    return entry[0] if entry else None


def get_doc_label(doc_type):
    """Return human-readable label ('SVC', 'XPRJ', 'PRJ')."""
    entry = DOC_REGISTRY.get(doc_type)
    return entry[1] if entry else doc_type.upper()


def get_doc_color(doc_type):
    """Return theme color for document badge."""
    entry = DOC_REGISTRY.get(doc_type)
    return entry[2] if entry else '#64748b'


def fetch_doc(doc_type, doc_id):
    """Fetch document instance safely by type and id."""
    Model = get_doc_model(doc_type)
    if not Model:
        return None
    return db.session.get(Model, doc_id)


def get_next_sequence_number(doc_type, doc_date):
    """Calculate the next sequence number for a document type based on SystemSetting rules.

    SVC and XPRJ share a unified sequence counter pool.  A HO has no
    sequence of its own: it carries its PRJ's number.  Void documents keep
    their number, so the next number is always after the highest one issued.
    """
    reset_period = SystemSetting.get_val('doc_no_reset_period', 'Yearly')
    year_str = doc_date.strftime('%Y')
    month_str = doc_date.strftime('%m')

    if doc_type == 'ho':
        doc_type = 'prj'

    # Shared sequence pool for SVC and XPRJ
    if doc_type in ['svc', 'xprj']:
        models = [DocumentSVC, DocumentXPRJ]
    else:
        Model = get_doc_model(doc_type)
        models = [Model] if Model else []

    if not models:
        return 1

    # Efficiently get the last-created document in each participating model
    # within the relevant time window (avoid loading all rows into memory)
    max_num = 0

    for m in models:
        # Build a base query filtered to the relevant period
        base_q = m.query.filter(m.doc_no.isnot(None), m.date.isnot(None))
        if reset_period == 'Monthly':
            base_q = base_q.filter(
                db.extract('year', m.date) == int(year_str),
                db.extract('month', m.date) == int(month_str)
            )
        elif reset_period == 'Yearly':
            base_q = base_q.filter(
                db.extract('year', m.date) == int(year_str)
            )
        # 'Never': no date filter

        # ``doc_no`` is the integer business sequence, so query its maximum directly.
        highest_for_model = base_q.with_entities(db.func.max(m.doc_no)).scalar() or 0
        max_num = max(max_num, highest_for_model)

    return max_num + 1


def compute_doc_period(doc_date):
    """Compute the doc_period value based on the current SystemSetting reset period.

    Returns a short string used as part of the unique constraint
    ``(document_type, doc_no, doc_period)`` so that documents with the
    same sequence number from different reset windows can coexist.

    * ``Yearly``  → ``"2026"``
    * ``Monthly`` → ``"2026-06"``
    * ``Never``   → ``"all"``
    """
    reset_period = SystemSetting.get_val('doc_no_reset_period', 'Yearly')
    if reset_period == 'Monthly':
        return doc_date.strftime('%Y-%m')
    elif reset_period == 'Yearly':
        return doc_date.strftime('%Y')
    return 'all'


def generate_doc_number(doc_type, doc_date=None, custom_seq=None):
    """Generate a dynamic document number using templates from SystemSetting."""
    if not doc_date:
        doc_date = wib_now().date()

    if isinstance(doc_date, str):
        try:
            doc_date = datetime.strptime(doc_date, '%Y-%m-%d').date()
        except Exception:
            doc_date = wib_now().date()

    # Read configuration
    default_formats = {
        'svc': '{NO}/{MM}/SVC/{YY}',
        'xprj': '{NO}/{MM}/XPRJ/{YY}',
        'prj': '{NO}/PRJ/{YY}',
        'ho': '{NO}/HO/{YY}',
    }
    format_key = f"{doc_type}_format"
    template = SystemSetting.get_val(format_key, default_formats.get(doc_type, '{NO}/DOC/{YY}'))
    padding_str = SystemSetting.get_val('doc_no_padding', '3')
    
    try:
        padding = int(padding_str)
    except ValueError:
        padding = 3

    # Calculate sequence
    seq_num = custom_seq if custom_seq is not None else get_next_sequence_number(doc_type, doc_date)
    formatted_seq = str(seq_num).zfill(padding)

    # Prepare variable replacements
    mm = doc_date.strftime('%m')
    roman_mm = ROMAN_MONTHS.get(doc_date.month, mm)
    yy = doc_date.strftime('%y')
    yyyy = doc_date.strftime('%Y')

    result = template.replace('{NO}', formatted_seq)\
                     .replace('{MM}', mm)\
                     .replace('{ROMAN_MM}', roman_mm)\
                     .replace('{YY}', yy)\
                     .replace('{YYYY}', yyyy)

    return result, seq_num


def format_document_number(doc_type, doc_no, doc_date=None):
    """Format a stored numeric sequence into its printable document number."""
    try:
        sequence = int(doc_no)
    except (TypeError, ValueError):
        return str(doc_no or '')

    formatted_number, _ = generate_doc_number(doc_type, doc_date, custom_seq=sequence)
    return formatted_number


def format_ho_number(prj_doc_no, prj_date=None):
    """A HO is numbered with its PRJ's sequence and date, in the HO format."""
    return format_document_number('ho', prj_doc_no, prj_date)


IMAGE_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.webp')
EVIDENCE_EXTENSIONS = IMAGE_EXTENSIONS + ('.pdf',)


def is_allowed_upload(filename, allowed=EVIDENCE_EXTENSIONS):
    """Accept only evidence formats that are safe to serve back inline."""
    return bool(filename) and os.path.splitext(filename.lower())[1] in allowed


def clean_path_segment(val):
    """Sanitizes text for safe directory/file name usage."""
    if not val:
        return 'unknown'
    # Replace slashes, backslashes, colons, and illegal characters with underscores
    cleaned = re.sub(r'[\\/:*?"<>|\s]+', '_', str(val).strip())
    cleaned = cleaned.strip('._')
    return cleaned or 'unknown'


def resolve_document_upload_path(doc_type, doc, user_id=None, original_filename='', now=None):
    """
    Resolves dynamic upload directory and filename for document work reports.
    Supported tokens:
      Folder: {DOC_TYPE}, {DOC_NO}, {YEAR}, {YYYY}, {MONTH}, {MM}, {DAY}, {DD}, {CLIENT_NAME}, {USER_ID}, {TIMESTAMP}
      File:   {DOC_TYPE}, {DOC_NO}, {YEAR}, {YYYY}, {MONTH}, {MM}, {DAY}, {DD}, {USER_ID}, {TIMESTAMP}, {UUID}, {ORIGINAL_NAME}
    """
    if now is None:
        now = wib_now()

    doc_date = doc.date if hasattr(doc, 'date') and doc.date else now.date()
    year_str = doc_date.strftime('%Y')
    month_str = doc_date.strftime('%m')
    day_str = doc_date.strftime('%d')
    doc_type_clean = clean_path_segment(doc_type.lower())
    doc_no_clean = clean_path_segment(getattr(doc, 'display_no', getattr(doc, 'doc_no', f"doc_{doc.id}")))
    client_name = 'no_client'
    if hasattr(doc, 'client') and doc.client and getattr(doc.client, 'company_name', None):
        client_name = clean_path_segment(doc.client.company_name)
    user_id_str = str(user_id) if user_id is not None else '0'
    timestamp_str = now.strftime('%Y%m%d_%H%M%S')
    unique_token = uuid.uuid4().hex[:12]
    orig_clean = secure_filename(original_filename) if original_filename else f"photo_{timestamp_str}.jpg"

    # Get templates from SystemSetting
    folder_template = SystemSetting.get_val('doc_upload_folder_format', 'documents/{DOC_TYPE}/{YEAR}/{DOC_NO}')
    file_template = SystemSetting.get_val('doc_upload_filename_format', '{TIMESTAMP}_{UUID}_{ORIGINAL_NAME}')

    # Process folder path
    folder_path = folder_template.replace('{DOC_TYPE}', doc_type_clean)\
                                 .replace('{DOC_NO}', doc_no_clean)\
                                 .replace('{YEAR}', year_str)\
                                 .replace('{YYYY}', year_str)\
                                 .replace('{MONTH}', month_str)\
                                 .replace('{MM}', month_str)\
                                 .replace('{DAY}', day_str)\
                                 .replace('{DD}', day_str)\
                                 .replace('{CLIENT_NAME}', client_name)\
                                 .replace('{USER_ID}', user_id_str)\
                                 .replace('{TIMESTAMP}', timestamp_str)

    # Sanitize each segment of the folder path (allow forward slash as directory separator)
    folder_segments = [clean_path_segment(s) for s in folder_path.replace('\\', '/').split('/') if s.strip()]
    final_rel_folder = "/".join(folder_segments) if folder_segments else "documents"

    # Process filename
    filename = file_template.replace('{DOC_TYPE}', doc_type_clean)\
                            .replace('{DOC_NO}', doc_no_clean)\
                            .replace('{YEAR}', year_str)\
                            .replace('{YYYY}', year_str)\
                            .replace('{MONTH}', month_str)\
                            .replace('{MM}', month_str)\
                            .replace('{DAY}', day_str)\
                            .replace('{DD}', day_str)\
                            .replace('{CLIENT_NAME}', client_name)\
                            .replace('{USER_ID}', user_id_str)\
                            .replace('{TIMESTAMP}', timestamp_str)\
                            .replace('{UUID}', unique_token)\
                            .replace('{ORIGINAL_NAME}', orig_clean)

    # Preserve administrators' readable naming format while making every stored
    # file collision-safe, even when two uploads use the same source filename.
    if '{UUID}' not in file_template:
        name_root, name_ext = os.path.splitext(filename)
        filename = f'{name_root}_{unique_token}{name_ext}'

    final_filename = clean_path_segment(filename)
    if not final_filename.lower().endswith(EVIDENCE_EXTENSIONS):
        ext = os.path.splitext(orig_clean)[1].lower()
        final_filename += ext if ext in EVIDENCE_EXTENSIONS else '.jpg'

    return final_rel_folder, final_filename
