"""The public version ships a stub engine: Auto-Suggest answers, but schedules nothing."""
import unittest
from datetime import date

from apps.scheduling.engine.pipeline import STUB_REASON, run_engine


class EngineStubTests(unittest.TestCase):
    def test_every_task_comes_back_unscheduled_with_a_reason(self):
        tasks = [
            {'id': 1, 'doc_type': 'svc', 'doc_no': '001/11/SVC/26', 'client_name': 'Example Client Ltd'},
            {'id': 2, 'doc_type': 'prj', 'doc_no': '002/PRJ/26', 'client_name': 'Sample Store Co'},
        ]
        result = run_engine(tasks, [{'id': 1, 'name': 'Alex'}], date(2026, 11, 2), set(), {})
        self.assertEqual(result['scheduled'], [])
        self.assertEqual([item['project_id'] for item in result['unscheduled']], [1, 2])
        self.assertTrue(all(item['reason'] == STUB_REASON for item in result['unscheduled']))
        self.assertEqual(result['engine_used'], 'none')


if __name__ == '__main__':
    unittest.main()
