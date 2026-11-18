"""Reference tabu search (notebook 05).

Every step looks at a sample of possible moves and makes the BEST one, even when
it is worse than the current schedule. To avoid walking straight back, a task
that was just moved becomes "tabu" (may not be moved) for `tenure` steps, unless
moving it would beat the best schedule ever seen (the aspiration rule).
"""
import random
import time
from collections import Counter

from research.local_search import Schedule
from research.reference.greedy import run_greedy
from research.reference.hill_climbing import MOVE_WEIGHTS, MOVES

NEIGHBOURS_PER_STEP = 20     # moves sampled and scored at every step
TABU_TENURE = 7              # steps a moved task stays tabu
TIME_LIMIT_S = 1.0


def tabu_search(data, start_result, moves=None, move_weights=None, neighbours=NEIGHBOURS_PER_STEP,
                tenure=TABU_TENURE, time_limit_s=TIME_LIMIT_S, max_steps=None, seed=0):
    """Improve start_result. Returns (best schedule, trace, moves made by name).

    Stops when time_limit_s has passed or max_steps steps were made (whichever is set
    and comes first). trace has one entry per step: 'current', 'best', 'moved_task'
    (None when every sampled move was tabu or invalid), 'went_down' and 'aspiration'.
    """
    moves = moves or MOVES
    move_weights = move_weights or MOVE_WEIGHTS
    names = list(moves)
    weights = [move_weights.get(name, 1) for name in names]
    rng = random.Random(seed)

    schedule = Schedule(data, start_result)
    current = schedule.total()
    best, best_snapshot = current, schedule.snapshot()
    tabu_until = {}                 # task_id -> last step on which it is still tabu
    trace = {'current': [], 'best': [], 'moved_task': [], 'went_down': [], 'aspiration': []}
    made = Counter()
    started = time.perf_counter()
    step = 0

    while True:
        if time_limit_s is not None and time.perf_counter() - started >= time_limit_s:
            break
        if max_steps is not None and step >= max_steps:
            break

        chosen = None               # (score, change, name, aspiration)
        for _ in range(neighbours):
            name = rng.choices(names, weights=weights)[0]
            change = moves[name](schedule, rng)
            if change is None or not schedule.apply(change):
                continue
            candidate = schedule.total()
            schedule.undo(change)
            is_tabu = tabu_until.get(change.task_id, -1) >= step
            aspiration = is_tabu and candidate > best
            if is_tabu and not aspiration:
                continue
            if chosen is None or candidate > chosen[0]:
                chosen = (candidate, change, name, aspiration)

        moved_task, went_down, aspiration = None, False, False
        if chosen is not None and schedule.apply(chosen[1]):
            candidate, change, name, aspiration = chosen
            went_down = candidate < current
            current = candidate
            tabu_until[change.task_id] = step + tenure
            moved_task = change.task_id
            made[name] += 1
            if current > best:
                best, best_snapshot = current, schedule.snapshot()

        trace['current'].append(current)
        trace['best'].append(best)
        trace['moved_task'].append(moved_task)
        trace['went_down'].append(went_down)
        trace['aspiration'].append(aspiration)
        step += 1

    schedule.restore(best_snapshot)
    return schedule, trace, made


def run_tabu_search(tasks, technicians, start_date, holidays, history_ctx, existing_jobs=(), seed=0, **options):
    """Same arguments and result as run_engine(): greedy first, then tabu search."""
    started = time.perf_counter()
    data = {'tasks': tasks, 'technicians': technicians, 'start_date': start_date, 'holidays': holidays,
            'history_ctx': history_ctx, 'existing_jobs': existing_jobs}
    schedule, _trace, _made = tabu_search(data, run_greedy(**data), seed=seed, **options)
    return schedule.to_result('tabu_search', started)
