"""Reference simulated annealing (notebook 04).

Like hill climbing, but a worse move is sometimes kept: with probability
e^(delta / T), where delta < 0 is how much worse it is and T is the temperature.
T starts high (accept many worse moves, explore widely) and cools towards
`end_temperature` (accept almost none, behave like hill climbing). The best
schedule ever seen is kept, because the current one can get worse.
"""
import math
import random
import statistics
import time
from collections import Counter

from research.local_search import Schedule
from research.reference.greedy import run_greedy
from research.reference.hill_climbing import MOVE_WEIGHTS, MOVES

START_ACCEPT_PROBABILITY = 0.5   # a typical worse move is accepted this often at the start
END_TEMPERATURE = 1.0            # at T = 1, losing even 5 points is accepted under 1% of the time
ITERATIONS = 10_000
TIME_LIMIT_S = None              # set a number of seconds to cool over time instead of over tries


def sa_accept(current, candidate, temperature, rng):
    """Keep a move that is at least as good; keep a worse one with probability e^(delta / T)."""
    if candidate >= current:
        return True
    if temperature <= 0:
        return False
    return rng.random() < math.exp((candidate - current) / temperature)


def cool(temperature, alpha):
    """Geometric cooling: the temperature shrinks by the same factor after every try."""
    return temperature * alpha


def estimate_start_temperature(data, start_result, moves=None, samples=200,
                               accept_probability=START_ACCEPT_PROBABILITY, seed=0):
    """Measure typical losses of random moves, then pick T so such a loss is accepted
    with `accept_probability`:  e^(-loss / T) = p  ->  T = loss / ln(1 / p)."""
    moves = moves or MOVES
    rng = random.Random(seed)
    schedule = Schedule(data, start_result)
    base = schedule.total()
    names, losses = list(moves), []
    for _ in range(samples * 20):
        change = moves[rng.choice(names)](schedule, rng)
        if change is None or not schedule.apply(change):
            continue
        delta = schedule.total() - base
        schedule.undo(change)
        if delta < 0:
            losses.append(-delta)
            if len(losses) >= samples:
                break
    if not losses:
        return END_TEMPERATURE
    return statistics.median(losses) / math.log(1 / accept_probability)


def simulated_annealing(data, start_result, moves=None, move_weights=None, iterations=ITERATIONS,
                        start_temperature=None, end_temperature=END_TEMPERATURE,
                        time_limit_s=TIME_LIMIT_S, seed=0):
    """Improve start_result. Returns (best schedule, trace, accepted moves by name).

    trace has one entry per try: 'current' and 'best' score, 'temperature', and
    'worse' (None when the move wasn't worse, 1 when a worse move was kept, 0 when undone).
    """
    moves = moves or MOVES
    move_weights = move_weights or MOVE_WEIGHTS
    names = list(moves)
    weights = [move_weights.get(name, 1) for name in names]
    rng = random.Random(seed)

    schedule = Schedule(data, start_result)
    current = schedule.total()
    best, best_snapshot = current, schedule.snapshot()

    t0 = start_temperature or estimate_start_temperature(data, start_result, moves, seed=seed)
    end_temperature = min(end_temperature, t0)     # never "cool" upwards
    alpha = (end_temperature / t0) ** (1 / iterations)
    temperature = t0
    trace = {'current': [], 'best': [], 'temperature': [], 'worse': []}
    accepted = Counter()
    started = time.perf_counter()
    step = 0

    while True:
        if time_limit_s is not None:
            fraction = (time.perf_counter() - started) / time_limit_s
            if fraction >= 1:
                break
            temperature = t0 * (end_temperature / t0) ** fraction
        elif step >= iterations:
            break

        name = rng.choices(names, weights=weights)[0]
        change = moves[name](schedule, rng)
        worse = None
        if change is not None and schedule.apply(change):
            candidate = schedule.total()
            if candidate < current:
                worse = 0
            if sa_accept(current, candidate, temperature, rng):
                if candidate < current:
                    worse = 1
                current = candidate
                accepted[name] += 1
                if current > best:
                    best, best_snapshot = current, schedule.snapshot()
            else:
                schedule.undo(change)

        trace['current'].append(current)
        trace['best'].append(best)
        trace['temperature'].append(temperature)
        trace['worse'].append(worse)
        step += 1
        if time_limit_s is None:
            temperature = cool(temperature, alpha)

    schedule.restore(best_snapshot)
    return schedule, trace, accepted


def run_simulated_annealing(tasks, technicians, start_date, holidays, history_ctx, existing_jobs=(),
                            seed=0, **options):
    """Same arguments and result as run_engine(): greedy first, then simulated annealing."""
    started = time.perf_counter()
    data = {'tasks': tasks, 'technicians': technicians, 'start_date': start_date, 'holidays': holidays,
            'history_ctx': history_ctx, 'existing_jobs': existing_jobs}
    schedule, _trace, _accepted = simulated_annealing(data, run_greedy(**data), seed=seed, **options)
    return schedule.to_result('simulated_annealing', started)
