"""证据指标与实验过滤的行为测试。"""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from rag.services import ServiceError
from tools.evaluate_evidence import command_source, evaluate, filtered_search, load_cases, main


def result(source, chunk_id, score=0.7):
    return {
        "source": source,
        "chunk_id": chunk_id,
        "score": score,
        "text": "示例文本",
        "section": "描述",
    }


class FakeEmbedding:
    def __init__(self):
        self.queries = []

    def embed(self, texts):
        self.queries.extend(texts)
        if "失败" in texts[0]:
            raise ServiceError("敏感细节不能进入报告")
        return [[1.0, 0.0]]


class FakeStore:
    def __init__(self):
        self.filtered_sources = []

    def ensure_collection(self, dimension):
        if dimension != 2:
            raise ServiceError("维度错误")

    def search(self, vector, limit):
        return [result("man(1)", "wrong"), result("ls(1)", "ls-evidence")]

    def _call(self, method, path, body):
        source = body["filter"]["must"][0]["match"]["value"]
        self.filtered_sources.append(source)
        return {"result": {"points": [{"payload": result(source, "ls-evidence"), "score": 0.9}]}}


class EvidenceTests(unittest.TestCase):
    def test_command_boundary_and_ambiguity(self):
        self.assertEqual(command_source("如何使用ls查看文件？"), "ls(1)")
        self.assertEqual(command_source("gpasswd 添加成员"), "gpasswd(1)")
        self.assertEqual(command_source("LS 与 ls 的帮助"), "ls(1)")
        for query in ("如何查看文件", "systemctl 与 ls", "false", "MANSECT", "my-ls"):
            self.assertIsNone(command_source(query))

    def test_comparison_embeds_once_fallback_and_failures(self):
        cases = [
            {
                "id": "a",
                "query": "ls 查看文件",
                "split": "development",
                "expected_sources": ["ls(1)"],
                "evidence_ids": ["ls-evidence"],
                "evidence_status": "supported",
            },
            {
                "id": "b",
                "query": "如何查看文件",
                "split": "development",
                "expected_sources": ["ls(1)"],
                "evidence_ids": ["ls-evidence"],
                "evidence_status": "supported",
            },
            {
                "id": "c",
                "query": "ps与ls比较",
                "split": "development",
                "expected_sources": [],
                "evidence_ids": [],
                "evidence_status": "insufficient",
            },
            {
                "id": "d",
                "query": "ls失败",
                "split": "validation",
                "expected_sources": ["ls(1)"],
                "evidence_ids": ["ls-evidence"],
                "evidence_status": "supported",
            },
        ]
        embedding, store = FakeEmbedding(), FakeStore()
        report = evaluate({"cases": cases}, embedding, store)
        self.assertEqual(len(embedding.queries), len(cases))
        self.assertEqual(store.filtered_sources, ["ls(1)"])
        self.assertEqual(report["cases"][0]["baseline"]["evidence_rank"], 2)
        self.assertEqual(report["cases"][0]["filtered"]["evidence_rank"], 1)
        self.assertEqual(
            report["cases"][1]["baseline"]["results"], report["cases"][1]["filtered"]["results"]
        )
        self.assertEqual(report["summary"]["development"]["baseline"]["evidence_hit_at_5"], 2)
        self.assertEqual(report["summary"]["development"]["filtered"]["evidence_top1"], 1)
        self.assertEqual(report["summary"]["validation"]["baseline"]["errors"], 1)
        self.assertEqual(report["summary"]["validation"]["filtered"]["filter_applied"], 0)
        self.assertNotIn("敏感细节", json.dumps(report, ensure_ascii=False))

    def test_filtered_failure_preserves_baseline(self):
        case = {
            "id": "a",
            "query": "ls 文件",
            "split": "development",
            "expected_sources": ["ls(1)"],
            "evidence_ids": ["ls-evidence"],
            "evidence_status": "supported",
        }
        store = FakeStore()
        with patch.object(store, "_call", side_effect=ServiceError("private response")):
            report = evaluate({"cases": [case]}, FakeEmbedding(), store)
        self.assertEqual(report["cases"][0]["baseline"]["evidence_rank"], 2)
        self.assertEqual(report["summary"]["development"]["baseline"]["errors"], 0)
        self.assertEqual(report["summary"]["development"]["filtered"]["errors"], 1)
        self.assertNotIn("private response", json.dumps(report))

    def test_filtered_service_failure_and_invalid_response(self):
        store = FakeStore()
        with patch.object(store, "_call", return_value=None):
            with self.assertRaises(ServiceError):
                filtered_search(store, [1.0], "ls(1)")
        with patch.object(
            store, "_call", return_value={"result": {"points": [{"payload": {}, "score": 0.5}]}}
        ):
            with self.assertRaises(ServiceError):
                filtered_search(store, [1.0], "ls(1)")

    def test_dataset_snapshot_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory) / "snapshot"
            dataset = Path(directory) / "dataset.json"
            snapshot.write_bytes(b"test")
            dataset.write_text(
                json.dumps({"version": "v1", "snapshot_sha256": "different", "cases": []})
            )
            with self.assertRaisesRegex(ValueError, "快照不匹配"):
                load_cases(dataset, snapshot)

    def test_existing_report_rejected_before_service(self):
        with (
            tempfile.NamedTemporaryFile() as output,
            patch("sys.argv", ["evaluate", "--output", output.name]),
            patch("tools.evaluate_evidence.configured_services") as service,
        ):
            self.assertEqual(main(), 1)
            service.assert_not_called()


if __name__ == "__main__":
    unittest.main()
