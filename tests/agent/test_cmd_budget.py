"""CMD付费验收的跨线程预算不可超发，重开账本不重置累计计数。"""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import unittest

from tools.validate_cmd import Budget, source_hit


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


class SourceVerdictTests(unittest.TestCase):
    def test_answer_mentions_are_not_evidence(self):
        final = {"text": "没有ls(1)证据", "sources": [{"source": "man(1)"}]}
        self.assertFalse(source_hit(final, "ls(1)"))

    def test_actual_source_identity_is_required(self):
        self.assertTrue(source_hit({"sources": [{"source": "ls(1)"}]}, "ls(1)"))
        self.assertFalse(source_hit(None, "ls(1)"))
        self.assertFalse(source_hit({"sources": None}, "ls(1)"))


if __name__ == "__main__":
    unittest.main()
