from datetime import datetime, timedelta, date
from flask import Blueprint, abort, jsonify, render_template, send_from_directory, session, redirect, url_for, request, flash, current_app
from sqlalchemy.exc import IntegrityError
from apps.models import (
    db, User, ScheduleBlock, WorkReport, WorkReportPhoto,
    HOPhoto, wib_now, STATUS_PENDING, FINAL_STATUSES,
    ROLE_TECHNICIAN,
)
from apps.core.registry import fetch_doc, get_doc_label, get_doc_color, is_allowed_upload
from apps.core.auth import login_required, technician_required
from apps.core.dates import format_month_year
from apps.core.uploads import save_document_upload, remove_upload_files

technician_bp = Blueprint('technician', __name__, url_prefix='/technician')


def _wants_json():
    return request.headers.get('X-Requested-With') == 'XMLHttpRequest'


def _doc_address(doc):
    """Work-site address of a document, falling back to the client's main address."""
    return getattr(doc, 'site_address', None) or (doc.client.address if doc.client else '') or ''


def _visible_to_technician(doc):
    """A paused document (Hold, or an PRJ of a Master Project on Hold): technicians do not
    see it anywhere, not even its past days."""
    return doc is not None and not doc.is_on_hold


# ==========================================
# HELPER — Build task list for a given date
# ==========================================
def _build_tasks_for_date(user_id, selected_date):
    """Returns deduplicated list of task dicts for a technician on selected_date."""
    # Only blocks that can cover the date: started on/before it, within the same
    # one-year look-back the month calendar uses (not the technician's whole history).
    candidate_blocks = ScheduleBlock.query.filter(
        ScheduleBlock.user_id == user_id,
        ScheduleBlock.document_id.isnot(None),
        ScheduleBlock.start_date <= selected_date,
        ScheduleBlock.start_date >= selected_date - timedelta(days=366),
    ).all()
    raw_tasks = []

    for b in candidate_blocks:
        doc = b.document
        if not _visible_to_technician(doc):
            continue
        if selected_date < b.start_date + timedelta(days=b.days_duration):
            raw_tasks.append({
                'block_id':        b.id,
                'doc_type':        b.doc_type,
                'doc_id':          doc.id,
                'doc_no':          doc.display_no,
                'client_name':     doc.client.company_name if doc.client else '',
                'client_address':  _doc_address(doc),
                'task_description': getattr(doc, 'task_description', None) or getattr(doc, 'customer_complaint', None) or getattr(doc, 'title', ''),
                'status':          doc.status,
                'color':           get_doc_color(b.doc_type),
                'label':           get_doc_label(b.doc_type),
                # PRJ stores urgency as 1/2; every task reports it as text like the other documents.
                'urgency':         'Urgent' if getattr(doc, 'urgency_level', 1) == 2 else 'Normal',
                'duration':        b.days_duration,
            })

    seen = set()
    unique_tasks = []
    for t in raw_tasks:
        key = (t['doc_type'], t['doc_id'])
        if key not in seen:
            seen.add(key)
            unique_tasks.append(t)
    return unique_tasks


def _technician_is_assigned(user_id, doc_type, doc_id):
    doc = fetch_doc((doc_type or '').lower(), doc_id)
    return bool(doc and ScheduleBlock.query.filter_by(user_id=user_id, document_id=doc.id).first())


# ==========================================
# 1. CALENDAR TAB — MONTHLY CALENDAR VIEW
# ==========================================
@technician_bp.route('/')
@technician_bp.route('/dashboard')
@technician_required
def dashboard():
    user_id = session.get('user_id')
    today = wib_now().date()

    # Selected date (defaults to today)
    selected_date_str = request.args.get('date')
    if selected_date_str:
        try:
            selected_date = datetime.strptime(selected_date_str, '%Y-%m-%d').date()
        except Exception:
            selected_date = today
    else:
        selected_date = today

    # Month navigation: exact calendar-month arithmetic (32-day jumps skipped months).
    month_offset = request.args.get('month_offset', 0, type=int) or 0
    month_index = today.year * 12 + (today.month - 1) + month_offset
    first_of_month = date(month_index // 12, month_index % 12 + 1, 1)
    next_month     = date((month_index + 1) // 12, (month_index + 1) % 12 + 1, 1)
    last_of_month  = next_month - timedelta(days=1)

    # Blocks that started before this month but still run into it are included too.
    all_blocks = ScheduleBlock.query.filter(
        ScheduleBlock.user_id == user_id,
        ScheduleBlock.start_date >= first_of_month - timedelta(days=366),
        ScheduleBlock.start_date <= last_of_month
    ).all()
    task_dates = set()

    for b in all_blocks:
        if b.doc_type == 'leave':
            for d_idx in range(b.days_duration):
                task_dates.add(b.start_date + timedelta(days=d_idx))
            continue
        doc = b.document
        if not _visible_to_technician(doc):
            continue
        for d_idx in range(b.days_duration):
            task_dates.add(b.start_date + timedelta(days=d_idx))

    # Build Sunday-first calendar grid (fixes original Monday-first misalignment)
    # Python weekday(): Mon=0 … Sun=6
    # In a Sun-first grid:  Sun→col0, Mon→col1, … Sat→col6
    # day_index = (weekday + 1) % 7
    weekday   = first_of_month.weekday()
    day_index = (weekday + 1) % 7
    cal_start = first_of_month - timedelta(days=day_index)

    last_weekday   = last_of_month.weekday()
    days_until_sat = (5 - last_weekday) % 7
    cal_end        = last_of_month + timedelta(days=days_until_sat)

    calendar_days = []
    cur = cal_start
    while cur <= cal_end:
        calendar_days.append({
            'date':             cur,
            'day_num':          cur.day,
            'is_current_month': cur.month == first_of_month.month,
            'is_today':         cur == today,
            'is_selected':      cur == selected_date,
            'has_tasks':        cur in task_dates,
            'date_str':         cur.strftime('%Y-%m-%d'),
        })
        cur += timedelta(days=1)

    tasks = _build_tasks_for_date(user_id, selected_date)

    current_month_name = format_month_year(first_of_month.year, first_of_month.month)

    return render_template('technician/dashboard.html',
        active_tab='calendar',
        today=today,
        selected_date=selected_date,
        current_month_name=current_month_name,
        month_offset=month_offset,
        calendar_days=calendar_days,
        tasks=tasks,
    )


# ==========================================
# 1b. AJAX — CALENDAR TASK PANEL FRAGMENT
# ==========================================
@technician_bp.route('/calendar-tasks')
@technician_required
def calendar_tasks():
    """Returns an HTML fragment for the task panel — consumed by dashboard JS."""
    user_id = session.get('user_id')
    today = wib_now().date()
    date_str = request.args.get('date', today.strftime('%Y-%m-%d'))
    try:
        selected_date = datetime.strptime(date_str, '%Y-%m-%d').date()
    except Exception:
        selected_date = today

    tasks = _build_tasks_for_date(user_id, selected_date)
    return render_template('technician/_task_panel.html',
        selected_date=selected_date,
        tasks=tasks,
    )


# ==========================================
# 2. TASK DETAIL & REPORT SUBMISSION
# ==========================================
@technician_bp.route('/task/<doc_type>/<int:doc_id>')
@login_required
def task_detail(doc_type, doc_id):
    user_id = session.get('user_id')
    user = db.session.get(User, user_id)
    doc = fetch_doc(doc_type, doc_id)
    if not doc:
        flash('Task not found.', 'danger')
        return redirect(url_for('technician.documents'))
    # Office accounts that may open the document see the task read-only.
    is_admin_view = bool(user) and user.can_open_document(doc.status)
    if not is_admin_view and (not user or user.role != ROLE_TECHNICIAN or not _technician_is_assigned(user.id, doc_type, doc_id)):
        flash('Access denied: you are not assigned to this document.', 'danger')
        return redirect(url_for('technician.documents'))
    if not is_admin_view and not _visible_to_technician(doc):
        flash(f'Document {doc.display_no} is on Hold.', 'warning')
        return redirect(url_for('technician.documents'))

    # All reports for this doc (any technician) — teams share visibility
    reports  = WorkReport.query.filter_by(document_id=doc.id).order_by(WorkReport.uploaded_at.desc()).all()
    today    = wib_now().date()

    # Query schedule blocks and build team members
    blocks = ScheduleBlock.query.filter_by(document_id=doc.id).all()
    all_team = []
    today_team = []

    for b in blocks:
        if b.technician and b.technician.name not in all_team:
            all_team.append(b.technician.name)
        for d_idx in range(b.days_duration):
            covered = b.start_date + timedelta(days=d_idx)
            if covered == today and b.technician and b.technician.name not in today_team:
                today_team.append(b.technician.name)

    captain = doc.captain_name
    if captain:
        all_team = [f'{name} (Captain)' if name == captain else name for name in all_team]
        today_team = [f'{name} (Captain)' if name == captain else name for name in today_team]

    return render_template('technician/task_detail.html',
        active_tab='documents',
        doc=doc,
        doc_type=doc_type,
        doc_label=get_doc_label(doc_type),
        doc_color=get_doc_color(doc_type),
        reports=reports,
        user_id=user_id,
        all_team=all_team,
        today_team=today_team,
        schedule_blocks=blocks,
        can_submit_report=not is_admin_view,
        today=today,
    )


@technician_bp.route('/report/submit', methods=['POST'])
@technician_required
def submit_report():
    doc_type    = (request.form.get('doc_type') or '').lower()
    doc_id      = request.form.get('doc_id', type=int)
    report_text = request.form.get('report_text', '').strip()
    report_title = request.form.get('report_title', '').strip()
    report_type = request.form.get('report_type', 'final')  # 'daily' | 'final'
    user_id     = session.get('user_id')

    if report_type not in ('daily', 'final'):
        abort(400, description='Invalid report type.')

    # Title is mandatory for every report. Validate before any file is saved
    # so a rejected request never leaves orphaned uploads behind.
    if not report_title:
        abort(400, description='The report title is required.')
    if len(report_title) > 200:
        abort(400, description='The report title can be at most 200 characters.')

    doc = fetch_doc(doc_type, doc_id)
    if not doc:
        flash('Document not found.', 'danger')
        return redirect(url_for('technician.documents'))
    if not _technician_is_assigned(user_id, doc_type, doc.id):
        flash('Access denied: you are not assigned to this document.', 'danger')
        return redirect(url_for('technician.documents'))

    def reject(message, category='danger', status=400):
        if _wants_json():
            return jsonify({'status': 'error', 'message': message}), status
        flash(message, category)
        return redirect(url_for('technician.task_detail', doc_type=doc_type, doc_id=doc.id))

    # Final documents (Completed or Void) no longer accept reports.
    if doc.status in FINAL_STATUSES:
        return reject(f'Document is {doc.status} and can no longer receive reports.', 'warning', 409)
    # A final report would also silently turn a Hold document into Pending Approval.
    if not _visible_to_technician(doc):
        message = f'Document {doc.display_no} is on Hold and cannot receive reports.'
        if _wants_json():
            return jsonify({'status': 'error', 'message': message}), 409
        flash(message, 'warning')
        return redirect(url_for('technician.documents'))

    if report_type == 'final':
        approved_final = WorkReport.query.filter_by(
            document_id=doc.id, report_type='final', status='Approved'
        ).first()
        if approved_final:
            return reject('The final report has already been approved and cannot be changed. Contact an Admin if it needs a revision.', 'warning', 409)

    uploaded_files = [f for f in request.files.getlist('photos') if f and f.filename]
    valid_ho_files = [f for f in request.files.getlist('ho_photos') if f and f.filename]
    rejected = [f.filename for f in uploaded_files + valid_ho_files if not is_allowed_upload(f.filename)]
    if rejected:
        return reject(f'Unsupported file format: {", ".join(rejected)}. Use JPG, PNG, WEBP, or PDF.')

    ho = getattr(doc, 'ho', None)
    if valid_ho_files and ho is not None and ho.is_locked:
        return reject(f'The HO is already "{ho.status}"; HO photos can no longer be added.', 'warning', 409)

    # Completing a PRJ (final report) requires a photo of the HO sheet signed on paper
    if report_type == 'final' and doc_type == 'prj':
        has_existing_ho_photos = bool(ho and ho.photos)
        if not valid_ho_files and not has_existing_ho_photos:
            return reject('Failed: a photo of the HO sheet signed by the client must be uploaded to complete the PRJ.')

    saved_photos = [save_document_upload(file, doc_type, doc, user_id) for file in uploaded_files]

    report_status = 'Daily' if report_type == 'daily' else 'Pending'
    replaced_paths = []

    if report_type == 'final':
        # A task has one current final report. Re-submitting it before approval
        # replaces its contents instead of creating duplicate final reports.
        report = WorkReport.query.filter_by(
            document_id=doc.id,
            report_type='final',
        ).first()

        if report:
            replaced_paths = report.photo_list
            report.uploaded_by = user_id
            report.uploaded_at = wib_now()
            report.report_title = report_title
            report.report_text = report_text
            report.photos.clear()
            report.status = report_status
            flash_msg = 'Completion report updated. The previous evidence has been replaced and awaits Admin approval.'
        else:
            report = WorkReport(
                document_id=doc.id,
                uploaded_by=user_id,
                report_type=report_type,
                report_title=report_title,
                report_text=report_text,
                status=report_status,
            )
            db.session.add(report)
            flash_msg = 'Completion report sent; it awaits Admin approval.'
    else:
        report = WorkReport(
            document_id=doc.id,
            uploaded_by=user_id,
            report_type=report_type,
            report_title=report_title,
            report_text=report_text,
            status=report_status,
        )
        db.session.add(report)
        flash_msg = 'Daily report saved.'

    for photo in saved_photos:
        report.photos.append(WorkReportPhoto(**photo))

    if report_type == 'final':
        doc.status = STATUS_PENDING

    # Save the signed HO photos, if any
    saved_ho_photos = []
    if ho is not None and valid_ho_files:
        for bfile in valid_ho_files:
            saved = save_document_upload(bfile, 'ho', doc, user_id)
            saved_ho_photos.append(saved)
            ho.photos.append(HOPhoto(**saved, uploaded_by=user_id, uploaded_at=wib_now()))

        ho.uploaded_by = user_id
        ho.uploaded_at = wib_now()

    # An PRJ's final upload always comes with HO evidence (checked above), so
    # the HO waits for approval together with its PRJ.
    if report_type == 'final' and ho is not None:
        ho.status = STATUS_PENDING

    try:
        db.session.commit()
    except IntegrityError:
        # The database is the final guard against a double-submit/race. Do not
        # leave orphaned evidence files if its uniqueness rules reject a write.
        db.session.rollback()
        remove_upload_files(saved_photos + saved_ho_photos)
        flash('The report could not be saved because the data just changed. Reload the page and try again.', 'warning')
        return redirect(url_for('technician.task_detail', doc_type=doc_type, doc_id=doc.id))

    # The database now points only to the replacement files, so old files may
    # safely be removed. Keep every path strictly inside UPLOAD_FOLDER.
    remove_upload_files(replaced_paths)

    flash(flash_msg, 'success')
    return redirect(url_for('technician.task_detail', doc_type=doc_type, doc_id=doc.id))


# ==========================================
# 4. DOCUMENTS TAB — ASSIGNED DOCUMENTS LIST
# ==========================================
@technician_bp.route('/documents')
@technician_required
def documents():
    """
    Lists all documents assigned to this technician via ScheduleBlock.
    Multi-team rule: any technician assigned to a doc can see & upload to it
    regardless of date or other team members' schedules.
    """
    user_id    = session.get('user_id')
    all_blocks = ScheduleBlock.query.filter_by(user_id=user_id).all()

    assigned_docs = []
    seen = set()

    for b in all_blocks:
        if b.doc_type == 'leave':
            continue
        doc = b.document
        key = (b.doc_type, b.doc_id)
        if _visible_to_technician(doc) and key not in seen:
            seen.add(key)
            assigned_docs.append({
                'doc_type':       b.doc_type,
                'doc_id':         doc.id,
                'doc_no':         doc.display_no,
                'client_name':    doc.client.company_name if doc.client else '',
                'client_address': _doc_address(doc),
                'status':         doc.status,
                'color':          get_doc_color(b.doc_type),
                'label':          get_doc_label(b.doc_type),
                'start_date':     b.start_date,
            })

    # Most recent assignment first
    assigned_docs.sort(key=lambda x: x['start_date'], reverse=True)

    search = request.args.get('q', '').strip()
    status_filter = request.args.get('filter', 'all')
    if status_filter not in ('all', 'active', 'completed'):
        status_filter = 'all'

    if search:
        search_lower = search.lower()
        assigned_docs = [item for item in assigned_docs if search_lower in ' '.join([
            item['doc_no'] or '', item['client_name'] or '', item['client_address'] or '',
            item['label'] or '', item['status'] or '',
        ]).lower()]

    total_count = len(assigned_docs)
    active_count = sum(item['status'] not in FINAL_STATUSES for item in assigned_docs)
    completed_count = total_count - active_count

    if status_filter == 'active':
        assigned_docs = [item for item in assigned_docs if item['status'] not in FINAL_STATUSES]
    elif status_filter == 'completed':
        assigned_docs = [item for item in assigned_docs if item['status'] in FINAL_STATUSES]

    per_page = 10
    total_filtered = len(assigned_docs)
    total_pages = max(1, (total_filtered + per_page - 1) // per_page)
    page = request.args.get('page', 1, type=int) or 1
    page = min(max(page, 1), total_pages)
    page_docs = assigned_docs[(page - 1) * per_page: page * per_page]

    page_numbers = []
    for page_number in range(1, total_pages + 1):
        if page_number in (1, total_pages) or abs(page_number - page) <= 1:
            if page_numbers and page_number - page_numbers[-1] > 1:
                page_numbers.append(None)
            page_numbers.append(page_number)

    return render_template('technician/documents.html',
        active_tab='documents',
        assigned_docs=page_docs,
        search=search,
        status_filter=status_filter,
        total_count=total_count,
        active_count=active_count,
        completed_count=completed_count,
        total_filtered=total_filtered,
        page=page,
        total_pages=total_pages,
        page_numbers=page_numbers,
    )


# ==========================================
# 5. SETTINGS TAB
# ==========================================
@technician_bp.route('/settings')
@technician_required
def settings():
    user_id = session.get('user_id')
    user    = db.session.get(User, user_id)
    return render_template('technician/settings.html',
        active_tab='settings',
        user=user,
    )


# ==========================================
# 6. HISTORY — REDIRECTS TO DOCUMENTS TAB
# ==========================================
@technician_bp.route('/history')
@technician_required
def history():
    return redirect(url_for('technician.documents'))


@technician_bp.route('/media/<path:stored_path>')
@login_required
def protected_media(stored_path):
    """Serve private evidence files only to a user allowed to view their parent record."""
    user = db.session.get(User, session.get('user_id'))
    if not user:
        abort(403)

    photo = WorkReportPhoto.query.filter_by(stored_path=stored_path).first()
    report = photo.report if photo else None
    if report:
        allowed = user.can_open_document(report.document.status) or (
            user.role == ROLE_TECHNICIAN and _technician_is_assigned(user.id, report.doc_type, report.doc_id)
        )
        if not allowed:
            abort(403)
        return send_from_directory(current_app.config['UPLOAD_FOLDER'], stored_path)

    ho_photo = HOPhoto.query.filter_by(stored_path=stored_path).first()
    if ho_photo:
        ho = ho_photo.ho
        allowed = user.can_open_document(ho.prj.status) or (
            user.role == ROLE_TECHNICIAN and _technician_is_assigned(user.id, 'prj', ho.prj_id)
        )
        if not allowed:
            abort(403)
        return send_from_directory(current_app.config['UPLOAD_FOLDER'], stored_path)

    abort(404)
