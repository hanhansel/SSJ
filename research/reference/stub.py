"""The stub engine that public SSJ ships: it schedules nothing.

A copy of apps/scheduling/engine/pipeline.py, so the research code runs without
the app. Every task comes back unscheduled; it is the starting point that every
algorithm here has to beat.
"""

STUB_REASON = ('No scheduling engine is included in the public version of SSJ. '
               'See the scheduling-algorithms branch.')


def run_stub(tasks, technicians, start_date, holidays, history_ctx, existing_jobs=()) -> dict:
    """Same arguments and result shape as run_engine(); nothing is scheduled."""
    return {
        'scheduled': [],
        'unscheduled': [
            {
                'project_id': task['id'],
                'doc_type': task['doc_type'],
                'doc_no': task['doc_no'],
                'client_name': task.get('client_name'),
                'reason': STUB_REASON,
            }
            for task in tasks
        ],
        'runtime_ms': 0.0,
        'search_space_estimate': 0,
        'engine_used': 'none',
    }
