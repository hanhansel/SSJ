"""The local-search Schedule, its moves, and the reference hill climbing."""
import random
import unittest
from datetime import timedelta

from research.evaluate import score, validate
from research.local_search import Change, Schedule, insert_move, replace_move
from research.reference.greedy import run_greedy
from research.reference.hill_climbing import hill_climb, run_hill_climbing, shift_move
from research.reference.stub import run_stub
from research.synthetic import generate


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.data = generate(seed=1)
        self.greedy = run_greedy(**self.data)
        self.schedule = Schedule(self.data, self.greedy)

    def test_round_trip_keeps_the_greedy_schedule_and_its_score(self):
        result = self.schedule.to_result('test', 0.0)
        self.assertEqual(validate(result, self.data), [])
        self.assertAlmostEqual(self.schedule.total(), score(self.greedy, self.data).total, places=1)
        self.assertAlmostEqual(score(result, self.data).total, score(self.greedy, self.data).total, places=1)

    def test_undo_restores_the_schedule(self):
        rng = random.Random(0)
        placements, total = dict(self.schedule.placements), self.schedule.total()
        for _ in range(50):
            change = replace_move(self.schedule, rng)
            if change is not None and self.schedule.apply(change):
                self.schedule.undo(change)
        self.assertEqual(dict(self.schedule.placements), placements)
        self.assertAlmostEqual(self.schedule.total(), total, places=1)

    def test_an_invalid_change_is_refused_and_changes_nothing(self):
        task_id, (team, start) = next(iter(self.schedule.placements.items()))
        too_early = self.schedule.tasks[task_id]['earliest_start'] - timedelta(days=1)
        before = dict(self.schedule.placements)
        self.assertFalse(self.schedule.apply(Change(task_id, (team, start), (team, too_early))))
        self.assertEqual(self.schedule.placements, before)

    def test_moves_never_produce_an_invalid_schedule(self):
        rng = random.Random(3)
        for _ in range(300):
            move = rng.choice([shift_move, replace_move, insert_move])
            change = move(self.schedule, rng)
            if change is not None:
                self.schedule.apply(change)
        self.assertEqual(validate(self.schedule.to_result('test', 0.0), self.data), [])

    def test_insert_fills_an_empty_schedule(self):
        schedule = Schedule(self.data, run_stub(**self.data))
        change = insert_move(schedule, random.Random(0))
        self.assertTrue(schedule.apply(change))
        self.assertEqual(len(schedule.placements), 1)


class ReferenceHillClimbingTests(unittest.TestCase):
    def test_never_worse_than_greedy_and_always_valid(self):
        for seed in range(5):
            data = generate(seed=seed)
            greedy_score = score(run_greedy(**data), data).total
            result = run_hill_climbing(**data, max_stall=300)
            self.assertEqual(validate(result, data), [], f'seed {seed}')
            self.assertGreaterEqual(score(result, data).total, greedy_score, f'seed {seed}')

    def test_history_only_goes_up(self):
        data = generate(seed=2)
        _schedule, history, _accepted = hill_climb(data, run_greedy(**data), max_stall=300)
        self.assertEqual(history, sorted(history))


if __name__ == '__main__':
    unittest.main()
