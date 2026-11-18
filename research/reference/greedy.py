"""Reference greedy scheduler (notebook 02).

Tasks are placed one at a time, most important first. Each task gets the best
(team, start day) among the first day any team is free and the following
`lookahead_days` days, and is booked for good before the next task is looked at.
"""
import time
from datetime import timedelta
from itertools import combinations

from research.contract import engine_result, scheduled_item, unscheduled_item
from research.evaluate import TEAM_SIZE
from research.schedule_tools import Calendar, candidate_score, score_notes, work_dates

LOOKAHEAD_DAYS = 7
MAX_SEARCH_DAYS = 365


def order_tasks(tasks):
    """XPRJ and urgent first (priority_rank), then continuations, then the longest waiting."""
    return sorted(tasks, key=lambda task: (task['priority_rank'], not task['is_continuation'],
                                           -task['initial_lapse_days'], task['id']))


def best_placement(task, data, calendar, lookahead_days=LOOKAHEAD_DAYS,
                   max_search_days=MAX_SEARCH_DAYS, team_size=TEAM_SIZE):
    """(points, team, start, parts) of the best placement, or None when no team is ever free.

    Also returns how many candidates were scored, as the last element.
    """
    best, first_free, tried = None, None, 0
    for offset in range(max_search_days):
        start = task['earliest_start'] + timedelta(days=offset)
        if first_free is not None and (start - first_free).days > lookahead_days:
            break
        dates = work_dates(start, task['remaining_days'])
        free = calendar.free_technicians(dates)
        if len(free) < team_size:
            continue
        if first_free is None:
            first_free = start
        for team in combinations(free, team_size):
            tried += 1
            points, parts = candidate_score(task, list(team), start, data, calendar)
            if best is None or points > best[0]:
                best = (points, list(team), start, parts)
    return (*best, tried) if best else None


def run_greedy(tasks, technicians, start_date, holidays, history_ctx, existing_jobs=(),
               lookahead_days=LOOKAHEAD_DAYS, max_search_days=MAX_SEARCH_DAYS, team_size=TEAM_SIZE):
    """Same arguments and result as run_engine()."""
    started = time.perf_counter()
    data = {'tasks': tasks, 'technicians': technicians, 'start_date': start_date, 'holidays': holidays,
            'history_ctx': history_ctx, 'existing_jobs': existing_jobs}
    calendar = Calendar(data)
    scheduled, unscheduled, tried_total = [], [], 0

    for task in order_tasks(tasks):
        if task['remaining_days'] < 1:
            unscheduled.append(unscheduled_item(task, 'The estimated days of this document are already fully scheduled.'))
            continue
        if len(technicians) < team_size:
            unscheduled.append(unscheduled_item(task, f'Needs at least {team_size} active technicians.'))
            continue
        found = best_placement(task, data, calendar, lookahead_days, max_search_days, team_size)
        if found is None:
            unscheduled.append(unscheduled_item(
                task, f'No {team_size} technicians are free for {task["remaining_days"]} days in a row '
                      f'within the next {max_search_days} days.'))
            continue
        points, team, start, parts, tried = found
        tried_total += tried
        calendar.book(team, work_dates(start, task['remaining_days']), task['location'])
        scheduled.append(scheduled_item(task, team, start, points, score_notes(parts)))

    return engine_result(scheduled, unscheduled, 'greedy', started, search_space_estimate=tried_total)
