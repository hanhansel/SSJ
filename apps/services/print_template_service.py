"""Print templates: which one is active, its source, and rendering it.

SSJ ships without built-in print layouts: every document type prints with an
HTML (Jinja2) template uploaded in Settings > Print Templates.  Until one is
uploaded, printing shows the variables a template can use.

Shared by the print routes (real documents) and the Settings page (download,
preview with test data, upload-then-confirm).
"""

import re
import shutil
import uuid
from pathlib import Path

from flask import current_app, render_template, render_template_string
from werkzeug.utils import secure_filename

from apps.core.paths import get_runtime_paths
from apps.models import (
    SystemSetting, ClientSite, MasterProject, DocumentSVC, DocumentXPRJ, DocumentPRJ,
    PRJItem, PRJTechnicianTask, HO, wib_now,
)

PRINT_TEMPLATE_SETTING_BY_TYPE = {
    'prj': 'print_template_prj',
    'svc': 'print_template_svc',
    'xprj': 'print_template_xprj',
    'ho': 'print_template_ho',
}
LEGACY_SVC_XPRJ_TEMPLATE_SETTING = 'print_template_svc_xprj'

PRINT_TEMPLATE_LABELS = {'svc': 'SVC', 'xprj': 'XPRJ', 'prj': 'PRJ', 'ho': 'HO'}

# What an uploaded template can print, per document type: (expression, meaning).
_CLIENT_VARIABLES = [
    ('doc.client.company_name', 'Client name'),
    ('doc.client.address', 'Client address'),
    ('doc.client.pic_name', 'Client contact person (PIC)'),
    ('doc.client.pic_phone', 'PIC phone number'),
    ('doc.client.client_type', 'Client type'),
]
_COMMON_VARIABLES = [
    ('doc.display_no', 'Document number'),
    ('doc.date', "Document date, e.g. {{ doc.date.strftime('%d-%m-%Y') }}"),
    ('doc.status', 'Status'),
    *_CLIENT_VARIABLES,
    ('doc.site_address', 'Site address of this document (may be empty: fall back to doc.client.address)'),
    ('doc.notes', 'Notes'),
    ('doc.estimated_calendar_days', 'Estimated days of work'),
    ('doc_type', "Document type code: 'svc', 'xprj', 'prj' or 'ho'"),
]
_SVC_VARIABLES = [
    ('doc.reference', 'Reference number'),
    ('doc.customer_complaint', 'Customer complaints, one per line'),
    ('doc.task_description', 'Tasks, one per line'),
]
PRINT_VARIABLES = {
    'svc': _COMMON_VARIABLES + _SVC_VARIABLES,
    'xprj': _COMMON_VARIABLES + _SVC_VARIABLES + [('doc.prj_parent.display_no', 'Number of the parent PRJ')],
    'prj': _COMMON_VARIABLES + [
        ('doc.title', 'PRJ title'),
        ('doc.so_number', 'SO number'),
        ('doc.urgency_level', '1 = Normal, 2 = Urgent'),
        ('doc.master_project.name', 'Master Project name (may be empty)'),
        ('doc.items', 'Work items: loop over them; each has .description, .qty, .uom, .display_order'),
        ('doc.technician_tasks', 'Technician tasks: loop over them; each has .description, .display_order'),
    ],
    'ho': [
        ('ho.display_no', 'HO number'),
        ('ho.date', 'HO date'),
        ('ho.status', 'Status'),
        ('ho.items', 'Items of its PRJ: loop over them; each has .description, .qty, .uom'),
        ('ho.prj', 'Its PRJ: every PRJ variable works as ho.prj..., e.g. ho.prj.display_no, '
                   'ho.prj.title, ho.prj.so_number, ho.prj.client.company_name, ho.prj.site_address'),
        ('doc_type', "Always 'ho'"),
    ],
}

STAGED_TOKEN_PATTERN = re.compile(r'^[0-9a-f]{32}$')


class PrintTemplateError(Exception):
    """A template that cannot be rendered; the message names the line when known."""


def active_template_name(doc_type):
    """Return the configured template, preserving the old combined setting as fallback."""
    setting_key = PRINT_TEMPLATE_SETTING_BY_TYPE.get(doc_type)
    if not setting_key:
        return ''
    setting = SystemSetting.query.filter_by(key=setting_key).first()
    if setting is not None:
        return setting.value
    if doc_type in ('svc', 'xprj'):
        return SystemSetting.get_val(LEGACY_SVC_XPRJ_TEMPLATE_SETTING)
    return ''


def custom_template_path(doc_type):
    """Path of the active uploaded template, or None when unset or missing on disk."""
    name = active_template_name(doc_type)
    if not name:
        return None
    candidates = [
        Path(current_app.config['PRINT_TEMPLATE_FOLDER']) / name,
        # Read-only compatibility for templates uploaded before the runtime
        # layout separated them from other evidence files.
        Path(current_app.config['UPLOAD_FOLDER']) / 'templates' / name,
    ]
    return next((path for path in candidates if path.is_file()), None)


def starter_source(doc_type):
    """A bare starting point for a new template: the variable list and a minimal page."""
    label = PRINT_TEMPLATE_LABELS[doc_type]
    record = 'ho' if doc_type == 'ho' else 'doc'
    width = max(len(name) for name, _ in PRINT_VARIABLES[doc_type])
    variables = '\n'.join(f'     {name.ljust(width)}  {meaning}' for name, meaning in PRINT_VARIABLES[doc_type])
    return (
        f'{{# SSJ print template for {label} documents.\n'
        f'   Design any HTML page you like; it is rendered with Jinja2 (https://jinja.palletsprojects.com/).\n'
        f'   Print a value with {{{{ variable }}}}, e.g. {{{{ {record}.display_no }}}}.\n\n'
        f'   Variables available:\n{variables}\n#}}\n'
        f'<!doctype html>\n<html lang="en">\n<head>\n'
        f'    <meta charset="utf-8">\n    <title>{label} {{{{ {record}.display_no }}}}</title>\n'
        f'</head>\n<body>\n    <h1>{label} {{{{ {record}.display_no }}}}</h1>\n</body>\n</html>\n'
    )


def template_source(doc_type):
    """(download filename, HTML source): the uploaded template, or the starter file without one."""
    path = custom_template_path(doc_type)
    if path:
        return path.name, path.read_text(encoding='utf-8')
    return f'template_{doc_type}_starter.html', starter_source(doc_type)


def _context(doc_type, record):
    if doc_type == 'ho':
        return {'ho': record, 'doc': record, 'doc_type': 'ho'}
    return {'doc': record, 'doc_type': doc_type}


def _render_source(source, context):
    try:
        return render_template_string(source, **context)
    except Exception as exc:
        line = getattr(exc, 'lineno', None)
        tb = exc.__traceback__
        while line is None and tb is not None:
            if tb.tb_frame.f_code.co_filename == '<template>':
                line = tb.tb_lineno
            tb = tb.tb_next
        prefix = f'Line {line}: ' if line else ''
        raise PrintTemplateError(f'{prefix}{exc}') from exc


def render_custom_print(doc_type, record):
    """Render the uploaded template for a real document, or None when none is uploaded."""
    path = custom_template_path(doc_type)
    if not path:
        return None
    return render_template_string(path.read_text(encoding='utf-8'), **_context(doc_type, record))


def render_missing_print(doc_type):
    """The page printed while no template is uploaded: how to add one and its variables."""
    return render_template('documents/print_missing.html', doc_type=doc_type,
                           label=PRINT_TEMPLATE_LABELS[doc_type], variables=PRINT_VARIABLES[doc_type])


def render_active_print(doc_type, record):
    """Render the uploaded template, or the no-template page when there is none."""
    path = custom_template_path(doc_type)
    if path:
        return _render_source(path.read_text(encoding='utf-8'), _context(doc_type, record))
    return render_missing_print(doc_type)


def render_source_with_sample(doc_type, source):
    """Render an uploaded template with test data; raises PrintTemplateError on failure."""
    return _render_source(source, _context(doc_type, sample_record(doc_type)))


# ------------------------------------------------------------------
# Test data: transient objects that are never added to the session.
# Every field a template may print is filled, so the preview shows where it lands.
# ------------------------------------------------------------------
def _sample_client():
    return ClientSite(
        company_name='Example Client Ltd',
        address='1 Example Street, Example City',
        client_type='Contract',
        pic_name='Alex Example',
        pic_phone='0800000000',
    )


def _sample_prj(client, today):
    prj = DocumentPRJ(
        id=0, document_type='prj', doc_no=1, date=today, client=client,
        site_address=client.address, status='Online',
        title='Example Project', so_number='SO-EXAMPLE-001',
        notes='Example notes', estimated_calendar_days=3, urgency_level=1,
        master_project=MasterProject(name='Example Master Project', client=client),
    )
    for order, (description, qty, uom) in enumerate([
        ('Example item A', 8, 'Unit'),
        ('Example item B', 120, 'Meter'),
        ('Example item C', 1, 'Set'),
    ], start=1):
        PRJItem(prj=prj, description=description, qty=qty, uom=uom, display_order=order)
    for order, description in enumerate([
        'Example task one',
        'Example task two',
        'Example task three',
    ], start=1):
        PRJTechnicianTask(prj=prj, description=description, display_order=order)
    return prj


def sample_record(doc_type):
    today = wib_now().date()
    client = _sample_client()
    if doc_type in ('svc', 'xprj'):
        model = DocumentSVC if doc_type == 'svc' else DocumentXPRJ
        doc = model(
            id=0, document_type=doc_type, doc_no=1, date=today, client=client,
            site_address=client.address, status='Online', reference='REF-EXAMPLE-001',
            customer_complaint='Example complaint one\nExample complaint two',
            task_description='Example task one\nExample task two',
            notes='Example notes', estimated_calendar_days=2,
        )
        if doc_type == 'xprj':
            doc.prj_parent = _sample_prj(client, today)
        return doc
    prj = _sample_prj(client, today)
    if doc_type == 'ho':
        return HO(id=0, prj=prj, date=today, status='Online', notes='')
    return prj


# ------------------------------------------------------------------
# Staging: an uploaded template waits here until the admin confirms it.
# ------------------------------------------------------------------
def _staging_dir():
    path = get_runtime_paths(current_app.config['APP_ENV']).staging_dir
    path.mkdir(parents=True, exist_ok=True)
    return path


def staged_path(token):
    if not STAGED_TOKEN_PATTERN.match(token or ''):
        return None
    path = _staging_dir() / f'print_template_{token}.html'
    return path if path.is_file() else None


def stage_template(source):
    token = uuid.uuid4().hex
    (_staging_dir() / f'print_template_{token}.html').write_text(source, encoding='utf-8')
    return token


def versioned_filename(doc_type, original_filename):
    """Create a readable, collision-resistant filename for an uploaded template."""
    safe_name = secure_filename(original_filename) or f'template_{doc_type}.html'
    timestamp = wib_now().strftime('%Y%m%d_%H%M%S')
    return f'{doc_type}_{timestamp}_{uuid.uuid4().hex[:8]}_{safe_name}'


def apply_staged(doc_type, token, original_filename):
    """Move a staged template into the template folder and make it active."""
    path = staged_path(token)
    if not path:
        return False
    folder = Path(current_app.config['PRINT_TEMPLATE_FOLDER'])
    folder.mkdir(parents=True, exist_ok=True)
    filename = versioned_filename(doc_type, original_filename)
    # shutil.move also works when staging and the template folder are on different drives.
    shutil.move(str(path), folder / filename)
    SystemSetting.set_val(PRINT_TEMPLATE_SETTING_BY_TYPE[doc_type], filename, category='print_template')
    return True


def discard_staged(token):
    path = staged_path(token)
    if path:
        path.unlink(missing_ok=True)
