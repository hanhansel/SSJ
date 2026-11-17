from datetime import timedelta

from apps.models import (
    db, MasterProject, STATUS_ONLINE, STATUS_HOLD, STATUS_DONE, STATUS_VOID, FINAL_STATUSES,
)
from apps.services.document_service import DocumentService

KPI_GOOD = 'Good'
KPI_BAD = 'Bad'
KPI_NEED_DATA = 'Need Data Input'

PROJECT_STATUSES = (STATUS_ONLINE, STATUS_HOLD, STATUS_DONE)
# KPI filter of the Master Project pages: query value -> KPI status (None = no PRJ yet).
KPI_FILTERS = {'good': KPI_GOOD, 'bad': KPI_BAD, 'need': KPI_NEED_DATA, 'none': None}


def _doc_timeline(doc):
    """Estimated vs. realised days/dates of one PRJ or XPRJ.

    Realisation uses the same unique-calendar-day count shown on the document
    page (``scheduled_target_days``), spanning the first to the last block.
    """
    blocks = doc._blocks
    real_start = min((b.start_date for b in blocks), default=None)
    real_end = max((b.start_date + timedelta(days=max(0, b.days_duration - 1)) for b in blocks), default=None)
    est_days = doc.estimated_calendar_days or 0
    return {
        'id': doc.id,
        'number': doc.display_no,
        'status': doc.status,
        'est_days': est_days,
        'est_start': doc.date,
        'est_end': doc.date + timedelta(days=est_days - 1) if doc.date and est_days else None,
        'real_days': doc.scheduled_target_days or 0,
        'real_start': real_start,
        'real_end': real_end,
    }


class ProjectService:
    @staticmethod
    def get_unfinished_documents(project: MasterProject):
        """Return the project's PRJs that are still active (neither Completed nor Void).

        XPRJ documents follow their own lifecycle and never block the Master Project.
        """
        return [prj for prj in project.prjs if prj.status not in FINAL_STATUSES]

    @staticmethod
    def project_kpi(project: MasterProject):
        """KPI of a Master Project from its non-Void PRJs and their XPRJ children.

        Any document without schedule (0 realised days) -> Need Data Input;
        otherwise total realised > total estimated -> Bad, else Good.
        It follows the schedule live, so PRJs do not need to be Completed first.
        """
        prj_rows = []
        all_rows = []
        for prj in sorted(project.prjs, key=lambda s: (s.date, s.id)):
            if prj.status == STATUS_VOID:
                continue
            row = _doc_timeline(prj)
            row['children'] = [
                _doc_timeline(child)
                for child in sorted(prj.xprj_children, key=lambda c: (c.date, c.id))
                if child.status != STATUS_VOID
            ]
            prj_rows.append(row)
            all_rows.append(row)
            all_rows.extend(row['children'])

        total_est = sum(r['est_days'] for r in all_rows)
        total_real = sum(r['real_days'] for r in all_rows)
        if not all_rows:
            status = None
        elif any(r['real_days'] == 0 for r in all_rows):
            status = KPI_NEED_DATA
        elif total_real > total_est:
            status = KPI_BAD
        else:
            status = KPI_GOOD
        return {'status': status, 'total_est': total_est, 'total_real': total_real, 'prjs': prj_rows}

    @staticmethod
    def active_prj_summary(project: MasterProject):
        """JSON-safe list of the PRJs that keep the project from being closed."""
        return [
            {'number': prj.display_no, 'status': prj.status or STATUS_ONLINE}
            for prj in ProjectService.get_unfinished_documents(project)
        ]

    @staticmethod
    def upcoming_block_count(project: MasterProject) -> int:
        """Schedule blocks from today on of the project's active PRJs: what Hold removes."""
        return sum(len(DocumentService.blocks_from_today(prj))
                   for prj in ProjectService.get_unfinished_documents(project))

    @staticmethod
    def update_master_project(project: MasterProject, name: str, status: str, notes: str, work_scope: str = None):
        """Update the project.  It may only be closed once every PRJ in it is Completed or Void.

        Closing never changes a PRJ: Completed and Void are final, so each PRJ is
        finished on its own page first.  Hold never changes a PRJ's status either;
        it removes the active PRJs' schedule from today on, and while the project is
        on Hold its PRJs stay out of the scheduler and the technician app.
        Returns: (success: bool, error_message: str)
        """
        if not name or status not in PROJECT_STATUSES:
            return False, 'Invalid Master Project data.'

        if status == STATUS_DONE and project.status != STATUS_DONE:
            unfinished = ProjectService.get_unfinished_documents(project)
            if unfinished:
                numbers = ', '.join(prj.display_no for prj in unfinished)
                return False, (
                    f'The Master Project cannot be closed yet because it still has active PRJs: {numbers}. '
                    'Complete or void those PRJs first.'
                )

        if status == STATUS_HOLD and project.status != STATUS_HOLD:
            for prj in ProjectService.get_unfinished_documents(project):
                DocumentService.clear_schedule_from_today(prj)

        project.name = name
        project.notes = notes
        if work_scope is not None:
            project.work_scope = work_scope
        project.status = status
        db.session.commit()
        return True, ''
