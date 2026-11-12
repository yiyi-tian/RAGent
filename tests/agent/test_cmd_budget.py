"""CMD付费验收的跨线程预算不可超发，重开账本不重置累计计数。"""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import unittest

from tools.validate_cmd import Budget


class CmdBudgetTests(unittest.TestCase):
    def test_concurrent_phase_and_total_limits_persist(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "budget.json"
            budget = Budget(path, "formal")

            def reserve(index):
                try:
                    budget.reserve("model")
                    return True
                except ValueError:
                    return False

            with ThreadPoolExecutor(max_workers=8) as workers:
                accepted = list(workers.map(reserve, range(64)))
            self.assertEqual(sum(accepted), 48)
            for phase in ("precheck", "fix"):
                for _ in range(16):
                    Budget(path, phase).reserve("model")
            with self.assertRaises(ValueError):
                Budget(path, "fix").reserve("model")
            for _ in range(40):
                budget.reserve("embedding")
            with self.assertRaises(ValueError):
                Budget(path, "precheck").reserve("embedding")
            self.assertEqual(json.loads(path.read_text())["used"], {"model": 80, "embedding": 40})


if __name__ == "__main__":
    unittest.main()
