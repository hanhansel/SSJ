"""Scheduling engine: not included in the public version of SSJ.

The Auto-Suggest button calls run_engine() with the input that
apps/scheduling/engine_input.py builds from the database. This stub keeps the
same signature and result shape but schedules nothing: every selected document
comes back as not scheduled, with a reason the Auto-Suggest window shows.

Scheduling algorithms are developed on the `scheduling-algorithms` branch;
see the README.
"""

STUB_REASON = ('No scheduling engine is included in the public version of SSJ. '
               'See the scheduling-algorithms branch.')


def run_engine(tasks, technicians, start_date, holidays, history_ctx, existing_jobs=()) -> dict:
    """Returns {'scheduled', 'unscheduled', 'runtime_ms', 'search_space_estimate', 'engine_used'}."""
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
