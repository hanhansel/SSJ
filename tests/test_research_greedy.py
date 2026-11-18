"""The shared calendar and the reference greedy scheduler."""
import unittest
from datetime import date

from research.evaluate import UNSCHEDULED_PENALTY, score, validate
from research.reference.greedy import order_tasks, run_greedy
from research.schedule_tools import Calendar, score_notes
from research.synthetic import Scenario, generate

MONDAY = date(2026, 11, 2)


class CalendarTests(unittest.TestCase):
    def test_existing_jobs_make_technicians_busy(self):
        data = {'technicians': [{'id': 1, 'name': 'Alex'}, {'id': 2, 'name': 'Bima'}],
                'existing_jobs': [{'tech_id': 1, 'start_date': MONDAY, 'days_duration': 2, 'location': (-6.2, 106.8)}]}
        calendar = Calendar(data)
        self.assertFalse(calendar.is_free(1, [date(2026, 11, 3)]))
        self.assertEqual([tech['id'] for tech in calendar.free_technicians([MONDAY])], [2])
        self.assertEqual(calendar.where[(1, MONDAY)], (-6.2, 106.8))

    def test_unbook_frees_the_days_again(self):
        calendar = Calendar({'technicians': [{'id': 1, 'name': 'Alex'}]})
        calendar.book([{'id': 1}], [MONDAY], None)
        calendar.unbook([{'id': 1}], [MONDAY])
        self.assertTrue(calendar.is_free(1, [MONDAY]))

    def test_score_notes_skip_zero_parts(self):
        self.assertEqual(score_notes({'priority': 50, 'wait': -80, 'sunday': 0}), ['Starts later than possible: -80', 'Priority: +50'])


class ReferenceGreedyTests(unittest.TestCase):
    def test_order_puts_xprj_and_urgent_first(self):
        ranks = [task['priority_rank'] for task in order_tasks(generate(seed=4)['tasks'])]
        self.assertEqual(ranks, sorted(ranks))

    def test_schedules_are_valid_on_many_seeds(self):
        for seed in range(10):
            data = generate(seed=seed)
            self.assertEqual(validate(run_greedy(**data), data), [], f'seed {seed}')

    def test_beats_the_empty_schedule(self):
        data = generate(seed=1)
        self.assertGreater(score(run_greedy(**data), data).total, UNSCHEDULED_PENALTY * len(data['tasks']))

    def test_too_few_technicians_leaves_everything_unscheduled(self):
        data = generate(seed=1, scenario=Scenario(n_technicians=1, n_tasks=3))
        result = run_greedy(**data)
        self.assertEqual(result['scheduled'], [])
        self.assertTrue(all('at least 2' in item['reason'] for item in result['unscheduled']))


if __name__ == '__main__':
    unittest.main()
