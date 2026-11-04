"""验证来源评测统计及失败路径。"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.evaluate_rag import evaluate, load_dataset, main
from rag.services import ServiceError


def match(source, score=0.9):
    return {"source": source, "chunk_id": source + "-1", "score": score,
            "section": "名称", "text": "a" * 200}


class EvaluationTests(unittest.TestCase):
    def test_source_metrics_and_failure_are_not_hidden(self):
        cases = [
            {"id": "a", "query": "a", "expected_sources": ["a(1)"]},
            {"id": "b", "query": "b", "expected_sources": ["b(1)"]},
            {"id": "c", "query": "c", "expected_sources": ["c(1)"]},
            {"id": "d", "query": "d", "expected_sources": []},
            {"id": "e", "query": "e", "expected_sources": []},
        ]
        def search(query, limit):
            self.assertEqual(limit, 5)
            if query == "a": return [match("a(1)")]
            if query == "b": return [match("a(1)"), match("b(1)")]
            if query == "c": raise ServiceError("secret response")
            if query == "d": return [match("a(1)", 0.2)]
            return []
        report = evaluate({"cases": cases}, search)
        summary = report["summary"]
        self.assertEqual((summary["positive"], summary["out_of_scope"], summary["failed"]), (3, 2, 1))
        self.assertEqual((summary["source_top1"], summary["source_hit_at_5"]), (1, 2))
        self.assertAlmostEqual(summary["source_mrr_at_5"], 0.5)
        self.assertEqual(report["cases"][1]["first_source_rank"], 2)
        self.assertEqual(report["cases"][2]["error"], "ServiceError")
        self.assertNotIn("secret response", json.dumps(report))
        self.assertEqual(len(report["cases"][0]["results"][0]["excerpt"]), 160)
        self.assertEqual(report["cases"][4]["results"], [])

    def test_dataset_validation(self):
        dataset, checksum = load_dataset("tests/rag/evaluation_v1.json")
        self.assertEqual(len(dataset["cases"]), 32)
        self.assertEqual(len(checksum), 64)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "bad.json"
            path.write_text(json.dumps({"version":"v1", "cases":[
                {"id":"same", "query":"a", "expected_sources":[]},
                {"id":"same", "query":"b", "expected_sources":[]}]}))
            with self.assertRaises(ValueError): load_dataset(path)

    def test_existing_report_rejected_before_service(self):
        with tempfile.NamedTemporaryFile() as output, \
             patch("sys.argv", ["evaluate", "--output", output.name]), \
             patch("tools.evaluate_rag.configured_services") as service:
            self.assertEqual(main(), 1)
            service.assert_not_called()


if __name__ == "__main__":
    unittest.main()
