"""Build the scheduling engine's input from the database.

The engine (apps/scheduling/engine) never touches the database: everything it
knows comes from build_engine_input(), as plain dicts, sets and dates.
"""
from collections import Counter, defaultdict
from datetime import timedelta
from itertools import combinations

from apps.core.registry import get_doc_label
from apps.models import (
    db, ClientSite, Document, Role, ScheduleBlock, SystemHoliday, User,
    FINAL_STATUSES, ROLE_TECHNICIAN, wib_now,
)

# Longest allowed schedule block (~5 months). Calendar queries look back this far
# for blocks that started before the window, so no block can be longer.
MAX_BLOCK_DAYS = 150


def document_urgency(doc):
    """'urgent' for an SVC of a Contract client or an PRJ marked Urgent, else 'normal'."""
    if doc.document_type == 'svc':
        return 'urgent' if doc.client and doc.client.is_contract else 'normal'
    if doc.document_type == 'prj':
        return 'urgent' if doc.urgency_level == 2 else 'normal'
    return 'normal'


def priority_rank(doc_type, urgency):
    """Business priority shared by the backlog and the engine: XPRJ (0), urgent (1), normal (2)."""
    if doc_type == 'xprj':
        return 0
    return 1 if urgency == 'urgent' else 2


def client_location(client):
    """(lat, lng) of a client, or None when it has no coordinates."""
    if client is None or client.latitude is None or client.longitude is None:
        return None
    return (client.latitude, client.longitude)


def build_task(doc, start_date):
    """One selected document as the engine sees it.

    remaining_days is the estimate minus the days already on the calendar; a document
    that already has days continues the day after its last scheduled day.
    """
    estimate = doc.estimated_calendar_days or 1
    already = doc.scheduled_target_days or 0
    last_day = doc.scheduled_end_date
    rank = priority_rank(doc.document_type, document_urgency(doc))
    return {
        'id': doc.id,
        'doc_type': doc.document_type,             # 'svc' | 'xprj' | 'prj'
        'doc_no': doc.display_no,
        'client_id': doc.client_id,
        'client_name': doc.client.company_name if doc.client else '',
        'visit_type': get_doc_label(doc.document_type),  # 'SVC' | 'XPRJ' | 'PRJ'
        'location': client_location(doc.client),
        'priority_rank': rank,
        'initial_lapse_days': max(0, (wib_now().date() - (doc.date or start_date)).days),
        'estimated_days': estimate,
        'remaining_days': min(max(0, estimate - already), MAX_BLOCK_DAYS),
        'is_continuation': already > 0,
        'earliest_start': max(start_date, last_day + timedelta(days=1)) if last_day else start_date,
    }


def active_technicians():
    return [
        {'id': tech.id, 'name': tech.name}
        for tech in User.query.join(Role)
        .filter(Role.code == ROLE_TECHNICIAN, User.is_active.is_(True))
        .order_by(User.id).all()
    ]


def existing_jobs(start_date):
    """Job blocks on the calendar from the day before start_date on.

    Every job block counts except those of Completed/Void documents (finished work
    frees the technician).  The day before start_date only tells where each
    technician was, for the proximity bonus.  Leave blocks are not sent:
    drop the document_id filter to make leave block technicians too.
    """
    first_day = start_date - timedelta(days=1)
    rows = (
        db.session.query(ScheduleBlock.user_id, ScheduleBlock.start_date, ScheduleBlock.days_duration,
                         ClientSite.latitude, ClientSite.longitude)
        .join(Document, ScheduleBlock.document_id == Document.id)
        .join(ClientSite, Document.client_id == ClientSite.id)
        .filter(ScheduleBlock.document_id.isnot(None),
                Document.status.notin_(FINAL_STATUSES),
                ScheduleBlock.start_date >= first_day - timedelta(days=MAX_BLOCK_DAYS))
        .all()
    )
    return [
        {'tech_id': user_id, 'start_date': block_start, 'days_duration': days,
         'location': (lat, lng) if lat is not None and lng is not None else None}
        for user_id, block_start, days, lat, lng in rows
        if block_start + timedelta(days=days - 1) >= first_day
    ]


def history_context():
    """Counters from every job block on record (one count per document, not per block)."""
    rows = (
        db.session.query(ScheduleBlock.user_id, Document.id, Document.document_type, Document.client_id)
        .join(Document, ScheduleBlock.document_id == Document.id)
        .distinct().all()
    )
    visits, type_count, pairs = Counter(), Counter(), Counter()
    techs_by_doc = defaultdict(set)
    for user_id, doc_id, doc_type, client_id in rows:
        visits[(user_id, client_id)] += 1
        type_count[(user_id, get_doc_label(doc_type))] += 1
        techs_by_doc[doc_id].add(user_id)
    for techs in techs_by_doc.values():
        for pair in combinations(sorted(techs), 2):
            pairs[pair] += 1
    return {
        'tech_client_visits': dict(visits),       # (tech_id, client_id) -> documents
        'tech_visit_type_count': dict(type_count),  # (tech_id, 'SVC'|'XPRJ'|'PRJ') -> documents
        'tech_pair_cowork': dict(pairs),          # (smaller tech_id, larger tech_id) -> documents together
    }


def build_engine_input(start_date, docs):
    """Keyword arguments for run_engine(): everything the engine needs, from the database."""
    return {
        'tasks': [build_task(doc, start_date) for doc in docs],
        'technicians': active_technicians(),
        'start_date': start_date,
        'holidays': {holiday.date for holiday in SystemHoliday.query.filter(SystemHoliday.date >= start_date)},
        'history_ctx': history_context(),
        'existing_jobs': existing_jobs(start_date),
    }
