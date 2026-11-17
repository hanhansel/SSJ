from io import BytesIO
from flask import Blueprint, render_template, request, jsonify, send_file
from datetime import date, timedelta, datetime
import holidays
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from apps.models import db, Role, ScheduleBlock, User, SystemHoliday, DocumentSVC, DocumentXPRJ, DocumentPRJ, wib_now, STATUS_HOLD, UNSCHEDULABLE_STATUSES, ROLE_TECHNICIAN, MENU_SCHEDULING
from apps.core.registry import fetch_doc, get_doc_label, get_doc_color
from apps.core.dates import DAY_NAMES, DAY_ABBREVIATIONS, format_date
from apps.scheduling.engine.pipeline import run_engine
from apps.scheduling.engine_input import MAX_BLOCK_DAYS, build_engine_input, document_urgency, priority_rank
from apps.core.auth import menu_required

scheduling_bp = Blueprint('scheduling', __name__, url_prefix='/scheduling')


def _json_error(message, status_code=400):
    """Return API errors in the same shape used by the scheduler client."""
    return jsonify({'status': 'error', 'message': message}), status_code


def _parse_positive_int(value, field_name, maximum=None):
    try:
        value = int(value)
    except (TypeError, ValueError):
        raise ValueError(f'{field_name} must be a number.')
    if value < 1:
        raise ValueError(f'{field_name} must be at least 1.')
    if maximum is not None and value > maximum:
        raise ValueError(f'{field_name} can be at most {maximum} days.')
    return value


PROJECT_ON_HOLD_MESSAGE = 'The Master Project of PRJ {no} is on Hold; this PRJ cannot be scheduled.'


def _unschedulable_reason(doc):
    """Why a document cannot get schedule blocks, or None when it can."""
    if doc.status in UNSCHEDULABLE_STATUSES:
        return f'Document {doc.display_no} is {doc.status} and cannot be scheduled.'
    if doc.is_on_hold:
        return PROJECT_ON_HOLD_MESSAGE.format(no=doc.display_no)
    return None


def _get_active_technician(user_id):
    """Scheduler records may only belong to active Technician accounts."""
    try:
        technician = db.session.get(User, int(user_id))
    except (TypeError, ValueError):
        return None
    if not technician or not technician.is_active or technician.role != ROLE_TECHNICIAN:
        return None
    return technician




def _build_dashboard_state(start_date):
    holiday_map = {h.date.strftime('%Y-%m-%d'): h.description for h in SystemHoliday.query.all()}

    # Only Online documents can be scheduled. Approval and closed documents are excluded,
    # and so are PRJs whose Master Project is on Hold.
    svc_backlog = DocumentSVC.query.filter_by(status='Online').all()
    xprj_backlog = DocumentXPRJ.query.filter_by(status='Online').all()
    prj_backlog = [prj for prj in DocumentPRJ.query.filter_by(status='Online').all() if not prj.is_on_hold]

    # Usage Map — total assigned days per backlog document over its whole
    # schedule, independent of which calendar window is being viewed.
    backlog_ids = [d.id for d in svc_backlog + xprj_backlog + prj_backlog]
    usage_map = {}
    if backlog_ids:
        usage_rows = (
            db.session.query(ScheduleBlock.document_id, db.func.sum(ScheduleBlock.days_duration))
            .filter(ScheduleBlock.document_id.in_(backlog_ids))
            .group_by(ScheduleBlock.document_id)
            .all()
        )
        usage_map = {doc_id: int(total or 0) for doc_id, total in usage_rows}

    today = wib_now().date()
    backlog = []

    for item in svc_backlog:
        age_days = (today - item.date).days if item.date else 0
        assigned_d = usage_map.get(item.id, 0)
        backlog.append({
            'id': item.id,
            'doc_type': 'svc',
            'doc_no': item.display_no,
            'client_name': item.client.company_name if item.client else '',
            'service_type': 'SVC',
            # SVC urgency is derived from the client: Contract = urgent.
            'urgency': document_urgency(item),
            'estimated_days': item.estimated_calendar_days,
            'assigned_days': assigned_d,
            'status': item.status,
            'age_days': age_days,
            'color': get_doc_color('svc'),
            'label': 'SVC',
            'assigned_techs': item.assigned_technicians
        })

    for item in xprj_backlog:
        age_days = (today - item.date).days if item.date else 0
        assigned_d = usage_map.get(item.id, 0)
        backlog.append({
            'id': item.id,
            'doc_type': 'xprj',
            'doc_no': item.display_no,
            'client_name': item.client.company_name if item.client else '',
            'service_type': 'XPRJ',
            'urgency': document_urgency(item),
            'estimated_days': item.estimated_calendar_days,
            'assigned_days': assigned_d,
            'status': item.status,
            'age_days': age_days,
            'color': get_doc_color('xprj'),
            'label': 'XPRJ',
            'assigned_techs': item.assigned_technicians
        })

    for item in prj_backlog:
        age_days = (today - item.date).days if item.date else 0
        assigned_d = usage_map.get(item.id, 0)
        backlog.append({
            'id': item.id,
            'doc_type': 'prj',
            'doc_no': item.display_no,
            'client_name': item.client.company_name if item.client else '',
            'project_name': item.master_project.name if item.master_project else '',
            'prj_title': item.title,
            'service_type': 'PRJ',
            'urgency': document_urgency(item),
            'estimated_days': item.estimated_calendar_days,
            'assigned_days': assigned_d,
            'status': item.status,
            'age_days': age_days,
            'color': get_doc_color('prj'),
            'label': 'PRJ',
            'assigned_techs': item.assigned_technicians
        })

    # Business priority: XPRJ first, then urgent work (SVC for Contract
    # clients, PRJ marked Urgent), then normal work.  The engine uses the same order.
    backlog.sort(key=lambda item: (priority_rank(item['doc_type'], item['urgency']), -item['age_days'], item['id']))

    # Scheduled Blocks in Window (14 days)
    end_date = start_date + timedelta(days=13)
    blocks = ScheduleBlock.query.filter(
        ScheduleBlock.start_date <= end_date,
        ScheduleBlock.start_date >= start_date - timedelta(days=MAX_BLOCK_DAYS)
    ).all()

    schedules_data = []
    for b in blocks:
        block_end = b.start_date + timedelta(days=b.days_duration - 1)
        if block_end < start_date:
            continue

        if b.doc_type == 'leave':
            schedules_data.append({
                'id': b.id,
                'user_id': b.user_id,
                'doc_type': 'leave',
                'project_id': 0,
                'start_date': b.start_date.strftime('%Y-%m-%d'),
                'days_duration': b.days_duration,
                'doc_no': b.leave_reason or 'Leave',
                'client_name': 'Leave / Absence',
                'color': '#ef4444',
                'status': b.status
            })
        else:
            doc = b.document
            if doc:
                schedules_data.append({
                    'id': b.id,
                    'user_id': b.user_id,
                    'doc_type': b.doc_type,
                    'project_id': b.doc_id,
                    'start_date': b.start_date.strftime('%Y-%m-%d'),
                    'days_duration': b.days_duration,
                    'doc_no': doc.display_no,
                    'client_name': doc.client.company_name if doc.client else '',
                    'estimated_days': doc.estimated_calendar_days,
                    'project_name': doc.master_project.name if doc.document_type == 'prj' and doc.master_project else '',
                    'prj_title': doc.title if doc.document_type == 'prj' else '',
                    'color': get_doc_color(b.doc_type),
                    'status': doc.status,
                    'is_captain': doc.captain_id == b.user_id,
                })

    return backlog, schedules_data, holiday_map


@scheduling_bp.route('/')
@menu_required(MENU_SCHEDULING)
def index():
    date_str = request.args.get('start_date')
    if date_str:
        try:
            start_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        except Exception:
            start_date = wib_now().date()
    else:
        start_date = wib_now().date()

    backlog, schedules_data, holiday_map = _build_dashboard_state(start_date)
    technicians = User.query.join(Role).filter(Role.code == ROLE_TECHNICIAN, User.is_active.is_(True)).all()

    dates = []
    today_str = wib_now().strftime('%Y-%m-%d')

    for i in range(14):
        current = start_date + timedelta(days=i)
        d_str = current.strftime('%Y-%m-%d')
        dates.append({
            'date_obj': current,
            'day_name': DAY_NAMES[current.weekday()],
            'formatted': format_date(current),
            'value': d_str,
            'is_today': (d_str == today_str),
            'is_holiday': d_str in holiday_map,
            'holiday_name': holiday_map.get(d_str, '')
        })

    prev_date = (start_date - timedelta(days=14)).strftime('%Y-%m-%d')
    next_date = (start_date + timedelta(days=14)).strftime('%Y-%m-%d')

    return render_template('scheduling/index.html',
        start_date=start_date.strftime('%Y-%m-%d'),
        prev_date=prev_date,
        next_date=next_date,
        current_year=start_date.year,
        dates=dates,
        technicians=technicians,
        backlog=backlog,
        backlog_json=backlog,
        schedules=schedules_data,
        holidays=holiday_map
    )


@scheduling_bp.route('/api/dashboard_state')
@menu_required(MENU_SCHEDULING)
def api_dashboard_state():
    """Return the data needed to re-render the current scheduler window."""
    date_str = request.args.get('start_date')
    if date_str:
        try:
            start_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        except ValueError:
            return _json_error('Invalid date format.')
    else:
        start_date = wib_now().date()

    backlog, schedules, holiday_map = _build_dashboard_state(start_date)
    return jsonify({
        'status': 'success',
        'backlog': backlog,
        'schedules': schedules,
        'holidays': holiday_map,
    })


# ==========================================
# SCHEDULE CRUD API ENDPOINTS
# ==========================================
@scheduling_bp.route('/api/assign_task', methods=['POST'])
@scheduling_bp.route('/api/assign', methods=['POST'])
@menu_required(MENU_SCHEDULING)
def api_assign():
    data = request.get_json() or request.form
    user_id = data.get('user_id')
    doc_type = data.get('doc_type', 'svc').lower()
    # doc_id and days_duration are the canonical scheduler API fields.
    # The alternatives keep older callers working during the migration.
    doc_id = data.get('doc_id') or data.get('project_id')
    start_date_str = data.get('start_date')

    if not user_id or not doc_id or not start_date_str:
        return _json_error('The assignment data is incomplete.')

    try:
        days_duration = _parse_positive_int(
            data.get('days_duration') or data.get('duration') or 1,
            'Duration', MAX_BLOCK_DAYS
        )
    except ValueError as exc:
        return _json_error(str(exc))

    try:
        start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
    except Exception:
        return _json_error('Invalid date format.')

    doc = fetch_doc(doc_type, doc_id)
    if not doc:
        return _json_error('Document not found.', 404)
    reason = _unschedulable_reason(doc)
    if reason:
        return _json_error(reason, 409)

    if not _get_active_technician(user_id):
        return _json_error('Work can only be assigned to active Technician accounts.', 422)

    block = ScheduleBlock(
        user_id=user_id,
        document_id=doc.id,
        start_date=start_date,
        days_duration=days_duration,
        status='Scheduled'
    )
    db.session.add(block)

    db.session.commit()
    return jsonify({'status': 'success', 'message': f'Document {doc.display_no} has been scheduled.', 'id': block.id})


@scheduling_bp.route('/api/remove_task', methods=['POST'])
@scheduling_bp.route('/api/unassign_task', methods=['POST'])  # legacy alias
@scheduling_bp.route('/api/unassign', methods=['POST'])  # legacy alias
@menu_required(MENU_SCHEDULING)
def api_unassign():
    data = request.get_json() or request.form
    block_id = data.get('block_id')

    if not block_id:
        return _json_error('Block ID is missing.')

    block = db.session.get(ScheduleBlock, block_id)
    if not block:
        return _json_error('Schedule not found.', 404)

    doc = block.document
    db.session.delete(block)
    if doc and doc.captain_id == block.user_id:
        still_assigned = ScheduleBlock.query.filter(
            ScheduleBlock.document_id == doc.id,
            ScheduleBlock.user_id == block.user_id,
            ScheduleBlock.id != block.id,
        ).first()
        if not still_assigned:
            doc.captain_id = None
    db.session.commit()
    return jsonify({'status': 'success', 'message': 'The assignment has been removed.'})


@scheduling_bp.route('/api/update_task', methods=['POST'])
@scheduling_bp.route('/api/edit_schedule', methods=['POST'])  # legacy alias
@menu_required(MENU_SCHEDULING)
def api_edit_schedule():
    data = request.get_json() or request.form
    block_id = data.get('block_id')
    try:
        duration = _parse_positive_int(
            data.get('days_duration') or data.get('duration') or 1,
            'Duration', MAX_BLOCK_DAYS
        )
    except ValueError as exc:
        return _json_error(str(exc))
    start_date_str = data.get('start_date')

    block = db.session.get(ScheduleBlock, block_id) if block_id else None
    if not block:
        return _json_error('Schedule block not found.', 404)
    if block.document and block.document.is_on_hold:
        if block.document.status == STATUS_HOLD:
            return _json_error('A document on Hold cannot be rescheduled.', 409)
        return _json_error(PROJECT_ON_HOLD_MESSAGE.format(no=block.document.display_no), 409)

    if start_date_str:
        try:
            block.start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
        except ValueError:
            return _json_error('Invalid date format.')
    block.days_duration = duration

    # Only the Edit Schedule modal sends is_captain; drag-resize leaves it untouched.
    if 'is_captain' in data and block.document:
        if data.get('is_captain') in (True, 'true', '1', 1):
            block.document.captain_id = block.user_id
        elif block.document.captain_id == block.user_id:
            block.document.captain_id = None

    db.session.commit()
    return jsonify({'status': 'success', 'message': 'The schedule has been updated.'})


@scheduling_bp.route('/api/save_absence', methods=['POST'])
@menu_required(MENU_SCHEDULING)
def api_save_absence():
    data = request.get_json() or request.form
    user_id = data.get('user_id')
    start_date_str = data.get('start_date')
    try:
        duration = _parse_positive_int(
            data.get('days_duration') or data.get('duration') or 1,
            'Duration', MAX_BLOCK_DAYS
        )
    except ValueError as exc:
        return _json_error(str(exc))
    reason = (data.get('reason') or '').strip() or 'Leave / Absence'
    absence_type = data.get('type', 'leave')

    if not start_date_str:
        return _json_error('The date is required.')

    try:
        start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
    except Exception:
        return _json_error('Invalid date format.')

    if absence_type == 'holiday':
        # Add system holiday
        if SystemHoliday.query.filter_by(date=start_date).first():
            return _json_error('A holiday already exists on that date.', 409)
        holiday = SystemHoliday(date=start_date, description=reason)
        db.session.add(holiday)
        db.session.commit()
        return jsonify({'status': 'success', 'message': 'The public holiday has been saved.'})
    else:
        if not user_id:
            return _json_error('Choose a technician for the leave.')
        if not _get_active_technician(user_id):
            return _json_error('Leave can only be recorded for active Technician accounts.', 422)
        block = ScheduleBlock(
            user_id=user_id,
            document_id=None,
            start_date=start_date,
            days_duration=duration,
            leave_reason=reason,
            status='Scheduled'
        )
        db.session.add(block)
        db.session.commit()
        return jsonify({'status': 'success', 'message': 'The technician leave has been saved.'})


@scheduling_bp.route('/api/remove_holiday', methods=['POST'])
@menu_required(MENU_SCHEDULING)
def api_remove_holiday():
    data = request.get_json() or request.form
    try:
        holiday_date = datetime.strptime(data.get('start_date', ''), '%Y-%m-%d').date()
    except ValueError:
        return _json_error('Invalid date format.')

    holiday = SystemHoliday.query.filter_by(date=holiday_date).first()
    if not holiday:
        return _json_error('Holiday not found.', 404)
    db.session.delete(holiday)
    db.session.commit()
    return jsonify({'status': 'success', 'message': 'The public holiday has been removed.'})


@scheduling_bp.route('/api/update_doc_estimation', methods=['POST'])
@menu_required(MENU_SCHEDULING)
def api_update_doc_estimation():
    data = request.get_json() or request.form
    doc_type = (data.get('doc_type') or '').lower()
    doc_id = data.get('doc_id') or data.get('project_id')
    if not doc_id:
        return _json_error('A document must be selected.')
    try:
        estimated_days = _parse_positive_int(data.get('estimated_days'), 'Estimated days')
    except ValueError as exc:
        return _json_error(str(exc))

    doc = fetch_doc(doc_type, doc_id)
    if not doc:
        return _json_error('Document not found.', 404)
    doc.estimated_calendar_days = estimated_days
    db.session.commit()
    return jsonify({'status': 'success', 'message': 'The document estimate has been updated.'})


@scheduling_bp.route('/api/generate_holidays_year', methods=['POST'])
@menu_required(MENU_SCHEDULING)
def api_generate_holidays_year():
    data = request.get_json() or request.form
    try:
        year = _parse_positive_int(data.get('year'), 'Year')
    except ValueError as exc:
        return _json_error(str(exc))
    if year < 1900 or year > 2100:
        return _json_error('The year must be between 1900 and 2100.')

    indonesia_holidays = holidays.country_holidays('ID', years=[year], language='en_US')
    existing_dates = {h.date for h in SystemHoliday.query.filter(
        SystemHoliday.date >= date(year, 1, 1),
        SystemHoliday.date <= date(year, 12, 31),
    ).all()}
    added = 0
    skipped = 0
    for holiday_date, description in indonesia_holidays.items():
        if holiday_date in existing_dates:
            skipped += 1
            continue
        db.session.add(SystemHoliday(date=holiday_date, description=description))
        added += 1
    db.session.commit()
    return jsonify({'status': 'success', 'added': added, 'skipped': skipped})


@scheduling_bp.route('/api/suggest', methods=['POST'])
@menu_required(MENU_SCHEDULING)
def api_suggest():
    """Build engine input from selected backlog documents and return suggestions."""
    data = request.get_json() or request.form
    try:
        start_date = datetime.strptime(data.get('start_date', ''), '%Y-%m-%d').date()
    except ValueError:
        return _json_error('Invalid start date format.')

    selected_documents = data.get('documents') or []
    if not isinstance(selected_documents, list) or not selected_documents:
        return _json_error('Choose at least one document.')

    docs = []
    for selected in selected_documents:
        if not isinstance(selected, dict):
            return _json_error('Invalid document format.')
        doc_type = (selected.get('doc_type') or '').lower()
        doc = fetch_doc(doc_type, selected.get('id'))
        if not doc:
            return _json_error('One of the documents was not found.', 404)
        reason = _unschedulable_reason(doc)
        if reason:
            return _json_error(reason, 409)
        docs.append(doc)

    engine_input = build_engine_input(start_date, docs)
    if not engine_input['technicians']:
        return _json_error('There are no active technicians to schedule.', 409)

    result = run_engine(**engine_input)
    return jsonify({
        'status': 'success',
        'results': result['scheduled'],
        'unscheduled': result['unscheduled'],
        'meta': {
            'engine_used': result['engine_used'],
            'runtime_ms': result['runtime_ms'],
            'search_space_estimate': result['search_space_estimate'],
        },
    })


@scheduling_bp.route('/api/export_schedule_xlsx')
@menu_required(MENU_SCHEDULING)
def api_export_schedule_xlsx():
    date_str = request.args.get('start_date')
    try:
        start_date = datetime.strptime(date_str, '%Y-%m-%d').date() if date_str else wib_now().date()
    except ValueError:
        start_date = wib_now().date()
    days_to_export = 31
    end_date = start_date + timedelta(days=days_to_export - 1)

    active_technicians = User.query.join(Role).filter(
        Role.code == ROLE_TECHNICIAN, User.is_active.is_(True)
    ).order_by(User.name).all()
    blocks = ScheduleBlock.query.filter(
        ScheduleBlock.start_date <= end_date,
        ScheduleBlock.start_date >= start_date - timedelta(days=MAX_BLOCK_DAYS),
    ).order_by(ScheduleBlock.user_id, ScheduleBlock.start_date, ScheduleBlock.id).all()

    # SQLite cannot reliably add a date and integer through a portable ORM
    # expression, so the final overlap test is done with native date objects.
    blocks_by_technician = {technician.id: [] for technician in active_technicians}
    for block in blocks:
        block_end = block.start_date + timedelta(days=block.days_duration - 1)
        if block_end >= start_date and block.user_id in blocks_by_technician:
            blocks_by_technician[block.user_id].append((block, block_end))

    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = 'Schedule Calendar'

    thin_gray = Side(style='thin', color='D1D5DB')
    border = Border(left=thin_gray, right=thin_gray, top=thin_gray, bottom=thin_gray)
    header_fill = PatternFill('solid', fgColor='1F2937')
    weekend_fill = PatternFill('solid', fgColor='F1F5F9')
    holiday_fill = PatternFill('solid', fgColor='FEE2E2')
    white_fill = PatternFill('solid', fgColor='FFFFFF')
    holiday_dates = {holiday.date for holiday in SystemHoliday.query.filter(
        SystemHoliday.date >= start_date, SystemHoliday.date <= end_date
    ).all()}
    day_names = DAY_ABBREVIATIONS
    palettes = {
        'svc': ['6366F1', '8B5CF6', '10B981', '06B6D4', 'F59E0B', '14B8A6'],
        'prj': ['0EA5E9', '84CC16', 'A855F7', '22D3EE', 'EAB308', 'F97316'],
    }

    worksheet.merge_cells(start_row=1, start_column=1, end_row=1, end_column=days_to_export + 2)
    title = worksheet.cell(1, 1, f'Technician Schedule Calendar — {format_date(start_date)} to {format_date(end_date)}')
    title.font = Font(bold=True, color='FFFFFF', size=14)
    title.fill = header_fill
    title.alignment = Alignment(horizontal='center', vertical='center')
    worksheet.row_dimensions[1].height = 26

    worksheet.cell(3, 1, 'TECHNICIAN')
    worksheet.cell(3, 2, 'TASK')
    for offset in range(days_to_export):
        day = start_date + timedelta(days=offset)
        cell = worksheet.cell(3, offset + 3, f'{day_names[day.weekday()]}\n{day:%d/%m}')
        cell.fill = header_fill
        cell.font = Font(bold=True, color='FFFFFF', size=9)
        cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
    for cell in worksheet[3]:
        cell.fill = header_fill
        cell.font = Font(bold=True, color='FFFFFF', size=9)
        cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
        cell.border = border
    worksheet.row_dimensions[3].height = 32
    worksheet.column_dimensions['A'].width = 23
    worksheet.column_dimensions['B'].width = 8
    for col in range(3, days_to_export + 3):
        worksheet.column_dimensions[get_column_letter(col)].width = 13
    worksheet.freeze_panes = 'C4'

    current_row = 4
    for technician in active_technicians:
        tech_blocks = blocks_by_technician[technician.id]
        lanes = []  # each lane stores the last occupied date
        assignments = []
        for block, block_end in tech_blocks:
            lane_index = next((index for index, lane_end in enumerate(lanes) if lane_end < block.start_date), None)
            if lane_index is None:
                lane_index = len(lanes)
                lanes.append(block_end)
            else:
                lanes[lane_index] = block_end
            assignments.append((block, block_end, lane_index))
        lane_count = max(1, len(lanes))
        tech_start_row = current_row

        for lane_index in range(lane_count):
            worksheet.cell(current_row, 2, f'Task {lane_index + 1}')
            for offset in range(days_to_export):
                day = start_date + timedelta(days=offset)
                cell = worksheet.cell(current_row, offset + 3)
                cell.border = border
                cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
                cell.fill = holiday_fill if day in holiday_dates else (weekend_fill if day.weekday() >= 5 else white_fill)
            worksheet.cell(current_row, 2).border = border
            worksheet.cell(current_row, 2).alignment = Alignment(horizontal='center', vertical='center')
            worksheet.row_dimensions[current_row].height = 42
            current_row += 1

        worksheet.merge_cells(start_row=tech_start_row, start_column=1, end_row=current_row - 1, end_column=1)
        tech_cell = worksheet.cell(tech_start_row, 1, technician.name)
        tech_cell.font = Font(bold=True, color='111827')
        tech_cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
        tech_cell.fill = PatternFill('solid', fgColor='F8FAFC')
        tech_cell.border = border

        for block, block_end, lane_index in assignments:
            visible_start = max(block.start_date, start_date)
            visible_end = min(block_end, end_date)
            first_col = 3 + (visible_start - start_date).days
            last_col = 3 + (visible_end - start_date).days
            row = tech_start_row + lane_index
            doc = block.document
            if not doc:
                color = 'EF4444'
                label = f'Leave / Absence\n{block.leave_reason or "No reason given"}'
            elif doc.document_type == 'svc' and doc.client and doc.client.is_contract:
                color = 'EF4444'
                label = f'URGENT SVC {doc.display_no}\n{doc.client.company_name if doc.client else ""}'
            else:
                palette_key = 'svc' if doc.document_type == 'svc' else 'prj'
                shift = 3 if doc.document_type == 'xprj' else 0
                color = palettes[palette_key][(doc.id - 1 + shift) % len(palettes[palette_key])]
                label = f'{get_doc_label(doc.document_type)} {doc.display_no}\n{doc.client.company_name if doc.client else ""}'
            if doc and doc.captain_id == block.user_id:
                label += '\nCAPTAIN'  # the whole block cell is already bold
            if visible_start > block.start_date:
                label = '← ' + label
            if visible_end < block_end:
                label += ' →'
            if first_col != last_col:
                worksheet.merge_cells(start_row=row, start_column=first_col, end_row=row, end_column=last_col)
            block_cell = worksheet.cell(row, first_col, label)
            block_cell.fill = PatternFill('solid', fgColor=color)
            block_cell.font = Font(bold=True, color='FFFFFF', size=9)
            block_cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
            block_cell.border = border

    if not active_technicians:
        worksheet.merge_cells(start_row=4, start_column=1, end_row=4, end_column=days_to_export + 2)
        empty_cell = worksheet.cell(4, 1, 'There are no active technicians to show yet.')
        empty_cell.alignment = Alignment(horizontal='center')

    legend = workbook.create_sheet('Legend')
    legend.append(['Colour', 'Meaning'])
    legend.append(['Red', 'Leave / Absence, or an urgent SVC (Contract client)'])
    legend.append(['Other colours', 'SVC / XPRJ / PRJ task blocks; each follows the document ID, as in the scheduler page'])
    legend.append(['← / →', 'The block starts before or continues after the exported range'])
    legend.append(['CAPTAIN', 'The technician who is captain (team lead) for that document'])
    legend.column_dimensions['A'].width = 24
    legend.column_dimensions['B'].width = 80
    for cell in legend[1]:
        cell.fill = header_fill
        cell.font = Font(bold=True, color='FFFFFF')

    output = BytesIO()
    workbook.save(output)
    output.seek(0)

    return send_file(
        output,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        as_attachment=True,
        download_name=f"SSJ_Schedule_Calendar_{start_date.strftime('%Y%m%d')}_{end_date.strftime('%Y%m%d')}.xlsx"
    )
