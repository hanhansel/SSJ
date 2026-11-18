"""The result shape every engine returns, so an experiment can replace the stub as is.

The Auto-Suggest window in templates/scheduling/index.html reads `team` as a list
of {'id', 'name'}, shows `start_date` as text and books `len(work_dates)` days,
so dates are ISO strings ('2026-11-02') in the result.
"""
import time
from datetime import timedelta


def work_dates(start, days):
    """`days` consecutive calendar days from `start` (Sundays and holidays included)."""
    return [start + timedelta(days=n) for n in range(days)]


def scheduled_item(task, team, start, score=0.0, score_notes=()):
    """One suggestion. `team` is a list of technician dicts ({'id', 'name'})."""
    return {
        'project_id': task['id'],
        'doc_type': task['doc_type'],
        'doc_no': task['doc_no'],
        'client_name': task.get('client_name'),
        'is_continuation': task.get('is_continuation', False),
        'team': [{'id': tech['id'], 'name': tech['name']} for tech in team],
        'start_date': start.isoformat(),
        'work_dates': [day.isoformat() for day in work_dates(start, task['remaining_days'])],
        'score': round(float(score), 2),
        'score_notes': list(score_notes),
    }


def unscheduled_item(task, reason):
    return {
        'project_id': task['id'],
        'doc_type': task['doc_type'],
        'doc_no': task['doc_no'],
        'client_name': task.get('client_name'),
        'reason': reason,
    }


def engine_result(scheduled, unscheduled, engine_used, started_at, search_space_estimate=0):
    """The dict run_engine() returns. Pass started_at=time.perf_counter() from the start of the run."""
    return {
        'scheduled': scheduled,
        'unscheduled': unscheduled,
        'runtime_ms': round((time.perf_counter() - started_at) * 1000, 2),
        'search_space_estimate': search_space_estimate,
        'engine_used': engine_used,
    }
