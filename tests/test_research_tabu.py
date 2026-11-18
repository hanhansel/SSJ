"""Tabu search: valid results, best-so-far, and the tabu rule itself."""
import unittest

from research.evaluate import score, validate
from research.reference.greedy import run_greedy
from research.reference.tabu_search import run_tabu_search, tabu_search
from research.synthetic import generate


class TabuSearchTests(unittest.TestCase):
    def test_valid_and_never_worse_than_greedy(self):
        for seed in range(3):
            data = generate(seed=seed)
            result = run_tabu_search(**data, time_limit_s=None, max_steps=40)
            self.assertEqual(validate(result, data), [], f'seed {seed}')
            self.assertGreaterEqual(score(result, data).total, score(run_greedy(**data), data).total - 0.1)

    def test_best_only_goes_up(self):
        data = generate(seed=2)
        _schedule, trace, _made = tabu_search(data, run_greedy(**data), time_limit_s=None, max_steps=60)
        self.assertEqual(trace['best'], sorted(trace['best']))

    def test_a_moved_task_is_left_alone_for_tenure_steps_unless_it_beats_the_best(self):
        data = generate(seed=4)
        tenure = 5
        _schedule, trace, _made = tabu_search(data, run_greedy(**data), tenure=tenure,
                                              time_limit_s=None, max_steps=80)
        moved, aspiration = trace['moved_task'], trace['aspiration']
        for i, task_id in enumerate(moved):
            if task_id is None:
                continue
            for j in range(i + 1, min(i + 1 + tenure, len(moved))):
                if moved[j] == task_id:
                    self.assertTrue(aspiration[j], f'task {task_id} moved again at step {j} while tabu')

    def test_it_does_take_worse_moves(self):
        data = generate(seed=1)
        _schedule, trace, _made = tabu_search(data, run_greedy(**data), time_limit_s=None, max_steps=200)
        self.assertTrue(any(trace['went_down']), 'after the climb, the best available move must sometimes be worse')


if __name__ == '__main__':
    unittest.main()
