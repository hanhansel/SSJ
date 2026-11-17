import math
import os
import json

from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from datetime import datetime
from sqlalchemy.orm import joinedload, with_polymorphic
from apps.models import (
    db, Role, User, ClientSite, Document, DocumentSVC, DocumentXPRJ, DocumentPRJ,
    MasterProject, WorkReport, ScheduleBlock,
    HO, HOPhoto, wib_now,
    STATUS_ONLINE, STATUS_PENDING, STATUS_DONE, STATUS_VOID, STATUS_HOLD, ACTIVE_STATUSES, FINAL_STATUSES,
    HO_APPROVED, ROLE_TECHNICIAN, MENU_DOCUMENTS, MENU_MASTER_PROJECT, MENU_STORAGE,
)
from apps.core.registry import (
    generate_doc_number, fetch_doc, get_doc_label, is_allowed_upload,
)
from apps.core.auth import current_user, deny_unless_can_open, list_menu_required, menu_required
from apps.core.paths import get_runtime_paths
from apps.core.uploads import save_document_upload, remove_upload_files
from apps.services import export_service
from apps.services.document_service import DocumentService, parse_positive_int
from apps.services.import_service import DocumentImportService, picker_clients
from apps.services.prj_import_service import PRJImportService
from apps.services.project_service import KPI_FILTERS, ProjectService
from apps.system.routes import get_all_client_types
from apps.services.print_template_service import render_custom_print, render_missing_print
import uuid

documents_bp = Blueprint('documents', __name__, url_prefix='/documents')


def _safe_local_redirect(target, fallback):
    """Only follow same-site relative paths (blocks //evil.com and absolute URLs)."""
    if target and target.startswith('/') and not target.startswith('//') and '\\' not in target:
        return redirect(target)
    return redirect(fallback)


def _active_master_projects():
    return MasterProject.query.filter_by(status=STATUS_ONLINE).order_by(MasterProject.name).all()


def _parent_prj_choices():
    """PRJs an XPRJ may hang under: any PRJ except a Void one (extra work often follows a finished PRJ)."""
    return DocumentPRJ.query.filter(DocumentPRJ.status != STATUS_VOID).order_by(DocumentPRJ.id.desc()).all()


def _completion_impacts(docs):
    """Popup data for the ACC button of each active document in a list, keyed by document id."""
    return {doc.id: DocumentService.status_change_impacts(doc, STATUS_DONE) for doc in docs}


def _master_project_active_prjs(projects):
    """JSON-safe list of the active PRJs that keep each project from being closed."""
    return {project.id: ProjectService.active_prj_summary(project) for project in projects}


def _json_list_of_dicts(raw_json, error_message):
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError:
        raise ValueError(error_message)
    if not isinstance(data, list) or not all(isinstance(row, dict) for row in data):
        raise ValueError(error_message)
    return data


def _prj_items_from_form():
    """Validate repeating PRJ work-item fields from JSON payload or form lists."""
    items_json = request.form.get('prj_items_json')
    if items_json:
        raw_items = _json_list_of_dicts(items_json, 'Invalid PRJ work item data.')

        items = []
        for index, raw in enumerate(raw_items):
            description = str(raw.get('description', '')).strip()
            if not description:
                continue
            try:
                qty = int(raw.get('qty', 1))
                if qty < 1:
                    raise ValueError
            except (ValueError, TypeError):
                raise ValueError(f'The quantity of work item #{index + 1} must be a number of at least 1.')
                
            uom = str(raw.get('uom', 'Unit')).strip() or 'Unit'
            items.append({'description': description, 'qty': qty, 'uom': uom, 'display_order': len(items) + 1})
            
        if not items:
            raise ValueError('At least one work description is required.')
        return items

    descriptions = request.form.getlist('item_description')
    quantities = request.form.getlist('item_qty')
    uoms = request.form.getlist('item_uom')

    items = []
    for index, raw_desc in enumerate(descriptions):
        description = str(raw_desc).strip()
        if not description:
            continue
        try:
            qty = int(quantities[index]) if index < len(quantities) else 1
            if qty < 1:
                raise ValueError
        except (ValueError, TypeError):
            raise ValueError(f'The quantity of work item #{index + 1} must be a number of at least 1.')
        uom = str(uoms[index]).strip() if index < len(uoms) else 'Unit'
        items.append({'description': description, 'qty': qty, 'uom': uom or 'Unit', 'display_order': len(items) + 1})

    if not items:
        raise ValueError('At least one work description is required.')
    return items


def _prj_technician_tasks_from_form():
    """Validate repeating PRJ technician tasks from JSON payload or form lists."""
    tasks_json = request.form.get('prj_tasks_json')
    if tasks_json:
        # Raise instead of returning [] so a broken payload never wipes existing tasks on edit.
        raw_tasks = _json_list_of_dicts(tasks_json, 'Invalid PRJ technician task data.')

        tasks = []
        for raw in raw_tasks:
            description = str(raw.get('description', '')).strip()
            if description:
                tasks.append({'description': description, 'display_order': len(tasks) + 1})
        return tasks

    descriptions = request.form.getlist('technician_task_description')
    tasks = []
    for raw_desc in descriptions:
        description = str(raw_desc).strip()
        if description:
            tasks.append({'description': description, 'display_order': len(tasks) + 1})
    return tasks


@documents_bp.route('/master-projects', methods=['GET', 'POST'])
@menu_required(MENU_MASTER_PROJECT)
def master_projects():
    if request.method == 'POST':
        name = (request.form.get('name') or '').strip()
        client_id = request.form.get('client_id', type=int)
        client = db.session.get(ClientSite, client_id) if client_id else None
        if not name or not client:
            flash('Master Project name and Client / Site are required.', 'danger')
        else:
            db.session.add(MasterProject(name=name, client_id=client.id, site_address=client.address or '',
                                         work_scope=(request.form.get('work_scope') or '').strip(),
                                         notes=(request.form.get('notes') or '').strip(), status='Online'))
            db.session.commit()
            flash('Master Project added.', 'success')
        return redirect(url_for('documents.master_projects'))
    return _master_project_list((STATUS_ONLINE, STATUS_HOLD), is_storage=False)


@documents_bp.route('/master-projects/completed')
@menu_required(MENU_MASTER_PROJECT)
def completed_master_projects():
    return _master_project_list((STATUS_DONE,), is_storage=True)


@documents_bp.route('/master-projects/export')
@menu_required(MENU_MASTER_PROJECT)
def export_master_projects():
    """Every Master Project ever made, whatever its status or the filters on screen."""
    projects = MasterProject.query.order_by(MasterProject.name, MasterProject.id).all()
    return send_file(
        export_service.master_projects_workbook([(p, ProjectService.project_kpi(p)) for p in projects]),
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        as_attachment=True,
        download_name=f"Master_Project_Data_{wib_now().strftime('%Y%m%d_%H%M')}.xlsx",
    )


def _master_project_list(statuses, is_storage):
    """A Master Project page: the projects in ``statuses``, filtered by search, status and KPI."""
    search = request.args.get('q', '').strip()
    status = request.args.get('status', '')
    status = status if len(statuses) > 1 and status in statuses else ''
    kpi_filter = request.args.get('kpi', '')
    kpi_filter = kpi_filter if kpi_filter in KPI_FILTERS else ''
    page = max(1, request.args.get('page', 1, type=int))
    query = MasterProject.query.options(joinedload(MasterProject.client)).filter(
        MasterProject.status.in_((status,) if status else statuses))
    if search:
        pattern = f'%{search}%'
        query = query.join(ClientSite).filter(db.or_(
            MasterProject.name.ilike(pattern),
            ClientSite.company_name.ilike(pattern),
        ))
    per_page = 15
    # The KPI follows the live schedule, so its filter runs on the computed KPI.
    projects = query.order_by(MasterProject.name).all()
    kpi_by_project = {p.id: ProjectService.project_kpi(p) for p in projects}
    if kpi_filter:
        projects = [p for p in projects if kpi_by_project[p.id]['status'] == KPI_FILTERS[kpi_filter]]
    total = len(projects)
    projects = projects[(page - 1) * per_page:page * per_page]
    return render_template(
        'documents/master_projects.html', projects=projects,
        clients=ClientSite.query.order_by(ClientSite.company_name).all(), is_storage=is_storage,
        search=search, status_filter=status, kpi_filter=kpi_filter, page=page,
        total_pages=max(1, math.ceil(total / per_page)),
        active_prjs_by_project=_master_project_active_prjs(projects),
        upcoming_blocks_by_project={p.id: ProjectService.upcoming_block_count(p) for p in projects},
        kpi_by_project=kpi_by_project,
    )


@documents_bp.route('/master-projects/<int:project_id>', methods=['POST'])
@menu_required(MENU_MASTER_PROJECT)
def update_master_project(project_id):
    project = db.session.get(MasterProject, project_id)
    if not project:
        flash('Master Project not found.', 'danger')
        return redirect(url_for('documents.master_projects'))

    name = (request.form.get('name') or '').strip()
    status = request.form.get('status', STATUS_ONLINE)
    notes = (request.form.get('notes') or '').strip()
    work_scope = request.form.get('work_scope')
    work_scope = work_scope.strip() if work_scope is not None else None
    was_closed = project.status == STATUS_DONE
    was_on_hold = project.status == STATUS_HOLD

    success, err_msg = ProjectService.update_master_project(project, name, status, notes, work_scope)
    if success and status == STATUS_HOLD and not was_on_hold:
        flash(f'Master Project "{project.name}" is on Hold. Its PRJs are hidden from Scheduling and the '
              'technician app until the project is Online again; the PRJ statuses do not change.', 'success')
    elif success:
        flash('Master Project updated.', 'success')
    else:
        flash(err_msg, 'danger')

    return redirect(url_for('documents.completed_master_projects' if was_closed else 'documents.master_projects'))


@documents_bp.route('/prj/<int:doc_id>/edit', methods=['GET', 'POST'])
@menu_required(MENU_DOCUMENTS)
def edit_prj(doc_id):
    prj = db.session.get(DocumentPRJ, doc_id)
    if not prj:
        flash('PRJ not found.', 'danger')
        return redirect(url_for('documents.prj_list'))

    if prj.status in FINAL_STATUSES:
        flash(f'A PRJ with status {prj.status} cannot be edited.', 'warning')
        return redirect(url_for('documents.view_doc', doc_type='prj', doc_id=prj.id))

    if request.method == 'GET':
        return redirect(url_for('documents.view_doc', doc_type='prj', doc_id=prj.id, edit='1'))

    client_id = request.form.get('client_id', type=int)
    title = (request.form.get('title') or '').strip()
    so_number = (request.form.get('so_number') or '').strip()
    site_address = (request.form.get('site_address') or '').strip()

    try:
        estimated_days = parse_positive_int(request.form.get('estimated_calendar_days'), 'Estimasi hari', default=5)
        items_data = _prj_items_from_form()
        tasks_data = _prj_technician_tasks_from_form()
    except ValueError as exc:
        flash(str(exc), 'danger')
        return redirect(url_for('documents.view_doc', doc_type='prj', doc_id=prj.id, edit='1'))

    master_project_id = request.form.get('master_project_id', type=int)
    urgency_level = request.form.get('urgency_level')
    notes = (request.form.get('notes') or request.form.get('admin_notes') or '').strip()

    success, err_msg = DocumentService.edit_prj(
        prj=prj,
        client_id=client_id,
        title=title,
        so_number=so_number,
        site_address=site_address,
        estimated_days=estimated_days,
        master_project_id=master_project_id,
        urgency_level=urgency_level,
        notes=notes,
        items_data=items_data,
        tasks_data=tasks_data
    )

    if not success:
        flash(err_msg, 'danger')
        return redirect(url_for('documents.view_doc', doc_type='prj', doc_id=prj.id, edit='1'))

    flash('PRJ updated.', 'success')
    return redirect(url_for('documents.view_doc', doc_type='prj', doc_id=prj.id))


def _export_links(endpoint):
    """Export popup of a list page: (label, link) per period, each with the filters on screen and every page."""
    args = request.args.to_dict()
    args.pop('page', None)
    return [(label, url_for(endpoint, **args, period=period))
            for period, (label, _) in export_service.EXPORT_PERIODS.items()]


def _export_period(args):
    """(period key, first date, last date) of an export; the list pages themselves have no period."""
    period = args.get('period', 'all')
    if period not in export_service.EXPORT_PERIODS:
        period = 'all'
    return (period,) + export_service.period_range(period, wib_now().date())


def _xlsx_download(output, prefix, is_storage, period):
    filename = (f"{prefix}_{'Completed' if is_storage else 'Active'}_{export_service.EXPORT_PERIODS[period][1]}_"
                f"{wib_now().strftime('%Y%m%d_%H%M')}.xlsx")
    return send_file(
        output,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        as_attachment=True,
        download_name=filename,
    )


def _prj_filters(args):
    """Filters of the PRJ list, read from its query string (shared by the list and its export)."""
    status = args.get('status', 'all')
    return {
        'search': args.get('q', '').strip(),
        'status': status if status in ACTIVE_STATUSES + (STATUS_HOLD,) else 'all',
        'is_storage': args.get('storage') == '1',
        'period': _export_period(args),
    }


def _prj_query(filters):
    """PRJs matching the filters, newest first."""
    query = DocumentPRJ.query.options(
        joinedload(DocumentPRJ.client),
        joinedload(DocumentPRJ.master_project)
    )
    if filters['is_storage']:
        query = query.filter(DocumentPRJ.status.in_(FINAL_STATUSES))
    elif filters['status'] != 'all':
        query = query.filter_by(status=filters['status'])
    else:
        query = query.filter(DocumentPRJ.status.in_(ACTIVE_STATUSES))
    if filters['search']:
        pattern = f"%{filters['search']}%"
        query = query.join(ClientSite).outerjoin(
            MasterProject, DocumentPRJ.master_project_id == MasterProject.id
        ).filter(db.or_(
            db.cast(DocumentPRJ.doc_no, db.String).ilike(pattern),
            DocumentPRJ.title.ilike(pattern),
            DocumentPRJ.so_number.ilike(pattern),
            ClientSite.company_name.ilike(pattern),
            MasterProject.name.ilike(pattern),
        ))
    _period, first, last = filters['period']
    if first:
        query = query.filter(DocumentPRJ.date >= first)
    if last:
        query = query.filter(DocumentPRJ.date <= last)
    return query.order_by(DocumentPRJ.date.desc(), DocumentPRJ.id.desc())


@documents_bp.route('/prj')
@list_menu_required
def prj_list():
    filters = _prj_filters(request.args)
    is_storage = filters['is_storage']
    page = max(1, request.args.get('page', 1, type=int))
    query = _prj_query(filters)
    per_page = 15
    total = query.count()
    prjs = query.offset((page - 1) * per_page).limit(per_page).all()
    return render_template('documents/prj_list.html', prjs=prjs, search=filters['search'], status=filters['status'],
                           is_storage=is_storage, page=page, total_pages=max(1, math.ceil(total / per_page)),
                           completion_impacts={} if is_storage else _completion_impacts(prjs),
                           export_links=_export_links('documents.export_prj'))


@documents_bp.route('/prj/export')
@list_menu_required
def export_prj():
    """Every PRJ the list shows with the same filters, as an Excel file the PRJ import can read."""
    filters = _prj_filters(request.args)
    output = export_service.prj_workbook(_prj_query(filters).all())
    return _xlsx_download(output, 'PRJ_Data', filters['is_storage'], filters['period'][0])




# ==========================================
# 1. DOCUMENT GENERATOR (SVC & XPRJ)
# ==========================================
@documents_bp.route('/generator', methods=['GET', 'POST'])
@menu_required(MENU_DOCUMENTS)
def generator():
    today = wib_now().date()

    if request.method == 'POST':
        doc_type = (request.form.get('doc_type') or 'svc').lower()
        client_id = request.form.get('client_id', type=int)
        date_str = request.form.get('doc_date')

        try:
            doc_date = datetime.strptime(date_str, '%Y-%m-%d').date() if date_str else today
        except Exception:
            doc_date = today

        client = db.session.get(ClientSite, client_id) if client_id else None
        if not client:
            flash('Failed: please choose a valid client.', 'danger')
            return redirect(url_for('documents.generator', type=doc_type))

        custom_no = request.form.get('doc_no', '').strip()
        if doc_type == 'prj':
            title = (request.form.get('prj_title') or '').strip()
            so_number = (request.form.get('prj_so_number') or '').strip()
            master_project_id = request.form.get('master_project_id', type=int)
            urgency_level = request.form.get('prj_urgency_level')
            notes = (request.form.get('prj_notes') or '').strip()

            try:
                estimated_days = parse_positive_int(request.form.get('prj_estimated_calendar_days'), 'Estimasi hari', default=5)
                items_data = _prj_items_from_form()
                tasks_data = _prj_technician_tasks_from_form()
            except ValueError as exc:
                flash(str(exc), 'danger')
                return redirect(url_for('documents.generator', type='prj'))

            success, new_doc, err_msg = DocumentService.create_prj(
                client_id=client_id,
                title=title,
                so_number=so_number,
                site_address=(request.form.get('site_address') or '').strip(),
                doc_date=doc_date,
                estimated_days=estimated_days,
                master_project_id=master_project_id,
                custom_no=custom_no,
                urgency_level=urgency_level,
                notes=notes,
                items_data=items_data,
                tasks_data=tasks_data
            )
            
            if not success:
                flash(err_msg, 'danger')
                return redirect(url_for('documents.generator', type='prj'))
                
            flash(f'PRJ "{new_doc.display_no}" and its HO "{new_doc.ho.display_no}" were issued.', 'success')
            return redirect(url_for('documents.view_doc', doc_type='prj', doc_id=new_doc.id))

        customer_complaint = (request.form.get('customer_complaint') or '').strip()
        task_description = (request.form.get('task_description') or '').strip()

        if not customer_complaint:
            flash('Failed: Customer Complaint is required and cannot be empty.', 'danger')
            return redirect(url_for('documents.generator', type=doc_type))

        if not task_description:
            flash('Failed: Task Description is required and cannot be empty.', 'danger')
            return redirect(url_for('documents.generator', type=doc_type))

        estimate_field = 'estimated_calendar_days_xprj' if doc_type == 'xprj' else 'estimated_calendar_days'
        try:
            est_days = parse_positive_int(request.form.get(estimate_field), 'Estimasi hari', default=2)
        except ValueError as exc:
            flash(str(exc), 'danger')
            return redirect(url_for('documents.generator', type=doc_type))

        if doc_type == 'svc':
            reference = request.form.get('reference', '').strip()
            site_address = request.form.get('site_address', '').strip()
            notes = request.form.get('notes', '').strip()

            success, new_doc, err_msg = DocumentService.create_svc(
                client_id=client_id,
                doc_date=doc_date,
                custom_no=custom_no,
                customer_complaint=customer_complaint,
                task_description=task_description,
                estimated_days=est_days,
                reference=reference,
                site_address=site_address,
                notes=notes
            )
            
            if not success:
                flash(err_msg, 'danger')
                return redirect(url_for('documents.generator', type='svc'))
                
            flash(f'SVC "{new_doc.display_no}" was issued.', 'success')
            return redirect(url_for('documents.view_doc', doc_type='svc', doc_id=new_doc.id))

        elif doc_type == 'xprj':
            reference = (request.form.get('reference') or request.form.get('xprj_reference') or '').strip()
            prj_id_val = request.form.get('prj_id')
            site_address = request.form.get('site_address', '').strip()
            notes = request.form.get('notes', '').strip()

            success, new_doc, err_msg = DocumentService.create_xprj(
                client_id=client_id,
                doc_date=doc_date,
                custom_no=custom_no,
                customer_complaint=customer_complaint,
                task_description=task_description,
                estimated_days=est_days,
                prj_id_val=prj_id_val,
                reference=reference,
                site_address=site_address,
                notes=notes
            )
            
            if not success:
                flash(err_msg, 'danger')
                return redirect(url_for('documents.generator', type='xprj'))

            flash(f'XPRJ "{new_doc.display_no}" was issued.', 'success')
            return redirect(url_for('documents.view_doc', doc_type='xprj', doc_id=new_doc.id))

        else:
            flash('This document type is not supported.', 'danger')
            return redirect(url_for('documents.generator'))

    # GET preview
    svc_preview, _ = generate_doc_number('svc', today)
    xprj_preview, _ = generate_doc_number('xprj', today)
    prj_preview, _ = generate_doc_number('prj', today)

    return render_template(
        'documents/generator.html',
        clients=ClientSite.query.order_by(ClientSite.company_name).all(),
        today=today,
        projects=_active_master_projects(),
        parent_prjs=_parent_prj_choices(),
        client_types=get_all_client_types(),
        svc_preview=svc_preview,
        xprj_preview=xprj_preview,
        prj_preview=prj_preview,
        selected_type=request.args.get('type', '')
    )


# ==========================================
# 2. ACTIVE & STORAGE DOCUMENTS LIST
# ==========================================
def _svc_xprj_filters(args):
    """Filters of the SVC & XPRJ list, read from its query string (shared by the list and its export)."""
    filter_type = args.get('type', 'all').lower()  # 'all', 'svc', 'xprj'
    status_filter = args.get('status', 'all').lower()
    return {
        'is_storage': args.get('storage') == '1',
        'type': filter_type if filter_type in ('all', 'svc', 'xprj') else 'all',
        'status': status_filter if status_filter in ('all', 'pending_approval', 'hold') else 'all',
        'search': args.get('q', '').strip().lower(),
        'start_date': args.get('start_date', '').strip(),
        'end_date': args.get('end_date', '').strip(),
        'client_id': args.get('client_id', '').strip(),
        'tech_id': args.get('tech_id', '').strip(),
        'period': _export_period(args),
    }


def _svc_xprj_query(filters):
    """SVC and XPRJ documents matching the filters, newest first."""
    # SVC and XPRJ are listed together: one query over both tables, filtered, counted and
    # paged by the database like the PRJ and HO lists, so only the shown page is loaded.
    docs = with_polymorphic(Document, [DocumentSVC, DocumentXPRJ])
    completion = db.func.coalesce(docs.DocumentSVC.actual_completion, docs.DocumentXPRJ.actual_completion)
    doc_types = ('svc', 'xprj') if filters['type'] == 'all' else (filters['type'],)
    query = db.session.query(docs).options(joinedload(docs.client)).filter(docs.document_type.in_(doc_types))

    if filters['is_storage']:
        query = query.filter(docs.status.in_(FINAL_STATUSES))
    else:
        # Hold documents are left out of the active list and shown only under the Hold filter.
        listed = (STATUS_HOLD,) if filters['status'] == 'hold' else ACTIVE_STATUSES
        query = query.filter(docs.status.in_(listed))

        if filters['status'] == 'pending_approval':
            query = query.filter(docs.status == STATUS_PENDING)

    # Date filtering: storage filters on the completion date, the active list on the document date.
    date_column = completion if filters['is_storage'] else docs.date
    if filters['start_date']:
        try:
            query = query.filter(date_column >= datetime.strptime(filters['start_date'], '%Y-%m-%d').date())
        except ValueError:
            pass

    if filters['end_date']:
        try:
            query = query.filter(date_column <= datetime.strptime(filters['end_date'], '%Y-%m-%d').date())
        except ValueError:
            pass

    # The export period always looks at the document date.
    _period, first, last = filters['period']
    if first:
        query = query.filter(docs.date >= first)
    if last:
        query = query.filter(docs.date <= last)

    if filters['client_id']:
        try:
            query = query.filter(docs.client_id == int(filters['client_id']))
        except ValueError:
            pass

    if filters['search']:
        pattern = f"%{filters['search']}%"
        query = query.join(ClientSite, docs.client_id == ClientSite.id).filter(
            db.or_(
                db.cast(docs.doc_no, db.String).ilike(pattern),
                ClientSite.company_name.ilike(pattern)
            )
        )

    if filters['tech_id']:
        try:
            assigned_ids = db.session.query(ScheduleBlock.document_id).filter_by(user_id=int(filters['tech_id']))
            query = query.filter(docs.id.in_(assigned_ids))
        except ValueError:
            pass

    # Newest first: finished documents by completion date, active ones by document date.
    return query.order_by(db.func.coalesce(completion, docs.date).desc(), docs.id.desc())


@documents_bp.route('/list')
@documents_bp.route('/list_svc')
@list_menu_required
def list_docs():
    filters = _svc_xprj_filters(request.args)
    is_storage = filters['is_storage']
    filter_type = filters['type']
    status_filter = filters['status']
    search = filters['search']
    tech_id = filters['tech_id']
    page_title = "Completed SVC & XPRJ Documents" if is_storage else "SVC & XPRJ Documents"
    try:
        page = max(1, int(request.args.get('page', 1)))
    except (TypeError, ValueError):
        page = 1
    per_page = 15

    query = _svc_xprj_query(filters)
    total_documents = query.count()
    total_pages = max(1, math.ceil(total_documents / per_page))
    page = min(page, total_pages)
    page_docs = query.offset((page - 1) * per_page).limit(per_page).all()
    page_numbers = range(max(1, page - 2), min(total_pages, page + 2) + 1)

    clients = ClientSite.query.order_by(ClientSite.company_name).all()
    technicians = User.query.join(Role).filter(Role.code == ROLE_TECHNICIAN, User.is_active.is_(True)).order_by(User.name).all()

    page_documents = [{
        'id': item.id,
        'doc_type': item.document_type,
        'doc_no': item.display_no,
        'date': item.date,
        'completion_date': item.actual_completion,
        'client': item.client,
        'status': item.status,
        'technicians': item.assigned_technicians,
        'obj': item,
    } for item in page_docs]

    return render_template('documents/list.html',
        documents=page_documents,
        clients=clients,
        technicians=technicians,
        selected_tech_id=tech_id,
        is_storage=is_storage,
        filter_type=filter_type,
        status_filter=status_filter,
        search=search,
        start_date=filters['start_date'],
        end_date=filters['end_date'],
        client_id=filters['client_id'],
        page_title=page_title,
        page=page,
        per_page=per_page,
        total_documents=total_documents,
        total_pages=total_pages,
        page_numbers=page_numbers,
        completion_impacts={} if is_storage else _completion_impacts(item['obj'] for item in page_documents),
        export_links=_export_links('documents.export_docs'),
    )


# ==========================================
# 3. EXPORT EXCEL (ACTIVE & STORAGE)
# ==========================================
@documents_bp.route('/export')
@list_menu_required
def export_docs():
    """Every SVC and XPRJ the list shows with the same filters, as an Excel file the import can read."""
    filters = _svc_xprj_filters(request.args)
    output = export_service.svc_xprj_workbook(_svc_xprj_query(filters).all())
    return _xlsx_download(output, 'SVC_XPRJ_Data', filters['is_storage'], filters['period'][0])


# ==========================================
# 4. VIEW DOCUMENT DETAIL
# ==========================================
@documents_bp.route('/view/<doc_type>/<int:doc_id>')
@menu_required(MENU_DOCUMENTS, MENU_STORAGE)
def view_doc(doc_type, doc_id):
    doc = fetch_doc(doc_type, doc_id)
    if not doc:
        flash('Document not found.', 'danger')
        return redirect(url_for('documents.list_docs'))
    denied = deny_unless_can_open(doc)
    if denied:
        return denied

    label = get_doc_label(doc_type)
    reports = WorkReport.query.filter_by(document_id=doc.id).order_by(WorkReport.uploaded_at.desc()).all()
    pending_final_report = next(
        (report for report in reports if report.report_type == 'final' and report.status == 'Pending'),
        None,
    )
    blocks = ScheduleBlock.query.filter_by(document_id=doc.id).all()

    # Modular template routing: PRJ uses view_prj.html, SVC & XPRJ use view_svc_xprj.html
    template_name = 'documents/view_prj.html' if doc_type.lower() == 'prj' else 'documents/view_svc_xprj.html'

    clients = ClientSite.query.order_by(ClientSite.company_name).all()
    projects = []
    parent_prjs = []

    if doc_type.lower() == 'prj':
        projects = _active_master_projects()
        if doc.master_project and doc.master_project not in projects:
            projects.append(doc.master_project)
    elif doc_type.lower() == 'xprj':
        parent_prjs = _parent_prj_choices()
        if doc.prj_parent and doc.prj_parent not in parent_prjs:
            parent_prjs.append(doc.prj_parent)

    # Completed and Void are final: such a document is read-only everywhere.
    is_completed = doc.status in FINAL_STATUSES or request.args.get('storage') == '1'
    is_edit_mode = request.args.get('edit') == '1' and not is_completed
    status_impacts = {} if is_completed else {
        target: DocumentService.status_change_impacts(doc, target) for target in (STATUS_DONE, STATUS_VOID, STATUS_HOLD)
    }

    return render_template(template_name,
        doc=doc,
        doc_type=doc_type.lower(),
        doc_label=label,
        reports=reports,
        pending_final_report=pending_final_report,
        schedule_blocks=blocks,
        clients=clients,
        projects=projects,
        parent_prjs=parent_prjs,
        status_impacts=status_impacts,
        is_completed=is_completed,
        is_edit_mode=is_edit_mode
    )


# ==========================================
# 5. PRINT DOCUMENT (uploaded print template; see Settings > Print Templates)
# ==========================================
@documents_bp.route('/print/<doc_type>/<int:doc_id>')
@menu_required(MENU_DOCUMENTS, MENU_STORAGE)
def print_doc(doc_type, doc_id):
    doc = fetch_doc(doc_type, doc_id)
    if not doc:
        flash('Document not found.', 'danger')
        return redirect(url_for('documents.list_docs'))
    denied = deny_unless_can_open(doc)
    if denied:
        return denied

    doc_t = doc_type.lower()
    custom_page = render_custom_print(doc_t, doc)
    if custom_page is not None:
        return custom_page
    # No template uploaded yet: show how to make one and the variables it can use.
    return render_missing_print(doc_t)


# ==========================================
# 5b. PRINT HO
# ==========================================
@documents_bp.route('/ho/<int:ho_id>/print')
@documents_bp.route('/print/ho/<int:ho_id>')
@menu_required(MENU_DOCUMENTS, MENU_STORAGE)
def print_ho(ho_id):
    ho = db.session.get(HO, ho_id)
    if not ho:
        flash('HO document not found.', 'danger')
        return redirect(url_for('documents.list_docs'))
    denied = deny_unless_can_open(ho.prj)
    if denied:
        return denied
    custom_page = render_custom_print('ho', ho)
    if custom_page is not None:
        return custom_page
    return render_missing_print('ho')


def _ho_return_url(ho):
    return url_for('documents.view_doc', doc_type='prj', doc_id=ho.prj_id)


# ==========================================
# 5d. HO PHOTO UPLOAD & MANAGEMENT
# ==========================================
@documents_bp.route('/ho/<int:ho_id>/upload-photo', methods=['POST'])
@menu_required(MENU_DOCUMENTS)
def upload_ho_photo(ho_id):
    ho = db.session.get(HO, ho_id)
    if not ho:
        flash('HO document not found.', 'danger')
        return redirect(request.referrer or url_for('documents.ho_list'))

    if ho.is_locked:
        flash(f'The HO is already "{ho.status}"; photos can no longer be added or changed.', 'warning')
        return redirect(_ho_return_url(ho))

    files = request.files.getlist('ho_photos')
    valid_files = [f for f in files if f and f.filename and f.filename.strip()]
    if not valid_files:
        flash('Choose at least one signed HO photo to upload.', 'warning')
        return redirect(_ho_return_url(ho))
    rejected = [f.filename for f in valid_files if not is_allowed_upload(f.filename)]
    if rejected:
        flash(f'File format not supported: {", ".join(rejected)}. Use JPG, PNG, WEBP or PDF.', 'danger')
        return redirect(_ho_return_url(ho))

    user_id = session.get('user_id')
    for bfile in valid_files:
        saved = save_document_upload(bfile, 'ho', ho.prj, user_id)
        ho.photos.append(HOPhoto(**saved, uploaded_by=user_id, uploaded_at=wib_now()))

    ho.uploaded_by = user_id
    ho.uploaded_at = wib_now()
    db.session.commit()

    flash(f'{len(valid_files)} signed HO photos uploaded.', 'success')
    return redirect(_ho_return_url(ho))


@documents_bp.route('/ho/photo/<int:photo_id>/delete', methods=['POST'])
@menu_required(MENU_DOCUMENTS)
def delete_ho_photo(photo_id):
    photo = db.session.get(HOPhoto, photo_id)
    if not photo:
        flash('HO photo not found.', 'danger')
        return redirect(request.referrer or url_for('documents.ho_list'))

    ho = photo.ho
    if ho.is_locked:
        flash(f'The HO is already "{ho.status}"; photos can no longer be deleted.', 'warning')
        return redirect(_ho_return_url(ho))

    stored_path = photo.stored_path
    db.session.delete(photo)
    db.session.commit()
    remove_upload_files([stored_path])
    flash('Signed HO photo deleted.', 'success')
    return redirect(_ho_return_url(ho))


# ==========================================
# 5e. HO TRACKER / STORAGE LIST
# ==========================================
@documents_bp.route('/ho')
@list_menu_required
def ho_list():
    search = request.args.get('q', '').strip()
    is_storage = request.args.get('storage') == '1'
    status = request.args.get('status', 'all')
    page = max(1, request.args.get('page', 1, type=int))
    per_page = 15

    query = HO.query.options(
        joinedload(HO.prj).joinedload(DocumentPRJ.client),
        joinedload(HO.uploader),
        joinedload(HO.approver)
    )

    if is_storage:
        query = query.filter(HO.status.in_([HO_APPROVED, STATUS_VOID]))
    elif status == STATUS_HOLD:
        query = query.filter(HO.status == STATUS_HOLD)
    else:
        query = query.filter(HO.status.in_(ACTIVE_STATUSES))

    if search:
        pattern = f'%{search}%'
        query = query.join(DocumentPRJ, HO.prj_id == DocumentPRJ.id).join(ClientSite, DocumentPRJ.client_id == ClientSite.id).filter(
            db.or_(
                HO.ho_number_text.ilike(pattern),
                DocumentPRJ.doc_no.cast(db.String).ilike(pattern),
                DocumentPRJ.so_number.ilike(pattern),
                DocumentPRJ.title.ilike(pattern),
                ClientSite.company_name.ilike(pattern)
            )
        )

    total = query.count()
    hos = query.order_by(HO.date.desc(), HO.id.desc()).offset((page - 1) * per_page).limit(per_page).all()

    return render_template(
        'documents/ho_list.html',
        hos=hos,
        search=search,
        status=status,
        is_storage=is_storage,
        page=page,
        total_pages=max(1, math.ceil(total / per_page))
    )


# ==========================================
# 6. ADMIN REVIEW & STATUS UPDATE
# ==========================================
@documents_bp.route('/review/<doc_type>/<int:doc_id>', methods=['POST'])
@menu_required(MENU_DOCUMENTS)
def review_doc(doc_type, doc_id):
    doc = fetch_doc(doc_type, doc_id)
    if not doc:
        flash('Document not found.', 'danger')
        return redirect(url_for('documents.list_docs'))

    success, status_msg, flash_category = DocumentService.review_doc(
        doc,
        request.form.get('action'),
        request.form.get('status'),
        approver_id=session.get('user_id'),
    )
    flash(status_msg, flash_category)
    # ACC from a list page returns to that list; the detail page returns to itself.
    return _safe_local_redirect(
        request.form.get('redirect_to'),
        url_for('documents.view_doc', doc_type=doc_type, doc_id=doc.id),
    )


# ==========================================
# 7. EDIT DOCUMENT
# ==========================================
@documents_bp.route('/edit/<doc_type>/<int:doc_id>', methods=['GET', 'POST'])
@menu_required(MENU_DOCUMENTS)
def edit_doc(doc_type, doc_id):
    doc = fetch_doc(doc_type, doc_id)
    if not doc:
        flash('Document not found.', 'danger')
        return redirect(url_for('documents.list_docs'))

    if doc_type.lower() == 'prj':
        # PRJ has its own edit flow (items, tasks, SO number).
        return redirect(url_for('documents.edit_prj', doc_id=doc.id), code=307 if request.method == 'POST' else 302)

    if doc.status in FINAL_STATUSES:
        flash(f'A document with status {doc.status} cannot be edited.', 'warning')
        return redirect(url_for('documents.view_doc', doc_type=doc_type, doc_id=doc.id))

    if request.method == 'GET':
        return redirect(url_for('documents.view_doc', doc_type=doc_type, doc_id=doc.id, edit='1'))

    complaint = (request.form.get('customer_complaint') or '').strip()
    task_desc = (request.form.get('task_description') or '').strip()

    if not complaint:
        flash('Failed: Customer Complaint is required and cannot be empty.', 'danger')
        return redirect(url_for('documents.view_doc', doc_type=doc_type, doc_id=doc.id, edit='1'))

    if not task_desc:
        flash('Failed: Task Description is required and cannot be empty.', 'danger')
        return redirect(url_for('documents.view_doc', doc_type=doc_type, doc_id=doc.id, edit='1'))

    success, err_msg = DocumentService.edit_doc(
        doc=doc,
        doc_type=doc_type,
        client_id=request.form.get('client_id', type=int),
        site_address=(request.form.get('site_address') or '').strip(),
        customer_complaint=complaint,
        task_description=task_desc,
        estimated_days=request.form.get('estimated_calendar_days'),
        reference=(request.form.get('reference') or request.form.get('xprj_reference') or '').strip(),
        prj_id_val=request.form.get('prj_id'),
        notes=(request.form.get('notes') or '').strip()
    )

    if not success:
        flash(err_msg, 'danger')
        return redirect(url_for('documents.view_doc', doc_type=doc_type, doc_id=doc.id, edit='1'))

    flash(f'Document {doc.display_no} updated.', 'success')
    return redirect(url_for('documents.view_doc', doc_type=doc_type, doc_id=doc.id))


@documents_bp.route('/import/upload', methods=['POST'])
@menu_required(MENU_DOCUMENTS)
def import_upload():
    if 'excel_file' not in request.files:
        flash('File not found.', 'danger')
        return redirect(url_for('documents.list_docs'))

    file = request.files['excel_file']
    if file.filename == '':
        flash('No file selected.', 'danger')
        return redirect(url_for('documents.list_docs'))

    if not file.filename.lower().endswith((".xlsx", ".xls")):
        flash('File format not supported. Please upload an .xlsx or .xls file.', 'danger')
        return redirect(url_for('documents.list_docs'))

    success, parse_result, error_msg = DocumentImportService.parse_excel(
        file, allow_new_clients=current_user().can_do('add_client_from_document'))
    if not success:
        flash(error_msg, 'danger')
        return redirect(url_for('documents.list_docs'))
        
    paths = get_runtime_paths(current_app.config["APP_ENV"])
    paths.staging_dir.mkdir(parents=True, exist_ok=True)
    
    session_id = str(uuid.uuid4())
    temp_file = paths.staging_dir / f"import_{session_id}.json"
    
    with open(temp_file, 'w', encoding='utf-8') as f:
        json.dump(parse_result.to_dict(), f)
        
    session['import_session_id'] = session_id
    return redirect(url_for('documents.import_preview'))

@documents_bp.route('/import/preview', methods=['GET'])
@menu_required(MENU_DOCUMENTS)
def import_preview():
    session_id = session.get('import_session_id')
    if not session_id:
        flash('The import session is invalid or has expired.', 'warning')
        return redirect(url_for('documents.list_docs'))

    paths = get_runtime_paths(current_app.config["APP_ENV"])
    temp_file = paths.staging_dir / f"import_{session_id}.json"

    if not temp_file.exists():
        flash('Import data not found. Please upload again.', 'danger')
        return redirect(url_for('documents.list_docs'))
        
    with open(temp_file, 'r', encoding='utf-8') as f:
        data = json.load(f)
        
    return render_template('documents/import_preview.html',
                           rows=data['rows'],
                           total_parsed=data['total_parsed'],
                           total_skipped=data['total_skipped'],
                           total_errors=data['total_errors'],
                           total_warnings=data['total_warnings'],
                           clients=picker_clients())

@documents_bp.route('/import/confirm', methods=['POST'])
@menu_required(MENU_DOCUMENTS)
def import_confirm():
    session_id = session.get('import_session_id')
    if not session_id:
        flash('The import session is invalid or has expired.', 'warning')
        return redirect(url_for('documents.list_docs'))

    paths = get_runtime_paths(current_app.config["APP_ENV"])
    temp_file = paths.staging_dir / f"import_{session_id}.json"

    if not temp_file.exists():
        flash('Import data not found.', 'danger')
        return redirect(url_for('documents.list_docs'))

    with open(temp_file, 'r', encoding='utf-8') as f:
        data = json.load(f)

    # Indices of the ticked rows (from the POST form)
    selected_indices_str = request.form.getlist('selected_rows')
    if not selected_indices_str:
        flash('No documents selected to import.', 'warning')
        return redirect(url_for('documents.import_preview'))

    selected_indices = {int(i) for i in selected_indices_str if str(i).strip().isdigit()}

    # Keep the selected rows
    confirmed_rows = [r for r in data['rows'] if r['row_index'] in selected_indices]

    # Override values with what the form sent (the preview allows inline edits).
    # A field sent blank is blanked; execute_import validates everything again and
    # recomputes doc_period from the edited date.  The client is not retyped:
    # client_choice holds the id of the picked client, or blank = the client from the Excel.
    for row in confirmed_rows:
        idx = str(row['row_index'])
        for field in ('address', 'reference', 'doc_date'):
            submitted = request.form.get(f"{field}_{idx}")
            if submitted is not None:
                row[field] = submitted.strip()
        row['client_choice'] = (request.form.get(f"client_choice_{idx}") or '').strip()

    result = DocumentImportService.execute_import(
        confirmed_rows, allow_new_clients=current_user().can_do('add_client_from_document'))

    try:
        os.remove(temp_file)
    except OSError:
        pass
    session.pop('import_session_id', None)

    for err in result.errors:
        flash(err, 'danger')

    msg = f'Imported {result.imported_count} documents.'
    if result.clients_created > 0:
        msg += f' ({result.clients_created} new clients created).'
    if result.skipped_count > 0:
        msg += f' {result.skipped_count} documents skipped because of errors.'
    flash(msg, 'success' if result.imported_count else 'warning')
    return redirect(url_for('documents.list_docs'))


# ==========================================
# 8. PRJ IMPORT (PRJ & HO recap)
# ==========================================
def _prj_import_staging_file():
    """Staged parse result of the current PRJ import, or None when the session has none."""
    session_id = session.get('prj_import_session_id')
    if not session_id:
        return None
    paths = get_runtime_paths(current_app.config["APP_ENV"])
    return paths.staging_dir / f"prj_import_{session_id}.json"


@documents_bp.route('/prj/import/upload', methods=['POST'])
@menu_required(MENU_DOCUMENTS)
def prj_import_upload():
    file = request.files.get('excel_file')
    if not file or file.filename == '':
        flash('No file selected.', 'danger')
        return redirect(url_for('documents.prj_list'))

    if not file.filename.lower().endswith((".xlsx", ".xls")):
        flash('File format not supported. Please upload an .xlsx or .xls file.', 'danger')
        return redirect(url_for('documents.prj_list'))

    success, parse_result, error_msg = PRJImportService.parse_excel(
        file, allow_new_clients=current_user().can_do('add_client_from_document'))
    if not success:
        flash(error_msg, 'danger')
        return redirect(url_for('documents.prj_list'))

    paths = get_runtime_paths(current_app.config["APP_ENV"])
    paths.staging_dir.mkdir(parents=True, exist_ok=True)

    session['prj_import_session_id'] = str(uuid.uuid4())
    with open(_prj_import_staging_file(), 'w', encoding='utf-8') as f:
        json.dump(parse_result, f)
    return redirect(url_for('documents.prj_import_preview'))


@documents_bp.route('/prj/import/preview', methods=['GET'])
@menu_required(MENU_DOCUMENTS)
def prj_import_preview():
    temp_file = _prj_import_staging_file()
    if not temp_file or not temp_file.exists():
        flash('The import session is invalid or has expired. Please upload again.', 'warning')
        return redirect(url_for('documents.prj_list'))

    with open(temp_file, 'r', encoding='utf-8') as f:
        data = json.load(f)

    return render_template('documents/prj_import_preview.html',
                           rows=data['rows'],
                           total_parsed=data['total_parsed'],
                           total_skipped=data['total_skipped'],
                           total_errors=data['total_errors'],
                           total_warnings=data['total_warnings'],
                           clients=picker_clients())


@documents_bp.route('/prj/import/confirm', methods=['POST'])
@menu_required(MENU_DOCUMENTS)
def prj_import_confirm():
    temp_file = _prj_import_staging_file()
    if not temp_file or not temp_file.exists():
        flash('The import session is invalid or has expired. Please upload again.', 'warning')
        return redirect(url_for('documents.prj_list'))

    with open(temp_file, 'r', encoding='utf-8') as f:
        data = json.load(f)

    selected_indices = {int(i) for i in request.form.getlist('selected_rows') if str(i).strip().isdigit()}
    if not selected_indices:
        flash('No PRJs selected to import.', 'warning')
        return redirect(url_for('documents.prj_import_preview'))

    # Rows with errors cannot be ticked in the preview; skip them here too.
    confirmed_rows = [r for r in data['rows'] if r['row_index'] in selected_indices and not r['validation_errors']]

    # Inline edits from the preview; execute_import re-validates every row.  The client is
    # picked, not typed: client_choice is an existing client's id, or blank = the recap's client.
    for row in confirmed_rows:
        idx = str(row['row_index'])
        for field in ('title', 'so_number', 'notes'):
            submitted = request.form.get(f"{field}_{idx}")
            if submitted is not None:
                row[field] = submitted.strip()
        row['client_choice'] = (request.form.get(f"client_choice_{idx}") or '').strip()
        # Items from the item editor, as JSON; execute_import checks them again (clean_items).
        submitted_items = request.form.get(f"items_{idx}")
        if submitted_items is not None:
            try:
                row['items'] = json.loads(submitted_items)
            except ValueError:
                row['items'] = None  # refused as an invalid item list

    result = PRJImportService.execute_import(
        confirmed_rows, allow_new_clients=current_user().can_do('add_client_from_document'))

    try:
        os.remove(temp_file)
    except OSError:
        pass
    session.pop('prj_import_session_id', None)

    for err in result.errors:
        flash(err, 'danger')

    msg = f'Imported {result.imported_count} PRJs with their HOs.'
    if result.clients_created > 0:
        msg += f' ({result.clients_created} new clients created).'
    if result.skipped_count > 0:
        msg += f' {result.skipped_count} PRJs skipped because of errors.'
    flash(msg, 'success' if result.imported_count else 'warning')
    return redirect(url_for('documents.prj_list'))
