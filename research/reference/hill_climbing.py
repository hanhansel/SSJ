"""Reference hill climbing (notebook 03).

Start from the greedy schedule. Repeatedly propose one random small change; keep
it only if the score goes up, otherwise undo it. Stop after `max_stall` tries in a
row without an improvement.
"""
import random
import time
from collections import Counter
from datetime import timedelta

from research.local_search import Change, Schedule, insert_move, random_placed_task, replace_move
from research.reference.greedy import run_greedy

MAX_SHIFT_DAYS = 3
MAX_STALL = 2000
MAX_ITERATIONS = 50_000


def shift_move(schedule, rng, max_shift=MAX_SHIFT_DAYS):
    """Move a random task 1 to max_shift days earlier or later, with the same team."""
    task_id = random_placed_task(schedule, rng)
    if task_id is None:
        return None
    team, start = schedule.placements[task_id]
    days = rng.choice([n for n in range(-max_shift, max_shift + 1) if n != 0])
    return Change(task_id, (team, start), (team, start + timedelta(days=days)))


def accept_if_better(current, candidate):
    """Hill climbing's rule: only ever go uphill."""
    return candidate > current


MOVES = {'shift': shift_move, 'replace': replace_move, 'insert': insert_move}
MOVE_WEIGHTS = {'shift': 4, 'replace': 4, 'insert': 1}


def hill_climb(data, start_result, moves=None, move_weights=None, accept=accept_if_better,
               max_stall=MAX_STALL, max_iterations=MAX_ITERATIONS, seed=0):
    """Improve start_result. Returns (schedule, score after every try, accepted moves by name)."""
    moves = moves or MOVES
    move_weights = move_weights or MOVE_WEIGHTS
    names = list(moves)
    weights = [move_weights.get(name, 1) for name in names]
    rng = random.Random(seed)

    schedule = Schedule(data, start_result)
    current = schedule.total()
    history, accepted, stall = [current], Counter(), 0

    for _ in range(max_iterations):
        name = rng.choices(names, weights=weights)[0]
        change = moves[name](schedule, rng)
        if change is not None and schedule.apply(change):
            candidate = schedule.total()
            if accept(current, candidate):
                stall = 0 if candidate > current else stall + 1
                current = candidate
                accepted[name] += 1
            else:
                schedule.undo(change)
                stall += 1
        else:
            stall += 1
        history.append(current)
        if stall >= max_stall:
            break
    return schedule, history, accepted


def run_hill_climbing(tasks, technicians, start_date, holidays, history_ctx, existing_jobs=(), seed=0, **options):
    """Same arguments and result as run_engine(): greedy first, then hill climbing."""
    started = time.perf_counter()
    data = {'tasks': tasks, 'technicians': technicians, 'start_date': start_date, 'holidays': holidays,
            'history_ctx': history_ctx, 'existing_jobs': existing_jobs}
    schedule, _history, _accepted = hill_climb(data, run_greedy(**data), seed=seed, **options)
    return schedule.to_result('hill_climbing', started)
