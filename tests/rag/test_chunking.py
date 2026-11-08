"""分块的原文边界、稳定性以及评测预算验证，不调用外部服务。"""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from rag.chunking import distinct_matches, manual_chunks
from rag.search import search_linux_manual
from rag.services import Qdrant, ServiceError
from tools.compare_rag import Budget, cached_vectors, evidence_rank, validate_evidence


def manual(text):
    return {
        "source": "ls(1)",
        "language": "en",
        "package": "coreutils",
        "package_version": "1",
        "license_file": "/usr/share/doc/coreutils/copyright",
        "text": text,
    }


class ChunkingTests(unittest.TestCase):
    def test_offsets_limits_options_and_stable_ids(self):
        text = "OPTIONS\n\n       -a, --all\n              include hidden files\n\n"
        text += "       -l\n              long listing format\n\n"
        text += "DESCRIPTION\n\n       " + "long paragraph " * 200
        chunks = manual_chunks(manual(text), target=100, limit=150, overlap=30)
        self.assertEqual(chunks, manual_chunks(manual(text), target=100, limit=150, overlap=30))
        self.assertEqual(len(chunks), len({chunk["chunk_id"] for chunk in chunks}))
        for chunk in chunks:
            body = text[chunk["start"] : chunk["end"]]
            self.assertEqual(chunk["body"], body)
            self.assertLessEqual(len(body), 150)
            self.assertEqual(chunk["document_sha256"], hashlib.sha256(text.encode()).hexdigest())
            self.assertEqual(chunk["content_sha256"], hashlib.sha256(body.encode()).hexdigest())
            self.assertTrue(chunk["text"].startswith("ls(1) — " + chunk["section"]))
        self.assertTrue(
            any("-a, --all\n              include hidden files" in c["body"] for c in chunks)
        )
        covered = set()
        for chunk in chunks:
            covered.update(range(chunk["start"], chunk["end"]))
        self.assertTrue(
            all(index in covered for index, char in enumerate(text) if not char.isspace())
        )
        changed = manual_chunks(manual(text + " changed"), target=100, limit=150, overlap=30)
        self.assertNotEqual(chunks[0]["chunk_id"], changed[0]["chunk_id"])

    def test_section_heading_without_blank_line_is_recognized(self):
        text = "SYNOPSIS\n    ls [OPTIONS]\n\nDESCRIPTION\n    list files\n\nOPTIONS\n    -a include hidden"
        chunks = manual_chunks(manual(text))
        self.assertEqual(
            [chunk["section"] for chunk in chunks], ["SYNOPSIS", "DESCRIPTION", "OPTIONS"]
        )
        self.assertIn("list files", chunks[1]["body"])

    def test_online_v2_search_fetches_candidates_and_validates_payload(self):
        chunks = manual_chunks(manual("OPTIONS\n\n    -a include hidden"))
        store = Mock()
        store.search.return_value = [chunks[0], chunks[0]]
        embedding = Mock()
        embedding.embed.return_value = [[1.0]]
        self.assertEqual(search_linux_manual("hidden", embedding, store), chunks)
        store.search.assert_called_once_with([1.0], 10)
        payload = {**chunks[0], "start": -1}
        qdrant = Qdrant("http://127.0.0.1", "test", "model", "http://127.0.0.1")
        qdrant._call = Mock(
            return_value={"result": {"points": [{"payload": payload, "score": 0.8}]}}
        )
        with self.assertRaisesRegex(ServiceError, "响应格式"):
            qdrant.search([1.0], 10)

    def test_overlap_retains_last_short_paragraph(self):
        text = "OPTIONS\n\n    " + "a" * 50 + "\n\n    " + "b" * 20 + "\n\n    " + "c" * 60
        chunks = manual_chunks(manual(text), target=90, limit=120, overlap=30)
        self.assertEqual(len(chunks), 2)
        overlap = chunks[0]["end"] - chunks[1]["start"]
        self.assertGreater(overlap, 0)
        self.assertLessEqual(overlap, 30)
        self.assertTrue(all("b" * 20 in chunk["body"] for chunk in chunks))

    def test_duplicates_preserve_rank_and_distinct_facts(self):
        first = {"source": "ls(1)", "text": "first", "start": 0, "end": 100, "document_sha256": "a"}
        duplicate = {**first, "text": "overlapping", "start": 10, "end": 110}
        distinct = {**first, "text": "other", "start": 90, "end": 200}
        exact = {**first, "text": " first  "}
        other_source = {"source": "ps(1)", "text": "first"}
        result = distinct_matches([first, duplicate, exact, distinct, other_source], 3)
        self.assertEqual(result, [first, distinct, other_source])
        self.assertEqual(distinct_matches([], 5), [])

    def test_evidence_matches_raw_text_across_multiple_chunks(self):
        text = "first fact\n\nsecond fact"
        digest = hashlib.sha256(text.encode()).hexdigest()
        case = {
            "id": "test",
            "query": "query",
            "evidence_status": "supported",
            "evidence": [],
        }
        for quote in ("first fact", "second fact"):
            start = text.index(quote)
            case["evidence"].append(
                {
                    "source": "ls(1)",
                    "document_sha256": digest,
                    "start": start,
                    "end": start + len(quote),
                    "quote": quote,
                }
            )
        validate_evidence([case], [manual(text)])
        matches = [{"source": "ls(1)", "text": "first  fact"}]
        self.assertIsNone(evidence_rank(case, matches))
        matches.append({"source": "ls(1)", "text": "second fact"})
        self.assertEqual(evidence_rank(case, matches), 2)
        case["evidence"][0]["start"] = 1
        with self.assertRaisesRegex(ValueError, "位置"):
            validate_evidence([case], [manual(text)])

    def test_budget_counts_failed_calls_and_cache_reuses_vectors(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            budget = Budget(directory / "budget.json")
            embedding = Mock()
            embedding.embed.side_effect = OSError("failed")
            with self.assertRaises(OSError):
                cached_vectors(embedding, ["one"], directory / "vectors.json", budget)
            self.assertEqual(Budget(budget.path).counts["embedding_requests"], 1)
            embedding.embed.side_effect = None
            embedding.embed.return_value = [[1.0]]
            for _ in range(2):
                self.assertEqual(
                    cached_vectors(embedding, ["one"], directory / "vectors.json", budget), [[1.0]]
                )
            self.assertEqual(embedding.embed.call_count, 2)
            for _ in range(20):
                budget.reserve(model=True)
            with self.assertRaisesRegex(RuntimeError, "预算"):
                budget.reserve(model=True)
            self.assertEqual(json.loads(budget.path.read_text())["model_calls"], 20)


if __name__ == "__main__":
    unittest.main()
