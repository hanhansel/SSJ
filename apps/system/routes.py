import json
from io import BytesIO
from flask import Blueprint, render_template, session, redirect, url_for, request, flash, jsonify, send_file, abort
from werkzeug.security import generate_password_hash
from datetime import datetime, timedelta
import pandas as pd
from apps.models import (
    db, Role, User, ClientSite, Document, DocumentSVC, DocumentXPRJ, DocumentPRJ, MasterProject, ScheduleBlock,
    SystemSetting, wib_now, ACTIVE_STATUSES, STATUS_ONLINE, STATUS_HOLD, DEFAULT_SYSTEM_SETTINGS,
    MENUS, ALL_MENUS, MENU_DESCRIPTIONS, CROSS_MENU_ACTIONS, MENU_DOCUMENTS, MENU_STORAGE, MENU_SCHEDULING, MENU_SETTINGS, MENU_ACCESS,
    ROLE_OFFICE, ROLE_TECHNICIAN,
)
from apps.services.import_service import XPRJ_CLIENT_TYPE, clean_cell_text, normalize_phone
from apps.services import export_service, print_template_service as print_templates
from apps.core.registry import generate_doc_number
from apps.core.auth import action_required, current_user, menu_required, office_required

MIN_PASSWORD_LENGTH = 8
# First-cell values that mark a header row in the client import.
CLIENT_NAME_HEADERS = ('name', 'company name', 'client name')

system_bp = Blueprint('system', __name__, url_prefix='/system')




# ==========================================
# 1. EXECUTIVE DASHBOARD
# ==========================================
@system_bp.route('/')
@system_bp.route('/dashboard')
@office_required
def dashboard():
    """Every office account lands here; it only shows the parts of the menus it may use."""
    now = wib_now()
    today = now.date()
    user = current_user()
    context = {'now': now, 'today': today}

    if user.can(MENU_SCHEDULING):
        # Unscheduled = the scheduler's "Not Scheduled" backlog: Online documents with no
        # schedule block yet. Pending Approval is excluded because it can't be scheduled,
        # and so are PRJs of a Master Project on Hold.
        # Leave blocks have no document_id; NOT IN over a NULL would match nothing.
        scheduled_ids = db.select(ScheduleBlock.document_id).where(ScheduleBlock.document_id.isnot(None))
        held_project_prjs = db.select(DocumentPRJ.id).join(
            MasterProject, DocumentPRJ.master_project_id == MasterProject.id).where(MasterProject.status == STATUS_HOLD)
        unscheduled_query = Document.query.filter(
            Document.status == STATUS_ONLINE,
            Document.document_type.in_(('svc', 'xprj', 'prj')),
            Document.id.notin_(scheduled_ids),
            Document.id.notin_(held_project_prjs),
        )
        context['unscheduled_docs'] = unscheduled_query.count()
        context['unscheduled_mc'] = unscheduled_query.join(ClientSite, Document.client_id == ClientSite.id).filter(
            Document.document_type == 'svc',
            db.func.lower(db.func.trim(ClientSite.client_type)) == 'contract',
        ).count()

        # Blocks running today: any block that started on/before today (up to a
        # year back, covering long assignments) and has not ended yet.
        candidate_blocks = ScheduleBlock.query.filter(
            ScheduleBlock.start_date <= today,
            ScheduleBlock.start_date >= today - timedelta(days=366),
        ).all()
        context['todays_blocks'] = [
            b for b in candidate_blocks
            if b.start_date + timedelta(days=b.days_duration - 1) >= today
        ]

    if user.can(MENU_DOCUMENTS):
        # Active = Online or Pending Approval, the same set the active list pages show.
        context['active_svc'] = DocumentSVC.query.filter(DocumentSVC.status.in_(ACTIVE_STATUSES)).count()
        context['active_prj'] = DocumentPRJ.query.filter(DocumentPRJ.status.in_(ACTIVE_STATUSES)).count()
        context['active_xprj'] = DocumentXPRJ.query.filter(DocumentXPRJ.status.in_(ACTIVE_STATUSES)).count()
        context['recent_svc'] = DocumentSVC.query.order_by(DocumentSVC.created_at.desc()).limit(5).all()
        context['recent_xprj'] = DocumentXPRJ.query.order_by(DocumentXPRJ.created_at.desc()).limit(5).all()

    return render_template('dashboard/executive_dashboard.html', **context)


# ==========================================
# 2. DYNAMIC SYSTEM SETTINGS
# ==========================================
@system_bp.route('/settings')
@menu_required(MENU_SETTINGS)
def settings():
    settings_list = SystemSetting.query.order_by(SystemSetting.category, SystemSetting.id).all()
    settings_dict = {s.key: s.value for s in settings_list}
    # Uploaded template file name per type, or None while none is uploaded.
    active_templates = {}
    for doc_type in PRINT_TEMPLATE_LABELS:
        path = print_templates.custom_template_path(doc_type)
        active_templates[doc_type] = path.name if path else None

    return render_template('system/settings.html',
        settings=settings_dict,
        print_template_labels=PRINT_TEMPLATE_LABELS,
        active_templates=active_templates,
    )


@system_bp.route('/settings/update', methods=['POST'])
@menu_required(MENU_SETTINGS)
def update_settings():
    form_data = request.form.to_dict()

    for key, value in form_data.items():
        if key.startswith('csrf_'):
            continue
        SystemSetting.set_val(key, value.strip())

    flash('System settings updated.', 'success')
    return redirect(url_for('system.settings'))


@system_bp.route('/settings/reset-defaults', methods=['POST'])
@menu_required(MENU_SETTINGS)
def reset_settings_to_default():
    """Restore every Settings value and clear every uploaded print template selection."""
    for key, value, category, description in DEFAULT_SYSTEM_SETTINGS:
        SystemSetting.set_val(key, value, category=category, description=description)
    # Uploaded template files stay on disk; only the active selection is cleared.
    template_keys = list(print_templates.PRINT_TEMPLATE_SETTING_BY_TYPE.values())
    template_keys.append(print_templates.LEGACY_SVC_XPRJ_TEMPLATE_SETTING)
    for setting_key in template_keys:
        SystemSetting.set_val(setting_key, '', category='print_template')
    flash('Every setting has been reset to its default.', 'success')
    return redirect(url_for('system.settings'))


# ==========================================
# 2b. PRINT TEMPLATES (download / preview / upload-then-confirm)
# ==========================================
PRINT_TEMPLATE_LABELS = print_templates.PRINT_TEMPLATE_LABELS


def _print_template_type_or_404(doc_type):
    if doc_type not in PRINT_TEMPLATE_LABELS:
        abort(404)
    return doc_type


@system_bp.route('/settings/print-template/<doc_type>/download')
@menu_required(MENU_SETTINGS)
def download_print_template(doc_type):
    """Download the template in use, or the starter file while none is uploaded."""
    filename, source = print_templates.template_source(_print_template_type_or_404(doc_type))
    return send_file(BytesIO(source.encode('utf-8')), mimetype='text/html',
                     as_attachment=True, download_name=filename)


@system_bp.route('/settings/print-template/<doc_type>/preview')
@menu_required(MENU_SETTINGS)
def preview_print_template(doc_type):
    """Render a template with test data: a staged upload (?token=) or the active one."""
    _print_template_type_or_404(doc_type)
    try:
        token = request.args.get('token')
        if token:
            path = print_templates.staged_path(token)
            if not path:
                abort(404)
            return print_templates.render_source_with_sample(doc_type, path.read_text(encoding='utf-8'))
        return print_templates.render_active_print(doc_type, print_templates.sample_record(doc_type))
    except print_templates.PrintTemplateError as exc:
        return render_template('system/print_template_error.html', error=str(exc)), 422


@system_bp.route('/settings/print-template/<doc_type>/stage', methods=['POST'])
@menu_required(MENU_SETTINGS)
def stage_print_template(doc_type):
    """Check an uploaded template with test data and hold it until the admin confirms."""
    _print_template_type_or_404(doc_type)
    file = request.files.get('template')
    if not file or not file.filename:
        return jsonify({'ok': False, 'error': 'No file selected.'}), 400
    if not file.filename.lower().endswith('.html'):
        return jsonify({'ok': False, 'error': 'The template must be an .html file.'}), 400
    try:
        source = file.read().decode('utf-8')
    except UnicodeDecodeError:
        return jsonify({'ok': False, 'error': 'The file must be saved with UTF-8 encoding.'}), 400
    try:
        print_templates.render_source_with_sample(doc_type, source)
    except print_templates.PrintTemplateError as exc:
        return jsonify({'ok': False, 'error': f'The template cannot be used. {exc}'}), 422

    token = print_templates.stage_template(source)
    staged = session.get('staged_print_templates', {})
    staged[token] = {'doc_type': doc_type, 'filename': file.filename}
    session['staged_print_templates'] = staged
    return jsonify({
        'ok': True,
        'token': token,
        'preview_url': url_for('system.preview_print_template', doc_type=doc_type, token=token),
    })


def _pop_staged(doc_type, token):
    staged = session.get('staged_print_templates', {})
    entry = staged.pop(token, None)
    session['staged_print_templates'] = staged
    return entry if entry and entry['doc_type'] == doc_type else None


@system_bp.route('/settings/print-template/<doc_type>/apply', methods=['POST'])
@menu_required(MENU_SETTINGS)
def apply_print_template(doc_type):
    _print_template_type_or_404(doc_type)
    token = (request.get_json(silent=True) or {}).get('token', '')
    entry = _pop_staged(doc_type, token)
    if not entry or not print_templates.apply_staged(doc_type, token, entry['filename']):
        return jsonify({'ok': False, 'error': 'The uploaded template is no longer available. Please upload it again.'}), 404
    flash(f'{PRINT_TEMPLATE_LABELS[doc_type]} print template updated.', 'success')
    return jsonify({'ok': True})


@system_bp.route('/settings/print-template/<doc_type>/discard', methods=['POST'])
@menu_required(MENU_SETTINGS)
def discard_print_template(doc_type):
    _print_template_type_or_404(doc_type)
    token = (request.get_json(silent=True) or {}).get('token', '')
    if _pop_staged(doc_type, token):
        print_templates.discard_staged(token)
    return jsonify({'ok': True})


@system_bp.route('/settings/print-template/<doc_type>/reset', methods=['POST'])
@menu_required(MENU_SETTINGS)
def reset_print_template(doc_type):
    """Stop using the uploaded template of one document type (the file stays on disk)."""
    _print_template_type_or_404(doc_type)
    SystemSetting.set_val(print_templates.PRINT_TEMPLATE_SETTING_BY_TYPE[doc_type], '', category='print_template')
    flash(f'{PRINT_TEMPLATE_LABELS[doc_type]} print template removed.', 'success')
    return redirect(url_for('system.settings'))


@system_bp.route('/api/preview-doc-no')
@menu_required(MENU_DOCUMENTS)
def api_preview_doc_no():
    doc_type = request.args.get('doc_type', 'svc').lower()
    date_str = request.args.get('date')

    if date_str:
        try:
            target_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        except ValueError:
            target_date = wib_now().date()
    else:
        target_date = wib_now().date()

    doc_no, seq = generate_doc_number(doc_type, target_date)
    return jsonify({
        'doc_no': doc_no,
        'sequence': seq,
        'date': target_date.strftime('%Y-%m-%d')
    })


def get_all_client_types():
    base_types = ['Contract', 'Survey', 'Temp', 'On-Call']
    saved_json = SystemSetting.get_val('custom_client_types', '[]')
    try:
        custom_types = json.loads(saved_json)
    except Exception:
        custom_types = []

    try:
        db_types = [r[0] for r in db.session.query(ClientSite.client_type).distinct().all() if r[0]]
    except Exception:
        db_types = []

    all_types = []
    for t in base_types + custom_types + db_types:
        if t and t.strip() and t.strip() not in all_types:
            all_types.append(t.strip())
    return all_types


# ==========================================
# 3. CLIENT SITES & QUICK ADD
# ==========================================
@system_bp.route('/clients')
@menu_required(MENU_STORAGE)
def clients():
    clients_list = ClientSite.query.order_by(ClientSite.company_name).all()
    client_types = get_all_client_types()
    return render_template('system/clients.html', clients=clients_list, client_types=client_types)


@system_bp.route('/api/client-types/add', methods=['POST'])
@menu_required(MENU_STORAGE)
def api_add_client_type():
    data = request.get_json() or {}
    new_type = (data.get('type_name') or '').strip()
    if not new_type:
        return jsonify({'success': False, 'message': 'The type name cannot be empty'}), 400

    saved_json = SystemSetting.get_val('custom_client_types', '[]')
    try:
        custom_types = json.loads(saved_json)
    except Exception:
        custom_types = []

    if new_type not in custom_types:
        custom_types.append(new_type)
        SystemSetting.set_val('custom_client_types', json.dumps(custom_types), category='clients', description='Custom Client / Customer Types')

    return jsonify({'success': True, 'type_name': new_type, 'all_types': get_all_client_types()})


@system_bp.route('/clients/create', methods=['POST'])
@menu_required(MENU_STORAGE)
def create_client():
    company_name = (request.form.get('company_name') or '').strip()
    address = (request.form.get('address') or '').strip()
    client_type = (request.form.get('client_type') or '').strip() or None
    pic_name = (request.form.get('pic_name') or '').strip()
    pic_phone = (request.form.get('pic_phone') or '').strip()
    lat_val = (request.form.get('latitude') or '').strip()
    lng_val = (request.form.get('longitude') or '').strip()

    if not company_name:
        flash('The Client / Company Name is required.', 'danger')
        return redirect(url_for('system.clients'))

    lat = None
    lng = None
    if lat_val:
        try:
            lat = float(lat_val)
        except ValueError:
            pass
    if lng_val:
        try:
            lng = float(lng_val)
        except ValueError:
            pass

    client = ClientSite(
        company_name=company_name,
        address=address,
        client_type=client_type,
        latitude=lat,
        longitude=lng,
        pic_name=pic_name,
        pic_phone=pic_phone
    )
    db.session.add(client)
    db.session.commit()
    flash(f'Client "{company_name}" added.', 'success')
    return redirect(url_for('system.clients'))


@system_bp.route('/clients/update/<int:client_id>', methods=['POST'])
@menu_required(MENU_STORAGE)
def update_client(client_id):
    client = db.get_or_404(ClientSite, client_id)
    company_name = (request.form.get('company_name') or '').strip()
    if not company_name:
        flash('The Client / Company Name cannot be empty.', 'danger')
        return redirect(url_for('system.clients'))

    client.company_name = company_name
    client.address = (request.form.get('address') or '').strip()
    client.client_type = (request.form.get('client_type') or '').strip() or None
    client.pic_name = (request.form.get('pic_name') or '').strip()
    client.pic_phone = (request.form.get('pic_phone') or '').strip()

    lat_val = (request.form.get('latitude') or '').strip()
    lng_val = (request.form.get('longitude') or '').strip()
    try:
        client.latitude = float(lat_val) if lat_val else None
    except ValueError:
        client.latitude = None
    try:
        client.longitude = float(lng_val) if lng_val else None
    except ValueError:
        client.longitude = None

    db.session.commit()
    flash(f'Client "{client.company_name}" updated.', 'success')
    return redirect(url_for('system.clients'))


@system_bp.route('/api/clients/quick-add', methods=['POST'])
@action_required('add_client_from_document')
def api_quick_add_client():
    data = request.get_json() or request.form
    company_name = (data.get('company_name') or '').strip()
    address = (data.get('address') or '').strip()
    client_type = (data.get('client_type') or '').strip() or None
    pic_name = (data.get('pic_name') or '').strip()
    pic_phone = (data.get('pic_phone') or '').strip()
    lat_val = data.get('latitude')
    lng_val = data.get('longitude')

    if not company_name:
        return jsonify({'success': False, 'message': 'The company name is required'}), 400
    # Quick add from the document generator: a client added while making an SVC
    # needs a type; one added for an PRJ or XPRJ has none.
    doc_type = (data.get('doc_type') or '').strip().lower()
    if doc_type == 'svc' and not client_type:
        return jsonify({'success': False, 'message': 'A Client Type must be chosen for an SVC client'}), 400
    if doc_type in ('prj', 'xprj'):
        client_type = None

    lat = None
    lng = None
    if lat_val:
        try:
            lat = float(lat_val)
        except (ValueError, TypeError):
            pass
    if lng_val:
        try:
            lng = float(lng_val)
        except (ValueError, TypeError):
            pass

    client = ClientSite(
        company_name=company_name,
        address=address,
        client_type=client_type,
        latitude=lat,
        longitude=lng,
        pic_name=pic_name,
        pic_phone=pic_phone
    )
    db.session.add(client)
    db.session.commit()

    return jsonify({
        'success': True,
        'client': {
            'id': client.id,
            'company_name': client.company_name,
            'address': client.address or '',
            'client_type': client.client_type or '',
            'latitude': client.latitude,
            'longitude': client.longitude,
            'pic_name': client.pic_name or '',
            'pic_phone': client.pic_phone or ''
        }
    })


@system_bp.route('/clients/import', methods=['POST'])
@menu_required(MENU_STORAGE)
def import_clients():
    if 'file' not in request.files:
        flash('No file selected.', 'danger')
        return redirect(url_for('system.clients'))

    file = request.files['file']
    if not file or file.filename == '':
        flash('No file selected.', 'danger')
        return redirect(url_for('system.clients'))

    if not (file.filename.lower().endswith('.xlsx') or file.filename.lower().endswith('.xls')):
        flash('Unsupported file format. Please upload an Excel file (.xlsx).', 'danger')
        return redirect(url_for('system.clients'))

    try:
        # Read every cell as text: a phone stored as a number would otherwise come back as 81234567890.0.
        df = pd.read_excel(file, dtype=str)
        if df.empty:
            flash('The Excel file is empty or has no data.', 'warning')
            return redirect(url_for('system.clients'))

        imported_count = 0
        skipped_count = 0
        seen_in_batch = set()

        for idx, row in df.iterrows():
            # Text cells lose Excel escapes such as "_x000D_" and stray line breaks.
            vals = [clean_cell_text(v) if isinstance(v, str) else v for v in row.values]
            if len(vals) == 0:
                continue

            # Check whether every cell in the row is empty / blank / nan (empty rows are ignored, not counted as skipped)
            has_meaningful_data = any(
                pd.notna(v) and str(v).strip() and str(v).strip().lower() not in ['nan', 'none', 'null', '-']
                for v in vals
            )
            if not has_meaningful_data:
                continue

            # Column layout:
            # Column A (0): No.
            # Column B (1): Company / Client Name
            # Column C (2): Address 1
            # Column D (3): Address 2
            # Column E (4): Address 3
            # Column F (5): PIC
            # Column G (6): PIC Phone
            # Column H (7): Customer Type (on-call / survey / temp / contract)
            # Column I (8): Lat (optional)
            # Column J (9): Long (optional)

            if len(vals) > 1 and str(vals[0]).strip().replace('.', '').isdigit():
                company_name = str(vals[1]).strip() if pd.notna(vals[1]) else ''
                c_idx, d_idx, e_idx, f_idx, g_idx, h_idx, i_idx, j_idx = 2, 3, 4, 5, 6, 7, 8, 9
            else:
                if len(vals) > 1 and pd.notna(vals[1]) and str(vals[0]).strip().lower() not in CLIENT_NAME_HEADERS:
                    company_name = str(vals[1]).strip() if str(vals[0]).strip().replace('.', '').isdigit() else str(vals[0]).strip()
                    if company_name == str(vals[1]).strip():
                        c_idx, d_idx, e_idx, f_idx, g_idx, h_idx, i_idx, j_idx = 2, 3, 4, 5, 6, 7, 8, 9
                    else:
                        c_idx, d_idx, e_idx, f_idx, g_idx, h_idx, i_idx, j_idx = 1, 2, 3, 4, 5, 6, 7, 8
                else:
                    company_name = str(vals[1]).strip() if len(vals) > 1 and pd.notna(vals[1]) else (str(vals[0]).strip() if pd.notna(vals[0]) else '')
                    c_idx, d_idx, e_idx, f_idx, g_idx, h_idx, i_idx, j_idx = 2, 3, 4, 5, 6, 7, 8, 9

            # Skip a header or an empty company name silently (not counted in skipped_count)
            if not company_name or company_name.lower() in ('nan', 'none', '', *CLIENT_NAME_HEADERS):
                continue

            # Address 1, 2, 3 (columns C, D, E) are joined
            addr_parts = []
            for col_i in [c_idx, d_idx, e_idx]:
                if len(vals) > col_i and pd.notna(vals[col_i]):
                    p_str = str(vals[col_i]).strip()
                    if p_str and p_str.lower() not in ['nan', 'none', '-']:
                        addr_parts.append(p_str)
            address = ", ".join(addr_parts)

            # PIC (column F) & PIC Phone (column G)
            pic_name = str(vals[f_idx]).strip() if len(vals) > f_idx and pd.notna(vals[f_idx]) and str(vals[f_idx]).lower() not in ['nan', 'none'] else ''
            pic_phone = normalize_phone(vals[g_idx]) if len(vals) > g_idx else ''

            # Customer Type (column H)
            raw_type = str(vals[h_idx]).strip() if len(vals) > h_idx and pd.notna(vals[h_idx]) else ''
            type_lower = raw_type.lower()
            if type_lower == XPRJ_CLIENT_TYPE.lower():
                client_type = XPRJ_CLIENT_TYPE
            elif 'call' in type_lower or 'service' in type_lower:
                client_type = 'On-Call'
            elif 'survey' in type_lower:
                client_type = 'Survey'
            elif 'temp' in type_lower:
                client_type = 'Temp'
            elif 'maintenance' in type_lower or 'contract' in type_lower or 'mc' in type_lower:
                client_type = 'Contract'
            elif raw_type and type_lower not in ['nan', 'none', '']:
                client_type = raw_type.title()
            else:
                client_type = None

            # Latitude & Longitude (columns I & J — optional; empty becomes None)
            lat = None
            lng = None
            if len(vals) > i_idx and pd.notna(vals[i_idx]):
                try:
                    lat_str = str(vals[i_idx]).strip()
                    if lat_str.lower() not in ['nan', 'none', '']:
                        lat = float(lat_str)
                except (ValueError, TypeError):
                    lat = None

            if len(vals) > j_idx and pd.notna(vals[j_idx]):
                try:
                    lng_str = str(vals[j_idx]).strip()
                    if lng_str.lower() not in ['nan', 'none', '']:
                        lng = float(lng_str)
                except (ValueError, TypeError):
                    lng = None

            # Uniqueness check: the combination (Company Name + Customer Type + Contact Person / PIC)
            clean_company = company_name.strip()
            clean_type = (client_type or '').strip()
            clean_pic = (pic_name or '').strip()
            dedup_key = (clean_company.lower(), clean_type.lower(), clean_pic.lower())

            # Duplicate inside the same Excel file
            if dedup_key in seen_in_batch:
                skipped_count += 1
                continue

            # Already in the database
            existing_query = ClientSite.query.filter(
                db.func.lower(ClientSite.company_name) == clean_company.lower()
            )
            if clean_type:
                existing_query = existing_query.filter(
                    db.func.lower(db.func.coalesce(ClientSite.client_type, '')) == clean_type.lower()
                )
            else:
                existing_query = existing_query.filter(
                    db.or_(ClientSite.client_type == None, ClientSite.client_type == '')
                )

            if clean_pic:
                existing_query = existing_query.filter(
                    db.func.lower(db.func.coalesce(ClientSite.pic_name, '')) == clean_pic.lower()
                )
            else:
                existing_query = existing_query.filter(
                    db.or_(ClientSite.pic_name == None, ClientSite.pic_name == '')
                )

            if existing_query.first():
                skipped_count += 1
                continue

            seen_in_batch.add(dedup_key)

            client = ClientSite(
                company_name=clean_company,
                address=address,
                client_type=client_type,
                pic_name=pic_name,
                pic_phone=pic_phone,
                latitude=lat,
                longitude=lng
            )
            db.session.add(client)
            imported_count += 1

        if imported_count > 0:
            db.session.commit()
            msg = f'Imported {imported_count} clients from the Excel file.'
            if skipped_count > 0:
                msg += f' ({skipped_count} duplicates).'
            flash(msg, 'success')
        elif skipped_count > 0:
            flash(f'No new data was imported. {skipped_count} duplicates.', 'warning')
        else:
            flash('No valid client data to import. Make sure column B holds the Client Name.', 'warning')

    except Exception as e:
        db.session.rollback()
        flash(f'Could not read or process the Excel file: {str(e)}', 'danger')

    return redirect(url_for('system.clients'))


@system_bp.route('/clients/template')
@menu_required(MENU_STORAGE)
def download_client_template():
    # Made-up example rows; the import reads the columns by position.
    headers = export_service.CLIENT_IMPORT_HEADERS
    template_data = [
        dict(zip(headers, (1, 'Example Client Ltd', 'Example Tower, 12th Floor', '1 Example Street',
                           'Example City', 'Alex Example', '081200000001', 'Contract', -6.2000, 106.8000))),
        dict(zip(headers, (2, 'Sample Store Co', 'Unit 3', '20 Sample Road',
                           'Sample City', 'Sam Sample', '081200000002', 'On-Call', -6.2100, 106.8200))),
    ]

    df = pd.DataFrame(template_data)
    output = BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, sheet_name='Client Data', index=False)

    output.seek(0)
    return send_file(
        output,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        as_attachment=True,
        download_name='Client_Import_Template.xlsx'
    )


@system_bp.route('/clients/export')
@menu_required(MENU_STORAGE)
def export_clients():
    """Every client, in the import template's columns so the file can be imported again."""
    clients_list = ClientSite.query.order_by(ClientSite.company_name, ClientSite.id).all()
    document_counts = dict(db.session.query(Document.client_id, db.func.count(Document.id))
                           .group_by(Document.client_id))
    return send_file(
        export_service.clients_workbook(clients_list, document_counts),
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        as_attachment=True,
        download_name=f"Client_Data_{wib_now().strftime('%Y%m%d_%H%M')}.xlsx",
    )


# ==========================================
# 4. ACCESS MANAGEMENT: ACCOUNTS AND THEIR MENUS
# ==========================================
ACCOUNT_TYPES = (ROLE_OFFICE, ROLE_TECHNICIAN)
# The form's three choices.  Superadmin and Admin are both office accounts: a
# Superadmin simply has every menu, an Admin the menus ticked for it.
# The values match User.access_label lowered, which the edit form uses to preselect a level.
LEVEL_SUPERADMIN, LEVEL_ADMIN, LEVEL_TECHNICIAN = 'superadmin', 'admin', 'technician'
YES_CELLS = ('y', 'yes', '1', 'true', 'v', 'x')
NO_ACCESS_MANAGER = ('Change cancelled: there must always be at least one active account '
                     'with Access Management.')


def _menus_for_level(level):
    """Menus a create/edit form grants: every menu for Superadmin, the ticked ones for Admin."""
    if level == LEVEL_SUPERADMIN:
        return list(ALL_MENUS)
    if level == LEVEL_ADMIN:
        return [menu for menu in request.form.getlist('menus') if menu in ALL_MENUS]
    return []


def _access_managers(excluding_id=None):
    """Active office accounts that can open Access Management."""
    office = User.query.join(Role).filter(Role.code == ROLE_OFFICE, User.is_active.is_(True)).all()
    return [user for user in office if user.id != excluding_id and user.can(MENU_ACCESS)]


def _leaves_no_access_manager(user, keeps_access):
    """True when this change would leave nobody able to manage accounts."""
    return (user.is_active and user.can(MENU_ACCESS) and not keeps_access
            and not _access_managers(excluding_id=user.id))


@system_bp.route('/accounts')
@menu_required(MENU_ACCESS)
def accounts():
    page = request.args.get('page', 1, type=int)
    sort = request.args.get('sort', 'desc').lower()
    sort = sort if sort in ('asc', 'desc') else 'desc'

    created_at_order = User.created_at.asc() if sort == 'asc' else User.created_at.desc()
    users_page = (
        User.query.join(Role, User.role_id == Role.id)
        .order_by(created_at_order, User.id.asc())
        .paginate(page=page, per_page=10, error_out=False)
    )

    return render_template(
        'system/accounts.html',
        users=users_page.items,
        pagination=users_page,
        sort=sort,
        total_users=User.query.count(),
        active_users=User.query.filter_by(is_active=True).count(),
        account_types=ACCOUNT_TYPES,
        menu_descriptions=MENU_DESCRIPTIONS,
        cross_actions=_access_summary_rules(),
    )


def _access_summary_rules():
    """Lines of the live access summary in the create/edit form.

    Each line appears once the account has one of `shown_with`, ticked when it has all of `menus`.
    """
    rules = [{'menus': list(menus), 'shown_with': list(menus), 'label': label}
             for menus, label in CROSS_MENU_ACTIONS.values()]
    # Data Storage alone only reaches the archive (User.can_open_document).
    rules.append({'menus': [MENU_DOCUMENTS], 'shown_with': [MENU_STORAGE],
                  'label': 'Open documents that are still active'})
    return rules


@system_bp.route('/accounts/create', methods=['POST'])
@menu_required(MENU_ACCESS)
def create_account():
    name = request.form.get('name', '').strip()
    email = request.form.get('email', '').strip().lower()
    password = request.form.get('password', '').strip()
    level = request.form.get('access_level', LEVEL_TECHNICIAN)
    account_type = ROLE_TECHNICIAN if level == LEVEL_TECHNICIAN else ROLE_OFFICE
    menus = _menus_for_level(level)

    if not name or not email or not password:
        flash('Name, Email, and Password are required.', 'danger')
        return redirect(url_for('system.accounts'))

    if len(password) < MIN_PASSWORD_LENGTH:
        flash(f'Password must be at least {MIN_PASSWORD_LENGTH} characters.', 'danger')
        return redirect(url_for('system.accounts'))

    if User.query.filter(db.func.lower(User.email) == email).first():
        flash('The email is already used by another account.', 'danger')
        return redirect(url_for('system.accounts'))

    role_record = Role.query.filter_by(code=account_type).first()
    if level not in (LEVEL_SUPERADMIN, LEVEL_ADMIN, LEVEL_TECHNICIAN) or not role_record:
        flash('Invalid account type.', 'danger')
        return redirect(url_for('system.accounts'))
    if account_type == ROLE_OFFICE and not menus:
        flash('Choose at least one menu for an Admin account.', 'danger')
        return redirect(url_for('system.accounts'))

    user = User(
        name=name,
        email=email,
        password=generate_password_hash(password),
        role_id=role_record.id,
        is_active=True,
        menus=menus,
    )
    db.session.add(user)
    db.session.commit()
    flash(f'Account "{name}" ({user.access_label}) created.', 'success')
    return redirect(url_for('system.accounts'))


@system_bp.route('/accounts/edit/<int:user_id>', methods=['POST'])
@menu_required(MENU_ACCESS)
def edit_account(user_id):
    """Fix a name or email, and for an office account its level and menus.

    The account type never changes: Superadmin and Admin can switch, a technician stays one.
    """
    user = db.get_or_404(User, user_id)
    name = request.form.get('name', '').strip()
    email = request.form.get('email', '').strip().lower()

    if not name or not email:
        flash('Name and Email are required.', 'danger')
        return redirect(url_for('system.accounts'))

    others = User.query.filter(User.id != user.id)
    if others.filter(db.func.lower(User.email) == email).first():
        flash('The email is already used by another account.', 'danger')
        return redirect(url_for('system.accounts'))
    # The name doubles as the login username, so it must stay unambiguous.
    if name.lower() != user.name.lower() and others.filter(db.func.lower(User.name) == name.lower()).first():
        flash('The name is already used by another account. The name is also used to log in, so it must be unique.', 'danger')
        return redirect(url_for('system.accounts'))

    if user.is_office:
        menus = _menus_for_level(request.form.get('access_level', LEVEL_ADMIN))
        if not menus:
            flash('An Admin account must have at least one menu.', 'danger')
            return redirect(url_for('system.accounts'))
        if _leaves_no_access_manager(user, keeps_access=MENU_ACCESS in menus):
            flash(NO_ACCESS_MANAGER, 'danger')
            return redirect(url_for('system.accounts'))
        user.menus = menus

    user.name = name
    user.email = email
    db.session.commit()
    flash(f'Account "{name}" updated.', 'success')
    return redirect(url_for('system.accounts'))


@system_bp.route('/accounts/toggle/<int:user_id>', methods=['POST'])
@menu_required(MENU_ACCESS)
def toggle_account_status(user_id):
    user = db.get_or_404(User, user_id)
    if user.id == session.get('user_id'):
        flash('You cannot deactivate your own account.', 'danger')
        return redirect(url_for('system.accounts'))
    if user.is_active and _leaves_no_access_manager(user, keeps_access=False):
        flash(NO_ACCESS_MANAGER, 'danger')
        return redirect(url_for('system.accounts'))

    user.is_active = not user.is_active
    db.session.commit()
    status_str = 'activated' if user.is_active else 'deactivated'
    flash(f'Account "{user.name}" {status_str}.', 'success')
    return redirect(url_for('system.accounts'))


def _cell(vals, index):
    return str(vals[index]).strip() if len(vals) > index and pd.notna(vals[index]) else ''


@system_bp.route('/accounts/import', methods=['POST'])
@menu_required(MENU_ACCESS)
def import_accounts():
    """Columns A-D: Name, Email, Password, Account Type; then one Y/N column per menu, in MENUS order.

    Account Type is Superadmin (every menu, the Y/N columns are ignored), Admin (the
    menus marked Y) or Technician.
    """
    if 'file' not in request.files:
        flash('No file selected.', 'danger')
        return redirect(url_for('system.accounts'))

    file = request.files['file']
    if not file or file.filename == '':
        flash('No file selected.', 'danger')
        return redirect(url_for('system.accounts'))

    if not (file.filename.lower().endswith('.xlsx') or file.filename.lower().endswith('.xls')):
        flash('Unsupported file format. Please upload an Excel file (.xlsx).', 'danger')
        return redirect(url_for('system.accounts'))

    try:
        # Read every cell as text: a numeric password like 123456 must not become 123456.0.
        df = pd.read_excel(file, dtype=str)
        if df.empty:
            flash('The Excel file is empty or has no data.', 'warning')
            return redirect(url_for('system.accounts'))

        roles = {role.code: role for role in Role.query.filter(Role.code.in_(ACCOUNT_TYPES))}
        imported_count = 0
        skipped_count = 0
        no_menu_count = 0
        seen_in_batch = set()

        for idx, row in df.iterrows():
            vals = row.values
            if len(vals) == 0:
                continue

            # Check whether every cell in the row is empty / blank / nan (empty rows are ignored, not counted as skipped)
            has_meaningful_data = any(
                pd.notna(v) and str(v).strip() and str(v).strip().lower() not in ['nan', 'none', 'null', '-']
                for v in vals
            )
            if not has_meaningful_data:
                continue

            name, email, password, raw_type = (_cell(vals, index) for index in range(4))

            # Skip header or empty rows (not counted in skipped_count)
            if not name or not email or not password or name.lower() in ['nan', 'none', 'name', 'full name', 'user name', '']:
                continue

            email_clean = email.lower()
            if email_clean in seen_in_batch or User.query.filter(db.func.lower(User.email) == email_clean).first():
                skipped_count += 1
                continue

            type_lower = raw_type.lower()
            is_superadmin = 'superadmin' in type_lower.replace(' ', '')
            is_office = is_superadmin or any(word in type_lower for word in ('admin', 'staff', 'office'))
            account_type = ROLE_OFFICE if is_office else ROLE_TECHNICIAN
            menus = []
            if is_superadmin:
                menus = list(ALL_MENUS)
            elif is_office:
                menus = [menu for offset, menu in enumerate(ALL_MENUS)
                         if _cell(vals, 4 + offset).lower() in YES_CELLS]
                if not menus:
                    no_menu_count += 1
                    continue

            seen_in_batch.add(email_clean)
            db.session.add(User(
                name=name,
                email=email_clean,
                password=generate_password_hash(password),
                role_id=roles[account_type].id,
                is_active=True,
                menus=menus,
            ))
            imported_count += 1

        notes = []
        if skipped_count:
            notes.append(f'{skipped_count} account(s) skipped because the email is already registered')
        if no_menu_count:
            notes.append(f'{no_menu_count} Admin account(s) skipped because no menu is marked Y')
        if imported_count > 0:
            db.session.commit()
            msg = f'Imported {imported_count} user account(s).'
            if notes:
                msg += f" ({'; '.join(notes)})."
            flash(msg, 'success')
        elif notes:
            flash(f"No new accounts were imported: {'; '.join(notes)}.", 'warning')
        else:
            flash('No valid account data was found in the Excel file.', 'warning')

    except Exception as e:
        db.session.rollback()
        flash(f'Could not read or process the Excel file: {str(e)}', 'danger')

    return redirect(url_for('system.accounts'))


@system_bp.route('/accounts/template')
@menu_required(MENU_ACCESS)
def download_account_template():
    def example(name, email, account_type, menus):
        record = {'Name': name, 'Email': email, 'Password': 'password123', 'Account Type': account_type}
        record.update({label: ('Y' if key in menus else 'N') for key, label in MENUS.items()})
        return record

    template_data = [
        example('Alex Example', 'alex.technician@example.com', ROLE_TECHNICIAN, ()),
        example('Blake Example', 'blake.admin@example.com', 'Admin',
                (MENU_DOCUMENTS, MENU_STORAGE, MENU_SCHEDULING)),
        example('Casey Example', 'casey.manager@example.com', 'Superadmin', ALL_MENUS),
    ]

    df = pd.DataFrame(template_data)
    output = BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, sheet_name='Account Data', index=False)

    output.seek(0)
    return send_file(
        output,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        as_attachment=True,
        download_name='Account_Import_Template.xlsx'
    )
