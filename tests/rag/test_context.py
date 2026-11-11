"""上下文边界与索引初始化的有意义行为。"""

import concurrent.futures
import hashlib
import unittest
from unittest.mock import Mock

from rag.context import ManualContext
from rag.services import Qdrant, ServiceError


def chunks():
    result = []
    text = "first fact\n\nsecond fact\n\nthird fact"
    digest = hashlib.sha256(text.encode()).hexdigest()
    for index, body in enumerate(("first fact", "second fact", "third fact")):
        start = text.index(body)
        result.append(
            {
                "chunk_id": str(index),
                "source": "test(1)",
                "section": "DESCRIPTION",
                "text": body,
                "body": body,
                "start": start,
                "end": start + len(body),
                "document_sha256": digest,
                "chunking_version": "paragraph-v2",
                "score": 0.9,
            }
        )
    return result


class ContextTests(unittest.TestCase):
    def test_neighbors_preserve_text_rank_score_and_anchor(self):
        items = chunks()
        index = ManualContext(items)
        result = index.select([items[1], items[0]])
        self.assertEqual(result[0]["chunk_id"], "1")
        self.assertEqual(result[0]["score"], 0.9)
        self.assertEqual(result[0]["body"], "second fact")
        self.assertIn("first fact", result[0]["context_text"])
        self.assertIn("third fact", result[0]["context_text"])
        self.assertEqual(result[0]["context_chunk_ids"], ["0", "1", "2"])
        self.assertNotIn("context_text", items[1])

    def test_different_sections_documents_and_limits_are_not_merged(self):
        items = chunks()
        items[0]["section"] = "NAME"
        items[2]["document_sha256"] = "other"
        index = ManualContext(items)
        self.assertEqual(index.expand(items[1]), items[1])
        self.assertEqual(ManualContext(chunks()).expand(chunks()[1], max_chars=12), chunks()[1])
        self.assertEqual(index.select([]), [])
        legacy = {"source": "old(1)", "chunk_id": "old", "text": "legacy", "score": 0.5}
        self.assertEqual(index.select([legacy]), [legacy])

    def test_same_context_not_returned_twice(self):
        items = chunks()[:2]
        result = ManualContext(items).select(items)
        self.assertEqual(len(result), 1)

    def test_index_is_initialized_once_across_threads(self):
        store = Qdrant("http://127.0.0.1", "manual", "test", "http://127.0.0.1", strategy="context")
        store.ensure_collection = Mock()
        info = {"result": {"config": {"params": {"vectors": {"size": 3}}}}}
        page = {
            "result": {"points": [{"payload": item} for item in chunks()], "next_page_offset": None}
        }
        store._call = Mock(side_effect=[info, page])
        with concurrent.futures.ThreadPoolExecutor(4) as executor:
            list(executor.map(lambda _: store.prepare_search(), range(8)))
        self.assertEqual(store._call.call_count, 2)
        store.ensure_collection.assert_called_once_with(3)

    def test_bad_context_payload_is_not_accepted(self):
        store = Qdrant("http://127.0.0.1", "manual", "test", "http://127.0.0.1", strategy="context")
        store.ensure_collection = Mock()
        item = {**chunks()[0], "start": -1}
        store._call = Mock(
            side_effect=[
                {"result": {"config": {"params": {"vectors": {"size": 3}}}}},
                {"result": {"points": [{"payload": item}], "next_page_offset": None}},
            ]
        )
        with self.assertRaisesRegex(ServiceError, "载荷"):
            store.prepare_search()
        self.assertIsNone(store.search_index)


if __name__ == "__main__":
    unittest.main()
