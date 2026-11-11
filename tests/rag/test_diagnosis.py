"""诊断结果区分排序遗漏和候选缺失，并拒绝错用查询缓存。"""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from tools.diagnose_retrieval import cached_queries, diagnose


class DiagnosisTests(unittest.TestCase):
    def test_ranking_and_recall_are_separate(self):
        cases = []
        for identity, quote in (
            ("first", "top fact"),
            ("late", "late fact"),
            ("absent", "absent fact"),
        ):
            cases.append(
                {
                    "id": identity,
                    "query": identity,
                    "split": "development",
                    "evidence": [{"source": "grep(1)", "quote": quote}],
                }
            )
        cases.append({"id": "negative", "query": "other", "evidence": []})
        store = Mock()
        matches = [{"source": "grep(1)", "text": "top fact"}]
        matches.extend({"source": "grep(1)", "text": "other"} for _ in range(5))
        matches.append({"source": "grep(1)", "text": "late fact"})
        store.search.return_value = matches
        result = diagnose(cases, [[1.0]] * 4, store)
        self.assertEqual(result["summary"], {"top5": 1, "ranking": 1, "recall_or_coverage": 1})
        self.assertEqual(store.search.call_count, 3)
        self.assertEqual(result["cases"][1]["ranks"]["10"], 7)

    def test_wrong_query_cache_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            (path / "queries-0.json").write_text(
                json.dumps({"sha256": "wrong", "vectors": [[1.0]]})
            )
            with self.assertRaisesRegex(ValueError, "缓存"):
                cached_queries([{"query": "question"}], path)


if __name__ == "__main__":
    unittest.main()
