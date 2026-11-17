from datetime import date, timedelta
from typing import Tuple, Optional, List, Dict, Any

from apps.models import (
    db, Document, DocumentPRJ, DocumentSVC, DocumentXPRJ, MasterProject, ClientSite, PRJItem, PRJTechnicianTask,
    ScheduleBlock, WorkReport, wib_now,
    STATUS_ONLINE, STATUS_PENDING, STATUS_DONE, STATUS_VOID, STATUS_HOLD, FINAL_STATUSES, HO_APPROVED,
)
from apps.core.registry import (
    get_next_sequence_number, compute_doc_period, format_document_number, get_doc_label,
)

SETTABLE_STATUSES = (STATUS_ONLINE, STATUS_PENDING, STATUS_DONE, STATUS_VOID, STATUS_HOLD)
# Hold only pauses Online work; a held document is resumed (Online) or cancelled (Void).
STATUSES_AFTER_HOLD = (STATUS_ONLINE, STATUS_VOID)


def parse_positive_int(value, field_label, default=None):
    """Parse a form value into an int >= 1, raising a user-readable ValueError."""
    if value in (None, '') and default is not None:
        return default
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError(f'{field_label} must be a whole number.')
    if number < 1:
        raise ValueError(f'{field_label} must be at least 1.')
    return number


def resolve_parent_prj(prj_id_val, current_prj_id=None) -> Tuple[bool, Optional[int], str]:
    """Validate the optional parent PRJ of an XPRJ.

    An XPRJ is often extra work issued after its PRJ is done, so a Completed
    PRJ is a valid parent; only a Void PRJ is not.
    """
    if prj_id_val in (None, ''):
        return True, None, ''
    try:
        prj_id = int(prj_id_val)
    except (TypeError, ValueError):
        return False, None, 'Invalid parent PRJ.'
    if prj_id == current_prj_id:
        return True, prj_id, ''
    prj = db.session.get(DocumentPRJ, prj_id)
    if not prj:
        return False, None, 'Parent PRJ not found.'
    if prj.status == STATUS_VOID:
        return False, None, f'Parent PRJ "{prj.display_no}" is Void and cannot be chosen.'
    return True, prj_id, ''


class DocumentService:
    @staticmethod
    def generate_number_and_validate(doc_type: str, doc_date: date, custom_no: str) -> Tuple[bool, int, str]:
        """Return the document's sequence number: the next free one, or a custom one after it.

        Numbers are never reused (Void documents keep theirs), so a custom
        number must be greater than the highest number already issued in the period.
        """
        next_no = get_next_sequence_number(doc_type, doc_date)
        if custom_no:
            try:
                doc_no = int(custom_no)
            except ValueError:
                return False, 0, 'The document sequence number must be a whole number.'
            if doc_no < next_no:
                return False, 0, (
                    f'Sequence number {doc_no} cannot be used. A custom number must be greater than '
                    f'the last number in this period (at least {next_no}).'
                )
        else:
            doc_no = next_no

        doc_period = compute_doc_period(doc_date)
        sequence_types = ['svc', 'xprj'] if doc_type in ['svc', 'xprj'] else [doc_type]
        if Document.query.filter(
            Document.document_type.in_(sequence_types),
            Document.doc_no == doc_no,
            Document.doc_period == doc_period,
        ).first():
            return False, 0, f'Document number "{doc_no}" is already used in period {doc_period}.'

        return True, doc_no, ''

    @staticmethod
    def create_prj(
        client_id: int,
        title: str,
        so_number: str,
        doc_date: date,
        estimated_days: int,
        master_project_id: Optional[int],
        custom_no: str,
        urgency_level: str,
        notes: str,
        items_data: List[Dict[str, Any]],
        tasks_data: List[Dict[str, Any]],
        site_address: str = '',
    ) -> Tuple[bool, Optional[DocumentPRJ], str]:
        """
        Handles the business logic for creating a new PRJ document and its HO.
        Returns: (success: bool, created_prj: DocumentPRJ, error_message: str)
        """
        if not title or not so_number:
            return False, None, 'Title and SO Number are required.'

        client = db.session.get(ClientSite, client_id)
        if not client:
            return False, None, 'Client/Site is required.'

        if estimated_days < 1:
            return False, None, 'Estimated days must be at least 1.'

        project = db.session.get(MasterProject, master_project_id) if master_project_id else None
        if master_project_id and (not project or project.status != STATUS_ONLINE or project.client_id != client.id):
            return False, None, 'The chosen Master Project is not active or not valid for this client.'

        success, doc_no, err_msg = DocumentService.generate_number_and_validate('prj', doc_date, custom_no)
        if not success:
            return False, None, err_msg

        urgency = 2 if urgency_level == '2' else 1

        prj = DocumentPRJ(
            doc_no=doc_no,
            doc_period=compute_doc_period(doc_date),
            doc_number_text=format_document_number('prj', doc_no, doc_date),
            date=doc_date,
            client_id=client.id,
            title=title,
            so_number=so_number,
            site_address=site_address or client.address or '',
            master_project=project,
            urgency_level=urgency,
            estimated_calendar_days=estimated_days,
            notes=notes,
            status=STATUS_ONLINE
        )

        db.session.add(prj)
        db.session.flush()

        for item in items_data:
            prj.items.append(PRJItem(**item))

        for task in tasks_data:
            prj.technician_tasks.append(PRJTechnicianTask(**task))

        # Its HO is created on flush (see models.give_every_new_prj_its_ho).
        db.session.commit()
        return True, prj, ''

    @staticmethod
    def edit_prj(
        prj: DocumentPRJ,
        client_id: int,
        title: str,
        so_number: str,
        site_address: str,
        estimated_days: int,
        master_project_id: Optional[int],
        urgency_level: str,
        notes: str,
        items_data: List[Dict[str, Any]],
        tasks_data: List[Dict[str, Any]]
    ) -> Tuple[bool, str]:
        """
        Handles the business logic for editing an existing PRJ.
        Returns: (success: bool, error_message: str)
        """
        if not title or not so_number:
            return False, 'SO Number, title and client/site are required.'

        client = db.session.get(ClientSite, client_id)
        if not client:
            return False, 'Client/Site not found.'

        if estimated_days < 1:
            return False, 'Estimated days must be at least 1.'

        project = db.session.get(MasterProject, master_project_id) if master_project_id else None
        if master_project_id and (not project or project.client_id != client.id):
            return False, 'Master Project not found or not valid for this client.'
        # A PRJ moves only into an Online project; it may keep the one it is in (even on Hold).
        if project and project.status != STATUS_ONLINE and project.id != prj.master_project_id:
            return False, f'Master Project "{project.name}" is {project.status}; a PRJ can only be moved into an Online Master Project.'

        prj.title = title
        prj.so_number = so_number
        prj.client_id = client.id
        prj.site_address = site_address or client.address or ''
        prj.master_project = project
        prj.urgency_level = 2 if urgency_level == '2' else 1
        prj.estimated_calendar_days = estimated_days
        prj.notes = notes

        for existing_item in list(prj.items):
            db.session.delete(existing_item)
        for item in items_data:
            prj.items.append(PRJItem(**item))

        for task in list(prj.technician_tasks):
            db.session.delete(task)
        for task in tasks_data:
            prj.technician_tasks.append(PRJTechnicianTask(**task))

        db.session.commit()
        return True, ''

    @staticmethod
    def create_svc(
        client_id: int,
        doc_date: date,
        custom_no: str,
        customer_complaint: str,
        task_description: str,
        estimated_days: int,
        reference: str,
        site_address: str,
        notes: str
    ) -> Tuple[bool, Optional[DocumentSVC], str]:

        if not customer_complaint:
            return False, None, 'Failed: Customer Complaint is required.'
        if not task_description:
            return False, None, 'Failed: Task Description is required.'

        client = db.session.get(ClientSite, client_id)
        if not client:
            return False, None, 'Client/Site not found.'

        success, doc_no, err_msg = DocumentService.generate_number_and_validate('svc', doc_date, custom_no)
        if not success:
            return False, None, err_msg

        svc = DocumentSVC(
            doc_no=doc_no,
            doc_period=compute_doc_period(doc_date),
            doc_number_text=format_document_number('svc', doc_no, doc_date),
            reference=reference,
            date=doc_date,
            client_id=client.id,
            site_address=site_address or client.address or '',
            customer_complaint=customer_complaint,
            task_description=task_description,
            notes=notes,
            estimated_calendar_days=estimated_days,
            status=STATUS_ONLINE
        )
        db.session.add(svc)
        db.session.commit()
        return True, svc, ''

    @staticmethod
    def create_xprj(
        client_id: int,
        doc_date: date,
        custom_no: str,
        customer_complaint: str,
        task_description: str,
        estimated_days: int,
        prj_id_val: Optional[str],
        reference: str,
        site_address: str,
        notes: str
    ) -> Tuple[bool, Optional[DocumentXPRJ], str]:

        if not customer_complaint:
            return False, None, 'Failed: Customer Complaint is required.'
        if not task_description:
            return False, None, 'Failed: Task Description is required.'

        parent_ok, parent_prj_id, parent_err = resolve_parent_prj(prj_id_val)
        if not parent_ok:
            return False, None, parent_err

        # An XPRJ with a parent PRJ belongs to that PRJ's client.
        if parent_prj_id:
            client = db.session.get(DocumentPRJ, parent_prj_id).client
        else:
            client = db.session.get(ClientSite, client_id)
        if not client:
            return False, None, 'Client/Site not found.'

        success, doc_no, err_msg = DocumentService.generate_number_and_validate('xprj', doc_date, custom_no)
        if not success:
            return False, None, err_msg

        xprj = DocumentXPRJ(
            doc_no=doc_no,
            doc_period=compute_doc_period(doc_date),
            doc_number_text=format_document_number('xprj', doc_no, doc_date),
            date=doc_date,
            client_id=client.id,
            site_address=site_address or client.address or '',
            reference=reference,
            prj_id=parent_prj_id,
            customer_complaint=customer_complaint,
            task_description=task_description,
            notes=notes,
            estimated_calendar_days=estimated_days,
            status=STATUS_ONLINE
        )
        db.session.add(xprj)
        db.session.commit()
        return True, xprj, ''

    @staticmethod
    def edit_doc(
        doc: Document,
        doc_type: str,
        client_id: Optional[int],
        site_address: str,
        customer_complaint: str,
        task_description: str,
        estimated_days: str,
        reference: str,
        prj_id_val: Optional[str],
        notes: str
    ) -> Tuple[bool, str]:
        if not customer_complaint:
            return False, 'Failed: Customer Complaint is required and cannot be empty.'
        if not task_description:
            return False, 'Failed: Task Description is required and cannot be empty.'

        try:
            estimated = parse_positive_int(estimated_days, 'Estimated days', default=doc.estimated_calendar_days or 2)
        except ValueError as exc:
            return False, str(exc)

        parent_prj_id = None
        if doc_type.lower() == 'xprj':
            parent_ok, parent_prj_id, parent_err = resolve_parent_prj(prj_id_val, current_prj_id=doc.prj_id)
            if not parent_ok:
                return False, parent_err

        if client_id:
            client = db.session.get(ClientSite, client_id)
            if client:
                doc.client_id = client.id
                doc.site_address = site_address or client.address or ''
            else:
                return False, 'Client not found.'

        doc.customer_complaint = customer_complaint
        doc.task_description = task_description
        doc.notes = notes
        doc.estimated_calendar_days = estimated

        if doc_type.lower() == 'svc':
            doc.reference = reference
        elif doc_type.lower() == 'xprj':
            doc.reference = reference
            doc.prj_id = parent_prj_id

        db.session.commit()
        return True, ''

    # ------------------------------------------------------------------
    # Status workflow
    # ------------------------------------------------------------------
    @staticmethod
    def _pending_final_report(doc: Document) -> Optional[WorkReport]:
        return WorkReport.query.filter_by(
            document_id=doc.id, report_type='final', status='Pending',
        ).order_by(WorkReport.uploaded_at.desc()).first()

    @staticmethod
    def blocks_from_today(doc: Document) -> List[ScheduleBlock]:
        """Schedule blocks of the document that have not ended yet (today included)."""
        today = wib_now().date()
        return [
            block for block in ScheduleBlock.query.filter_by(document_id=doc.id).all()
            if block.start_date + timedelta(days=block.days_duration) > today
        ]

    @staticmethod
    def status_change_impacts(doc: Document, target: str) -> Dict[str, Any]:
        """JSON-safe summary for the confirmation popup shown before Completed/Void/Hold.

        Lists what is still missing (Completed only) and what changes with it (the
        HO of a PRJ, the schedule removed by Hold).  The Master Project is
        never changed here: it is closed on the Master Project page.
        XPRJ documents are independent and are never part of a PRJ's impacts.
        """
        is_prj = doc.document_type == 'prj'
        impacts = {
            'doc_label': get_doc_label(doc.document_type),
            'doc_no': doc.display_no,
            'target': target,
            'missing': [],
            'cascade': [],
        }
        if target == STATUS_DONE:
            report_types = {
                report_type for (report_type,) in
                db.session.query(WorkReport.report_type).filter_by(document_id=doc.id).distinct()
            }
            if 'daily' not in report_types:
                impacts['missing'].append('No daily report from a technician yet.')
            if 'final' not in report_types:
                impacts['missing'].append('The final report has not been uploaded yet.')
            if is_prj:
                if not doc.ho.photos:
                    impacts['missing'].append(f'The signed HO {doc.ho.display_no} photo has not been uploaded yet.')
                impacts['cascade'].append(f'HO {doc.ho.display_no} becomes Approved as well.')
        elif target == STATUS_VOID and is_prj:
            impacts['cascade'].append(f'HO {doc.ho.display_no} becomes Void as well.')
        elif target == STATUS_HOLD:
            upcoming = len(DocumentService.blocks_from_today(doc))
            if upcoming:
                impacts['cascade'].append(
                    f'{upcoming} technician schedule blocks from today on are deleted; past schedules are kept.')
            if is_prj:
                impacts['cascade'].append(f'HO {doc.ho.display_no} becomes Hold as well.')
        return impacts

    @staticmethod
    def _complete(doc: Document, approver_id: Optional[int]) -> None:
        now = wib_now()
        WorkReport.query.filter_by(document_id=doc.id, report_type='final', status='Pending').update(
            {'status': 'Approved'}, synchronize_session=False
        )
        doc.status = STATUS_DONE
        if hasattr(doc, 'actual_completion'):
            doc.actual_completion = now.date()
        if doc.document_type == 'prj':
            doc.ho.status = HO_APPROVED
            doc.ho.approved_at = now
            doc.ho.approved_by = approver_id
            doc.ho.completion_date = now.date()

    @staticmethod
    def _void(doc: Document) -> None:
        doc.status = STATUS_VOID
        if hasattr(doc, 'actual_completion') and not doc.actual_completion:
            doc.actual_completion = wib_now().date()
        ScheduleBlock.query.filter_by(document_id=doc.id).delete()
        if doc.document_type == 'prj':
            doc.ho.status = STATUS_VOID

    @staticmethod
    def clear_schedule_from_today(doc: Document) -> None:
        """Remove the document's schedule from today on; days already worked stay as history."""
        today = wib_now().date()
        for block in DocumentService.blocks_from_today(doc):
            if block.start_date >= today:
                db.session.delete(block)
            else:
                block.days_duration = (today - block.start_date).days

    @staticmethod
    def _hold(doc: Document) -> None:
        """Pause the document: its schedule from today on is removed, days already worked stay as history."""
        DocumentService.clear_schedule_from_today(doc)
        doc.status = STATUS_HOLD
        if doc.document_type == 'prj':
            doc.ho.status = STATUS_HOLD

    @staticmethod
    def review_doc(
        doc: Document, action: str, new_status: str,
        approver_id: Optional[int] = None,
    ) -> Tuple[bool, str, str]:
        """Apply an approval (``action='approve'``) or a status change.

        Completed and Void are final.  Approval requires the pending final report (and,
        for a PRJ, the HO photo); choosing Completed from the status dropdown is
        an override the admin confirmed in the popup.
        Returns: (success: bool, status_msg: str, flash_category: str)
        """
        if doc.status in FINAL_STATUSES:
            return False, f'Document {doc.display_no} is {doc.status}, which is final; its status can no longer change.', 'warning'

        is_prj = doc.document_type == 'prj'
        if action == 'approve':
            if not DocumentService._pending_final_report(doc):
                return False, 'The document has no final report awaiting approval.', 'warning'
            if is_prj and not doc.ho.photos:
                return False, 'The photo of the signed HO has not been uploaded yet. A PRJ cannot be approved without it.', 'warning'
            new_status = STATUS_DONE
        elif action:
            return False, 'Invalid action.', 'danger'

        if new_status not in SETTABLE_STATUSES:
            return False, 'Invalid document status.', 'danger'
        if new_status == doc.status:
            return True, f'The status of document {doc.display_no} did not change ({doc.status}).', 'info'
        if new_status == STATUS_HOLD and doc.status != STATUS_ONLINE:
            return False, f'Only Online documents can be put on Hold (current status: {doc.status}).', 'warning'
        if doc.status == STATUS_HOLD and new_status not in STATUSES_AFTER_HOLD:
            return False, 'A held document can only be set back to Online or voided.', 'warning'

        if new_status == STATUS_DONE:
            DocumentService._complete(doc, approver_id)
        elif new_status == STATUS_VOID:
            DocumentService._void(doc)
        elif new_status == STATUS_HOLD:
            DocumentService._hold(doc)
        else:
            doc.status = new_status
            if is_prj:
                doc.ho.status = new_status

        ho_suffix = ''
        if is_prj and new_status in FINAL_STATUSES + (STATUS_HOLD,):
            ho_suffix = f' HO {doc.ho.display_no} is now {doc.ho.status} as well.'

        db.session.commit()
        return True, f'Document {doc.display_no} is now "{doc.status}".{ho_suffix}', 'success'
