"""The research tools: synthetic data in the real engine input shape, and a fair evaluator."""
import unittest
from datetime import date

from research.contract import engine_result, scheduled_item, unscheduled_item
from research.evaluate import SOFT, UNSCHEDULED_PENALTY, score, summary, validate
from research.reference.stub import run_stub
from research.synthetic import Scenario, generate

# The keys apps/scheduling/engine_input.build_task() gives every task.
TASK_KEYS = {
    'id', 'doc_type', 'doc_no', 'client_id', 'client_name', 'visit_type', 'location',
    'priority_rank', 'initial_lapse_days', 'estimated_days', 'remaining_days',
    'is_continuation', 'earliest_start',
}
MONDAY = date(2026, 11, 2)
ALEX, BIMA, CITRA = {'id': 1, 'name': 'Alex'}, {'id': 2, 'name': 'Bima'}, {'id': 3, 'name': 'Citra'}


def task(task_id, days=1, earliest=MONDAY, rank=2):
    return {
        'id': task_id, 'doc_type': 'svc', 'doc_no': f'{task_id:03d}/11/SVC/26', 'client_id': 1,
        'client_name': 'Example Store', 'visit_type': 'SVC', 'location': (-6.2, 106.8),
        'priority_rank': rank, 'initial_lapse_days': 0, 'estimated_days': days,
        'remaining_days': days, 'is_continuation': False, 'earliest_start': earliest,
    }


def tiny_data(*tasks, existing_jobs=()):
    return {
        'tasks': list(tasks), 'technicians': [ALEX, BIMA, CITRA], 'start_date': MONDAY,
        'holidays': set(), 'existing_jobs': list(existing_jobs),
        'history_ctx': {'tech_client_visits': {}, 'tech_visit_type_count': {}, 'tech_pair_cowork': {}},
    }


def result(scheduled=(), unscheduled=()):
    return engine_result(list(scheduled), list(unscheduled), 'test', started_at=0.0)


class SyntheticDataTests(unittest.TestCase):
    def test_matches_the_engine_input_shape(self):
        data = generate(seed=3)
        self.assertEqual(set(data), {'tasks', 'technicians', 'start_date', 'holidays', 'history_ctx', 'existing_jobs'})
        for item in data['tasks']:
            self.assertEqual(set(item), TASK_KEYS)
            self.assertGreaterEqual(item['remaining_days'], 1)
            self.assertGreaterEqual(item['earliest_start'], data['start_date'])
        self.assertEqual(set(data['history_ctx']), {'tech_client_visits', 'tech_visit_type_count', 'tech_pair_cowork'})

    def test_same_seed_gives_the_same_problem(self):
        self.assertEqual(generate(seed=7), generate(seed=7))
        self.assertNotEqual(generate(seed=7), generate(seed=8))

    def test_scenario_sets_the_size(self):
        data = generate(seed=1, scenario=Scenario(n_technicians=4, n_tasks=6))
        self.assertEqual(len(data['technicians']), 4)
        self.assertEqual(len(data['tasks']), 6)

    def test_existing_jobs_never_overlap_for_one_technician(self):
        data = generate(seed=5, scenario=Scenario(n_technicians=20, busy_share=0.6, yesterday_share=0.4))
        jobs_per_tech = {}
        for job in data['existing_jobs']:
            jobs_per_tech[job['tech_id']] = jobs_per_tech.get(job['tech_id'], 0) + 1
        self.assertTrue(all(count == 1 for count in jobs_per_tech.values()))


class EvaluatorTests(unittest.TestCase):
    def test_the_public_stub_is_valid_and_pays_for_every_unscheduled_task(self):
        data = generate(seed=2, scenario=Scenario(n_tasks=5))
        stub = run_stub(**data)
        self.assertEqual(validate(stub, data), [])
        self.assertEqual(score(stub, data).total, UNSCHEDULED_PENALTY * 5)

    def test_a_correct_schedule_is_valid(self):
        data = tiny_data(task(1, days=2), task(2))
        schedule = result([scheduled_item(data['tasks'][0], [ALEX, BIMA], MONDAY),
                           scheduled_item(data['tasks'][1], [CITRA, ALEX], date(2026, 11, 4))])
        self.assertEqual(validate(schedule, data), [])
        self.assertEqual(summary(schedule, data)['total_wait_days'], 2)

    def test_double_booking_is_caught(self):
        data = tiny_data(task(1, days=2), task(2))
        schedule = result([scheduled_item(data['tasks'][0], [ALEX, BIMA], MONDAY),
                           scheduled_item(data['tasks'][1], [BIMA, CITRA], date(2026, 11, 3))])
        self.assertTrue(any('technician 2 is already busy' in line for line in validate(schedule, data)))

    def test_existing_jobs_count_as_busy(self):
        data = tiny_data(task(1), existing_jobs=[{'tech_id': 1, 'start_date': MONDAY, 'days_duration': 1, 'location': None}])
        schedule = result([scheduled_item(data['tasks'][0], [ALEX, BIMA], MONDAY)])
        self.assertTrue(any('already busy' in line for line in validate(schedule, data)))

    def test_wrong_team_size_and_missing_tasks_are_caught(self):
        data = tiny_data(task(1), task(2))
        schedule = result([scheduled_item(data['tasks'][0], [ALEX], MONDAY)])
        problems = validate(schedule, data)
        self.assertTrue(any('needs 2 different technicians' in line for line in problems))
        self.assertTrue(any('missing from the result' in line for line in problems))

    def test_starting_before_earliest_start_is_caught(self):
        data = tiny_data(task(1, earliest=date(2026, 11, 4)))
        schedule = result([scheduled_item(data['tasks'][0], [ALEX, BIMA], MONDAY)])
        self.assertTrue(any('before its earliest start' in line for line in validate(schedule, data)))

    def test_sunday_work_and_waiting_are_penalised_per_technician(self):
        sunday = date(2026, 11, 8)
        data = tiny_data(task(1))
        parts = score(result([scheduled_item(data['tasks'][0], [ALEX, BIMA], sunday)]), data).parts
        self.assertEqual(parts['sunday'], SOFT['sunday_work_per_day'] * 2)
        self.assertEqual(parts['wait'], SOFT['wait_per_day_normal'] * 6)

    def test_unscheduled_tasks_cost_points(self):
        data = tiny_data(task(1))
        empty = result(unscheduled=[unscheduled_item(data['tasks'][0], 'No free team')])
        self.assertEqual(validate(empty, data), [])
        self.assertEqual(score(empty, data).total, UNSCHEDULED_PENALTY)


if __name__ == '__main__':
    unittest.main()
