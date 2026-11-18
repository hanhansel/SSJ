"""Synthetic input for scheduling experiments.

generate() returns the keyword arguments of run_engine(), in exactly the shape
apps/scheduling/engine_input.py builds from the database:

    data = generate(seed=1)
    result = run_engine(**data)

Everything is random but reproducible from `seed`. No real company data is used:
names, clients and locations are made up, and locations are scattered around a
made-up centre point.
"""
import math
import random
from dataclasses import dataclass
from datetime import date, timedelta
from itertools import combinations

CENTER = (-6.2000, 106.8000)
KM_PER_DEGREE = 111.32

VISIT_TYPE = {'svc': 'SVC', 'xprj': 'XPRJ', 'prj': 'PRJ'}

TECHNICIAN_NAMES = [
    'Alex', 'Bima', 'Citra', 'Dimas', 'Eka', 'Fajar', 'Gita', 'Hadi', 'Indra', 'Joko',
    'Kiki', 'Lukas', 'Maya', 'Nanda', 'Oki', 'Putra', 'Rani', 'Sari', 'Tono', 'Wawan',
]
CLIENT_WORDS = [
    'Example', 'Sample', 'Demo', 'Northwind', 'Bluebird', 'Summit', 'Harbor', 'Cedar',
    'Maple', 'Orchid', 'Lotus', 'Atlas', 'Delta', 'Nova', 'Vista', 'Zenith',
]
CLIENT_KINDS = ['Store', 'Office', 'Mall', 'Bank Branch', 'Clinic', 'Warehouse', 'Hotel', 'School']


@dataclass(frozen=True)
class Scenario:
    """The knobs of a synthetic problem. The defaults are a mid-sized two-week backlog."""
    n_technicians: int = 10
    n_tasks: int = 25
    n_clients: int = 15
    radius_km: float = 25.0             # clients are scattered within this distance of CENTER
    missing_location_share: float = 0.05  # clients without coordinates (allowed by engine_input)
    type_mix: tuple = (('svc', 0.5), ('prj', 0.35), ('xprj', 0.15))
    urgent_share: float = 0.3           # share of SVC/PRJ that are urgent (XPRJ always rank 0)
    max_lapse_days: int = 30            # how long a document may have waited already
    continuation_share: float = 0.1     # share of PRJ that already have days on the calendar
    busy_share: float = 0.4             # share of technicians already on a job at the start
    yesterday_share: float = 0.3        # share of technicians with a one-day job the day before
    n_holidays: int = 2                 # public holidays (never on a Sunday) in the first 30 days
    history_documents: int = 120        # past documents used to build history_ctx


def _random_location(rng, radius_km):
    """A point within radius_km of CENTER, spread evenly over the disc."""
    distance = radius_km * math.sqrt(rng.random())
    angle = rng.uniform(0, 2 * math.pi)
    lat = CENTER[0] + distance * math.cos(angle) / KM_PER_DEGREE
    lng = CENTER[1] + distance * math.sin(angle) / (KM_PER_DEGREE * math.cos(math.radians(CENTER[0])))
    return (round(lat, 5), round(lng, 5))


def _clients(rng, scenario):
    names = set()
    while len(names) < scenario.n_clients:
        names.add(f'{rng.choice(CLIENT_WORDS)} {rng.choice(CLIENT_KINDS)}')
    clients = []
    for client_id, name in enumerate(sorted(names), start=1):
        location = None if rng.random() < scenario.missing_location_share else _random_location(rng, scenario.radius_km)
        clients.append({'id': client_id, 'name': name, 'location': location})
    return clients


def _technicians(scenario):
    names = TECHNICIAN_NAMES[:scenario.n_technicians]
    extra = range(len(names) + 1, scenario.n_technicians + 1)
    names += [f'Technician {n}' for n in extra]
    return [{'id': tech_id, 'name': name} for tech_id, name in enumerate(names, start=1)]


def _estimated_days(rng, doc_type):
    if doc_type == 'svc':
        return rng.choices([1, 2], weights=[4, 1])[0]
    if doc_type == 'xprj':
        return rng.randint(1, 3)
    return rng.randint(2, 10)


def _doc_no(doc_type, number, doc_date):
    yy, mm = doc_date.strftime('%y'), doc_date.strftime('%m')
    if doc_type == 'prj':
        return f'{number:03d}/PRJ/{yy}'
    return f'{number:03d}/{mm}/{VISIT_TYPE[doc_type]}/{yy}'


def _tasks(rng, scenario, clients, start_date):
    doc_types, shares = zip(*scenario.type_mix)
    counters = {doc_type: 0 for doc_type in doc_types}
    tasks = []
    for task_id in range(1, scenario.n_tasks + 1):
        doc_type = rng.choices(doc_types, weights=shares)[0]
        counters[doc_type] += 1
        client = rng.choice(clients)
        lapse = rng.randint(0, scenario.max_lapse_days)
        doc_date = start_date - timedelta(days=lapse)
        estimate = _estimated_days(rng, doc_type)

        if doc_type == 'xprj':
            rank = 0
        else:
            rank = 1 if rng.random() < scenario.urgent_share else 2

        already, earliest = 0, start_date
        if doc_type == 'prj' and estimate >= 2 and rng.random() < scenario.continuation_share:
            already = rng.randint(1, estimate - 1)
            last_day = start_date + timedelta(days=rng.randint(-1, 3))
            earliest = max(start_date, last_day + timedelta(days=1))

        tasks.append({
            'id': task_id,
            'doc_type': doc_type,
            'doc_no': _doc_no(doc_type, counters[doc_type], doc_date),
            'client_id': client['id'],
            'client_name': client['name'],
            'visit_type': VISIT_TYPE[doc_type],
            'location': client['location'],
            'priority_rank': rank,
            'initial_lapse_days': lapse,
            'estimated_days': estimate,
            'remaining_days': estimate - already,
            'is_continuation': already > 0,
            'earliest_start': earliest,
        })
    return tasks


def _existing_jobs(rng, scenario, technicians, clients, start_date):
    """Job blocks already on the calendar. Never overlapping for one technician."""
    jobs = []
    for tech in technicians:
        roll = rng.random()
        if roll < scenario.busy_share:
            # Already on a job that started up to five days ago.
            block_start = start_date - timedelta(days=rng.randint(0, 5))
            end = start_date + timedelta(days=rng.randint(0, 8))
            jobs.append({'tech_id': tech['id'], 'start_date': block_start,
                         'days_duration': (end - block_start).days + 1,
                         'location': rng.choice(clients)['location']})
        elif roll < scenario.busy_share + scenario.yesterday_share:
            # Worked somewhere yesterday only: this matters for the proximity bonus.
            jobs.append({'tech_id': tech['id'], 'start_date': start_date - timedelta(days=1),
                         'days_duration': 1, 'location': rng.choice(clients)['location']})
    return jobs


def _holidays(rng, scenario, start_date):
    weekdays = [start_date + timedelta(days=n) for n in range(30)
                if (start_date + timedelta(days=n)).weekday() < 6]
    return set(rng.sample(weekdays, min(scenario.n_holidays, len(weekdays))))


def _history(rng, scenario, technicians, clients):
    """Counters in the shape of engine_input.history_context().

    Every technician has a preferred partner, so some pairs have worked together
    much more often than others, as happens in real teams.
    """
    ids = [tech['id'] for tech in technicians]
    shuffled = ids[:]
    rng.shuffle(shuffled)
    partner = {}
    for a, b in zip(shuffled[::2], shuffled[1::2]):
        partner[a], partner[b] = b, a

    doc_types, shares = zip(*scenario.type_mix)
    visits, type_count, pairs = {}, {}, {}
    documents = scenario.history_documents if len(ids) >= 2 else 0   # a team needs two people
    for _ in range(documents):
        first = rng.choice(ids)
        if first in partner and rng.random() < 0.6:
            second = partner[first]
        else:
            second = rng.choice([tech_id for tech_id in ids if tech_id != first])
        team = sorted((first, second))
        client = rng.choice(clients)
        label = VISIT_TYPE[rng.choices(doc_types, weights=shares)[0]]
        for tech_id in team:
            visits[(tech_id, client['id'])] = visits.get((tech_id, client['id']), 0) + 1
            type_count[(tech_id, label)] = type_count.get((tech_id, label), 0) + 1
        for pair in combinations(team, 2):
            pairs[pair] = pairs.get(pair, 0) + 1
    return {
        'tech_client_visits': visits,       # (tech_id, client_id) -> documents
        'tech_visit_type_count': type_count,  # (tech_id, 'SVC'|'XPRJ'|'PRJ') -> documents
        'tech_pair_cowork': pairs,          # (smaller tech_id, larger tech_id) -> documents together
    }


def generate(seed=0, start_date=date(2026, 11, 2), scenario=Scenario()):
    """Keyword arguments for run_engine(): tasks, technicians, start_date, holidays,
    history_ctx and existing_jobs, all synthetic and reproducible from `seed`."""
    rng = random.Random(seed)
    clients = _clients(rng, scenario)
    technicians = _technicians(scenario)
    return {
        'tasks': _tasks(rng, scenario, clients, start_date),
        'technicians': technicians,
        'start_date': start_date,
        'holidays': _holidays(rng, scenario, start_date),
        'history_ctx': _history(rng, scenario, technicians, clients),
        'existing_jobs': _existing_jobs(rng, scenario, technicians, clients, start_date),
    }
