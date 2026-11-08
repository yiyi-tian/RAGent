"""独立条目分块及所属条目上下文，避免选项间混合和跨条目拼接。"""

import hashlib
import unittest

from rag.chunking import distinct_matches, manual_chunks
from rag.context import ManualContext
from rag.entries import entry_chunks
from rag.services import Qdrant


def manual(text):
    return {
        "source": "test(1)",
        "language": "en",
        "package": "test",
        "package_version": "1",
        "license_file": "/copyright",
        "text": text,
    }


class EntryTests(unittest.TestCase):
    def test_options_are_independent_and_positions_stable(self):
        text = (
            "OPTIONS\n       -r     reverse order\n\n"
            "       -R\n              recurse into directories"
        )
        first = entry_chunks(manual(text))
        self.assertEqual(first, entry_chunks(manual(text)))
        options = [chunk for chunk in first if chunk["entry"].startswith("-")]
        self.assertEqual(len(options), 2)
        self.assertNotIn("recurse", options[0]["body"])
        self.assertNotIn("reverse", options[1]["body"])
        for chunk in first:
            self.assertEqual(chunk["body"], text[chunk["start"] : chunk["end"]])
            self.assertEqual(chunk["document_sha256"], hashlib.sha256(text.encode()).hexdigest())
            self.assertLessEqual(len(chunk["body"]), 900)
            Qdrant._point_payload({"payload": chunk})

    def test_subcommand_continuations_keep_title_and_head(self):
        text = "COMMANDS\n       enable UNIT...\n           Enable one or more units.\n\n"
        text += "           " + "details " * 130 + "\n\n"
        text += "           automatically started on boot\n\n"
        text += "       disable UNIT...\n           Disable one or more units."
        items = entry_chunks(manual(text))
        anchor = next(chunk for chunk in items if "automatically started on boot" in chunk["body"])
        self.assertEqual(anchor["entry"], "enable UNIT...")
        self.assertIn("enable UNIT...", anchor["text"])
        result = ManualContext(items).select([{**anchor, "score": 0.8}])[0]
        self.assertIn("Enable one or more units.", result["context_text"])
        self.assertIn("automatically started on boot", result["context_text"])
        self.assertNotIn("Disable one or more units.", result["context_text"])
        self.assertLessEqual(len(result["context_text"]), 1800)
        self.assertEqual(result["chunk_id"], anchor["chunk_id"])

    def test_same_body_from_different_entries_is_not_discarded(self):
        a = {"source": "test(1)", "entry": "first", "text": "same body"}
        b = {"source": "test(1)", "entry": "second", "text": "same body"}
        self.assertEqual(distinct_matches([a, b]), [a, b])

    def test_entry_context_does_not_cross_sources_with_same_document_hash(self):
        items = entry_chunks(
            manual("OPTIONS\n       -a\n           first\n\n           second"), target=20
        )
        other = [
            {**item, "source": "alias(1)", "chunk_id": "alias-" + item["chunk_id"]}
            for item in items
        ]
        anchor = next(chunk for chunk in items if chunk["entry"].startswith("-"))
        result = ManualContext(items + other).select([{**anchor, "score": 0.9}])[0]
        self.assertTrue(
            all(
                not identity.startswith("alias-")
                for identity in result.get("context_chunk_ids", [])
            )
        )

    def test_multigrain_votes_do_not_count_repeated_entries_twice(self):
        text = (
            "OPTIONS\n\n       -a\n           first option\n\n       -b\n           second option"
        )
        coarse = manual_chunks(manual(text))
        fine = entry_chunks(manual(text))
        options = [item for item in fine if item["entry"].startswith("-")]
        index = ManualContext(coarse + fine)
        once = index.select_multigrain([], [options[0]])
        twice = index.select_multigrain([], options)
        self.assertEqual(once[0]["chunk_id"], coarse[0]["chunk_id"])
        self.assertEqual(once[0]["score"], twice[0]["score"])
        self.assertEqual(len(twice), 1)

    def test_multigrain_does_not_transfer_votes_across_documents(self):
        text = "OPTIONS\n\n       -a\n           first option"
        coarse = manual_chunks(manual(text))
        fine = [{**item, "source": "other(1)"} for item in entry_chunks(manual(text))]
        index = ManualContext(coarse + fine)
        self.assertFalse(index.multigrain)
        result = index.select_multigrain([{**coarse[0], "score": 0.8}], fine)
        self.assertEqual(len(result), 1)
        self.assertNotIn("other(1)", result[0]["text"])

    def test_entry_boundaries_are_checked_at_network_boundary(self):
        item = entry_chunks(manual("OPTIONS\n       -a\n           first"))[-1]
        item["entry_start"] = item["start"] + 1
        with self.assertRaises(ValueError):
            Qdrant._point_payload({"payload": item})


if __name__ == "__main__":
    unittest.main()
