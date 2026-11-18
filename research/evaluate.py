"""Check and score a schedule the same way for every algorithm.

    problems = validate(result, data)   # [] means every hard rule holds
    s = score(result, data)             # s.total, s.parts, s.per_task
    m = summary(result, data)           # plain numbers for tables and charts

The score uses the weights and penalties of the original engine's config
(its cold-start values), with two changes so it can judge any algorithm alike:

- Waiting is counted from each task's earliest_start, not from "the first free
  slot of the chosen team", which depends on the algorithm's own choices.
- Leaving a task unscheduled costs UNSCHEDULED_PENALTY. Without it, an empty
  schedule would beat any schedule that has to use a Sunday.
"""
import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from itertools import combinations

TEAM_SIZE = 2   # DEFAULT_REQUIRED_TECHNICIANS in the original engine

WEIGHTS = {
    'priority_urgent': 50,
    'priority_normal': 10,
    'aging_per_day': 3,
    'aging_cap': 60,
    'familiarity_bonus': 20,          # per technician who has worked for this client before
    'experience_per_job': 5,          # per past job of the same type, per technician
    'experience_cap': 30,
    'chemistry_per_pair_per_job': 15,  # per past job the pair did together
    'chemistry_cap': 45,
    'proximity_bonus': 20,            # full at the same site yesterday, 0 at PROXIMITY_RADIUS_KM
}

SOFT = {
    'sunday_work_per_day': -100,      # per technician per Sunday inside the block
    'holiday_work_per_day': -100,     # per technician per holiday (not a Sunday) inside the block
    'saturday_start': -30,            # per technician
    'wait_per_day_normal': -40,       # per day after earliest_start
    'wait_per_day_urgent': -100,      # XPRJ and urgent documents cost more to delay
}

PROXIMITY_RADIUS_KM = 15
UNSCHEDULED_PENALTY = -500            # research addition, per task left unscheduled


def as_date(value):
    """A date from a date or an ISO string ('2026-11-02')."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


def days_from(start, count):
    return [start + timedelta(days=n) for n in range(count)]


def haversine_km(a, b):
    """Great-circle distance in km between two (lat, lng) points."""
    lat1, lng1, lat2, lng2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def is_urgent(task):
    """XPRJ (rank 0) and urgent documents (rank 1)."""
    return task['priority_rank'] <= 1


def _team_ids(item):
    return [member['id'] for member in item['team']]


def validate(result, data, team_size=TEAM_SIZE):
    """Every broken hard rule, as readable lines. An empty list means the schedule is valid.

    Hard rules: each task appears exactly once (scheduled or unscheduled); a team has
    `team_size` different, known technicians; work_dates are remaining_days consecutive
    days from start_date; nobody starts before earliest_start; and no technician
    works two jobs on one day, counting the existing jobs.
    """
    problems = []
    tasks = {task['id']: task for task in data['tasks']}
    known_techs = {tech['id'] for tech in data['technicians']}

    busy = defaultdict(set)
    for job in data.get('existing_jobs', ()):
        busy[job['tech_id']].update(days_from(job['start_date'], job['days_duration']))

    seen = set()
    for item in result['scheduled']:
        task_id = item['project_id']
        label = item.get('doc_no') or task_id
        task = tasks.get(task_id)
        if task is None:
            problems.append(f'{label}: is not one of the selected tasks')
            continue
        if task_id in seen:
            problems.append(f'{label}: appears more than once')
            continue
        seen.add(task_id)

        team = _team_ids(item)
        if len(team) != team_size or len(set(team)) != len(team):
            problems.append(f'{label}: needs {team_size} different technicians, got {team}')
        for tech_id in team:
            if tech_id not in known_techs:
                problems.append(f'{label}: technician {tech_id} is not active')

        start = as_date(item['start_date'])
        dates = [as_date(day) for day in item['work_dates']]
        if task['remaining_days'] < 1:
            problems.append(f'{label}: has no remaining days and must be unscheduled')
        elif dates != days_from(start, task['remaining_days']):
            problems.append(f'{label}: work_dates must be {task["remaining_days"]} consecutive days from start_date')
        if start < task['earliest_start']:
            problems.append(f'{label}: starts {start} before its earliest start {task["earliest_start"]}')

        for tech_id in set(team):
            clash = busy[tech_id].intersection(dates)
            if clash:
                problems.append(f'{label}: technician {tech_id} is already busy on {min(clash)}')
            busy[tech_id].update(dates)

    for item in result['unscheduled']:
        task_id = item['project_id']
        if task_id in seen:
            problems.append(f'{item.get("doc_no") or task_id}: is both scheduled and unscheduled, or listed twice')
        seen.add(task_id)

    for task_id in tasks.keys() - seen:
        problems.append(f'{tasks[task_id]["doc_no"]}: is missing from the result')
    return problems


@dataclass
class Score:
    total: float
    parts: Counter                      # part name -> points, over the whole schedule
    per_task: dict = field(default_factory=dict)  # task id -> Counter of parts


def _locations_by_day(result, data, tasks):
    """(tech_id, day) -> (lat, lng) or None, from existing jobs and the new schedule."""
    where = {}
    for job in data.get('existing_jobs', ()):
        for day in days_from(job['start_date'], job['days_duration']):
            where[(job['tech_id'], day)] = job['location']
    for item in result['scheduled']:
        location = tasks[item['project_id']]['location']
        for day in item['work_dates']:
            for tech_id in _team_ids(item):
                where[(tech_id, as_date(day))] = location
    return where


def score_item(item, task, data, where, weights=WEIGHTS, soft=SOFT):
    """The points of one scheduled task, by part."""
    history = data['history_ctx']
    team = _team_ids(item)
    start = as_date(item['start_date'])
    dates = [as_date(day) for day in item['work_dates']]
    urgent = is_urgent(task)
    parts = Counter()

    parts['priority'] = weights['priority_urgent'] if urgent else weights['priority_normal']
    parts['aging'] = min(weights['aging_cap'], weights['aging_per_day'] * task['initial_lapse_days'])

    for tech_id in team:
        if history['tech_client_visits'].get((tech_id, task['client_id']), 0):
            parts['familiarity'] += weights['familiarity_bonus']
        jobs = history['tech_visit_type_count'].get((tech_id, task['visit_type']), 0)
        parts['experience'] += min(weights['experience_cap'], weights['experience_per_job'] * jobs)
        yesterday = where.get((tech_id, start - timedelta(days=1)))
        if yesterday and task['location']:
            km = haversine_km(yesterday, task['location'])
            if km < PROXIMITY_RADIUS_KM:
                parts['proximity'] += weights['proximity_bonus'] * (1 - km / PROXIMITY_RADIUS_KM)

    for pair in combinations(sorted(team), 2):
        together = history['tech_pair_cowork'].get(pair, 0)
        parts['chemistry'] += min(weights['chemistry_cap'], weights['chemistry_per_pair_per_job'] * together)

    holidays = data['holidays']
    sundays = sum(day.weekday() == 6 for day in dates)
    holiday_days = sum(day in holidays and day.weekday() != 6 for day in dates)
    parts['sunday'] = soft['sunday_work_per_day'] * sundays * len(team)
    parts['holiday'] = soft['holiday_work_per_day'] * holiday_days * len(team)
    if start.weekday() == 5:
        parts['saturday_start'] = soft['saturday_start'] * len(team)
    wait = max(0, (start - task['earliest_start']).days)
    parts['wait'] = (soft['wait_per_day_urgent'] if urgent else soft['wait_per_day_normal']) * wait
    return parts


def score(result, data, weights=WEIGHTS, soft=SOFT, unscheduled_penalty=UNSCHEDULED_PENALTY):
    """Total points of a schedule (higher is better), with the parts that make it up.

    Score a schedule only after validate() returns no problems.
    """
    tasks = {task['id']: task for task in data['tasks']}
    where = _locations_by_day(result, data, tasks)
    parts, per_task = Counter(), {}
    for item in result['scheduled']:
        item_parts = score_item(item, tasks[item['project_id']], data, where, weights, soft)
        per_task[item['project_id']] = item_parts
        parts.update(item_parts)
    if result['unscheduled']:
        parts['unscheduled'] = unscheduled_penalty * len(result['unscheduled'])
    return Score(total=round(sum(parts.values()), 2), parts=parts, per_task=per_task)


def summary(result, data, horizon_days=14):
    """Plain numbers to compare algorithms: counts, waiting, weekend/holiday work, utilisation."""
    tasks = {task['id']: task for task in data['tasks']}
    start_date = data['start_date']
    horizon = set(days_from(start_date, horizon_days))
    waits, sunday_days, holiday_days, last_day = [], 0, 0, start_date
    worked = 0
    for item in result['scheduled']:
        task = tasks[item['project_id']]
        dates = [as_date(day) for day in item['work_dates']]
        team_size = len(item['team'])
        waits.append((as_date(item['start_date']) - task['earliest_start']).days)
        sunday_days += sum(day.weekday() == 6 for day in dates) * team_size
        holiday_days += sum(day in data['holidays'] and day.weekday() != 6 for day in dates) * team_size
        worked += sum(day in horizon for day in dates) * team_size
        last_day = max(last_day, dates[-1]) if dates else last_day
    capacity = len(data['technicians']) * horizon_days
    return {
        'scheduled': len(result['scheduled']),
        'unscheduled': len(result['unscheduled']),
        'total_wait_days': sum(waits),
        'mean_wait_days': round(sum(waits) / len(waits), 2) if waits else 0.0,
        'sunday_tech_days': sunday_days,
        'holiday_tech_days': holiday_days,
        'days_to_finish': (last_day - start_date).days + 1 if result['scheduled'] else 0,
        f'new_work_share_first_{horizon_days}_days': round(worked / capacity, 3) if capacity else 0.0,
        'runtime_ms': result.get('runtime_ms', 0.0),
    }
