"""Simulated annealing: the accept rule, the start temperature, and the reference run."""
import math
import random
import unittest

from research.evaluate import score, validate
from research.local_search import Schedule, replace_move
from research.reference.greedy import run_greedy
from research.reference.simulated_annealing import (
    estimate_start_temperature, run_simulated_annealing, sa_accept, simulated_annealing,
)
from research.synthetic import generate


class AcceptRuleTests(unittest.TestCase):
    def test_better_or_equal_is_always_kept(self):
        rng = random.Random(0)
        self.assertTrue(sa_accept(100, 120, 1.0, rng))
        self.assertTrue(sa_accept(100, 100, 1.0, rng))

    def test_worse_is_never_kept_at_zero_temperature(self):
        self.assertFalse(sa_accept(100, 99, 0, random.Random(0)))

    def test_worse_is_kept_about_e_to_the_delta_over_t(self):
        rng = random.Random(1)
        kept = sum(sa_accept(100, 90, 10.0, rng) for _ in range(20000)) / 20000
        self.assertAlmostEqual(kept, math.exp(-1), delta=0.02)


class AnnealingTests(unittest.TestCase):
    def test_snapshot_and_restore(self):
        data = generate(seed=1)
        schedule = Schedule(data, run_greedy(**data))
        saved, total = schedule.snapshot(), schedule.total()
        rng = random.Random(0)
        for _ in range(100):
            change = replace_move(schedule, rng)
            if change is not None:
                schedule.apply(change)
        schedule.restore(saved)
        self.assertEqual(schedule.placements, saved)
        self.assertAlmostEqual(schedule.total(), total, places=1)

    def test_start_temperature_is_positive(self):
        data = generate(seed=1)
        self.assertGreater(estimate_start_temperature(data, run_greedy(**data)), 0)

    def test_valid_and_never_worse_than_greedy(self):
        for seed in range(3):
            data = generate(seed=seed)
            result = run_simulated_annealing(**data, iterations=1500)
            self.assertEqual(validate(result, data), [], f'seed {seed}')
            self.assertGreaterEqual(score(result, data).total, score(run_greedy(**data), data).total - 0.1)

    def test_best_only_goes_up_and_temperature_only_goes_down(self):
        data = generate(seed=2)
        _schedule, trace, _accepted = simulated_annealing(data, run_greedy(**data), iterations=1500)
        self.assertEqual(trace['best'], sorted(trace['best']))
        self.assertEqual(trace['temperature'], sorted(trace['temperature'], reverse=True))


if __name__ == '__main__':
    unittest.main()
