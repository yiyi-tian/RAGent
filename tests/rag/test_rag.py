"""独立模拟 HTTP 服务，不依赖云端凭据。"""

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from rag.__main__ import import_manuals
from rag.search import search_linux_manual
from rag.corpus import find_manual, prepare_manual, split_sections
from rag.services import Embedding, Qdrant, ServiceError


class Handler(BaseHTTPRequestHandler):
    collection = None
    points = {}
    embeddings_invalid = False
    response_status = 200
    batch_sizes = []

    def log_message(self, format, *args):
        pass

    def reply(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        return json.loads(self.rfile.read(int(self.headers["Content-Length"])))

    def do_GET(self):
        if self.path == "/collections/manual" and self.collection:
            return self.reply(200, {"status": "ok", "result": {"config": self.collection}})
        return self.reply(404, {})

    def do_PUT(self):
        body = self.body()
        if self.path == "/collections/manual":
            self.__class__.collection = {
                "params": {"vectors": body["vectors"]},
                "metadata": body["metadata"],
            }
        elif self.path == "/collections/manual/points?wait=true":
            self.points.update({point["id"]: point for point in body["points"]})
        else:
            return self.reply(404, {})
        return self.reply(200, {"status": "ok", "result": True})

    def do_POST(self):
        body = self.body()
        if self.path == "/v1/embeddings":
            if self.response_status != 200:
                return self.reply(
                    self.response_status, {"error": {"message": "secret should not leak"}}
                )
            self.batch_sizes.append(len(body["input"]))
            data = [
                {"index": index, "embedding": [1.0, 0.5, 0.1]}
                for index, _ in enumerate(body["input"])
            ]
            return self.reply(200, {"data": [] if self.embeddings_invalid else data})
        if self.path == "/collections/manual/points/scroll":
            points = []
            source = body.get("filter", {}).get("must", [{}])[0].get("match", {}).get("value")
            for point in self.points.values():
                if source is not None and point["payload"]["source"] != source:
                    continue
                entry = {"id": point["id"]}
                if body.get("with_payload"):
                    entry["payload"] = point["payload"]
                points.append(entry)
            return self.reply(
                200, {"status": "ok", "result": {"points": points, "next_page_offset": None}}
            )
        if self.path in (
            "/collections/manual/points/query",
            "/collections/manual/points/query/batch",
        ):
            requests = body["searches"] if self.path.endswith("/batch") else [body]
            results = []
            for request in requests:
                points = []
                conditions = request.get("filter", {}).get("must", [])
                for point in self.points.values():
                    payload = point["payload"]
                    if any(payload.get(c["key"]) != c["match"]["value"] for c in conditions):
                        continue
                    entry = {"id": point["id"], "score": 0.9}
                    if request.get("with_payload"):
                        entry["payload"] = payload
                    points.append(entry)
                results.append({"points": points[: request["limit"]]})
            result = results if self.path.endswith("/batch") else results[0]
            return self.reply(200, {"status": "ok", "result": result})
        return self.reply(404, {})


class RagTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.thread.join()
        cls.server.server_close()

    def setUp(self):
        Handler.collection = None
        Handler.points = {}
        Handler.embeddings_invalid = False
        Handler.response_status = 200
        Handler.batch_sizes = []
        self.embedding = Embedding(self.url + "/v1", "test-model", "test-key")
        self.store = Qdrant(self.url, "manual", "test-model", self.url + "/v1")

    def test_manual_chunks_are_stable_and_traceable(self):
        with (
            patch("rag.corpus.find_manual", return_value=("/test/man/passwd.1.gz", "zh_CN")),
            patch(
                "rag.corpus.package_info",
                return_value=("test-package", "1.0", "/usr/share/doc/test/copyright"),
            ),
            patch("rag.corpus.render_manual", return_value="名称\n  测试手册\n描述\n  功能说明"),
        ):
            first = prepare_manual("passwd")
            second = prepare_manual("passwd")
        self.assertEqual(first, second)
        self.assertTrue(first)
        self.assertEqual(first[0]["source"], "passwd(1)")
        self.assertEqual(first[0]["package_version"], "1.0")
        self.assertTrue(
            all(len(text) <= 128 for _, text in split_sections("描述\n " + "a" * 400, 128))
        )
        with self.assertRaises(ValueError):
            find_manual("../passwd")

    def test_import_reimport_search_and_mismatch(self):
        chunk = {
            "chunk_id": "11111111-1111-5111-8111-111111111111",
            "source": "ls(1)",
            "text": "列出文件",
            "language": "zh_CN",
            "package": "coreutils",
            "package_version": "1",
            "license_file": "/usr/share/doc/coreutils/copyright",
        }
        with patch("rag.__main__.prepare_manual", return_value=[chunk]):
            self.assertEqual(import_manuals(["ls"], self.embedding, self.store)["chunks"], 1)
            import_manuals(["ls"], self.embedding, self.store)
        self.assertEqual(len(Handler.points), 1)
        changed = {**chunk, "chunk_id": "22222222-2222-5222-8222-222222222222"}
        with patch("rag.__main__.prepare_manual", return_value=[changed]):
            with self.assertRaisesRegex(ServiceError, "新集合"):
                import_manuals(["ls"], self.embedding, self.store)
        self.assertEqual(len(Handler.points), 1)
        result = search_linux_manual("如何列出文件", self.embedding, self.store)
        self.assertEqual(
            (result[0]["source"], result[0]["text"], result[0]["score"]), ("ls(1)", "列出文件", 0.9)
        )
        with self.assertRaises(ServiceError):
            Qdrant(self.url, "manual", "other-model", self.url + "/v1").ensure_collection(3)
        with self.assertRaises(ServiceError):
            self.store.ensure_collection(4)
        Handler.points = {}
        self.assertEqual(search_linux_manual("不存在", self.embedding, self.store), [])

    def test_context_strategy_reimport_and_search(self):
        from rag.chunking import manual_chunks

        text = "OPTIONS\n\n"
        for index in range(8):
            text += f"    option {index}: " + "description " * 5 + "\n\n"
        chunks = manual_chunks(
            {
                "source": "ls(1)",
                "language": "en",
                "package": "coreutils",
                "package_version": "1",
                "license_file": "/copyright",
                "text": text,
            },
            target=100,
            limit=150,
            overlap=0,
        )
        with patch("rag.__main__.prepare_manual", return_value=chunks):
            import_manuals(["ls"], self.embedding, self.store)
            import_manuals(["ls"], self.embedding, self.store)
        self.assertEqual(len(Handler.points), len(chunks))
        self.store.retrieval_strategy = "context"
        before = len(Handler.batch_sizes)
        result = search_linux_manual("option", self.embedding, self.store)
        self.assertTrue(any("context_text" in match for match in result))
        self.assertEqual(len(Handler.batch_sizes), before + 1)
        index = self.store.search_index
        self.store.prepare_search()
        self.assertIs(self.store.search_index, index)
        self.assertTrue(all(match["score"] == 0.9 for match in result))

    def test_multigrain_roundtrip_and_package_only_change_is_rejected(self):
        from rag.chunking import manual_chunks
        from rag.entries import entry_chunks

        text = (
            "OPTIONS\n\n       -a\n           first option\n\n       -b\n           second option"
        )
        manual = {
            "source": "ls(1)",
            "language": "en",
            "package": "test",
            "package_version": "1",
            "license_file": "/copyright",
            "text": text,
        }
        chunks = manual_chunks(manual) + entry_chunks(manual)
        with patch("rag.__main__.prepare_manual", return_value=chunks):
            import_manuals(["ls"], self.embedding, self.store)
            import_manuals(["ls"], self.embedding, self.store)
        self.store.retrieval_strategy = "context"
        before = len(Handler.batch_sizes)
        result = search_linux_manual("first option", self.embedding, self.store)
        self.assertTrue(self.store.search_index.multigrain)
        self.assertEqual(len(Handler.batch_sizes), before + 1)
        self.assertTrue(result)
        self.assertTrue(all(0 < item["score"] < 0.9 for item in result))
        unknown = {"result": [{"points": [{"id": "unknown", "score": 0.9}]}, {"points": []}]}
        with patch.object(self.store, "_call", return_value=unknown):
            with self.assertRaisesRegex(ServiceError, "格式错误"):
                self.store.search_multigrain([1.0, 0.5, 0.1], 20)
        changed = [{**chunk, "package_version": "2"} for chunk in chunks]
        with patch("rag.__main__.prepare_manual", return_value=changed):
            with self.assertRaisesRegex(ServiceError, "来源版本"):
                import_manuals(["ls"], self.embedding, self.store)
        self.assertTrue(
            all(point["payload"]["package_version"] == "1" for point in Handler.points.values())
        )

    def test_default_import_builds_both_grains(self):
        text = (
            "OPTIONS\n\n       -a\n           first option\n\n       -b\n           second option"
        )
        with (
            patch("rag.corpus.find_manual", return_value=("/test/ls.1", "en")),
            patch("rag.corpus.package_info", return_value=("test", "1", "/copyright")),
            patch("rag.corpus.render_manual", return_value=text),
        ):
            import_manuals(["ls"], self.embedding, self.store)
        versions = {point["payload"]["chunking_version"] for point in Handler.points.values()}
        self.assertEqual(versions, {"paragraph-v2", "entry-v3"})

    def test_batch_boundaries_and_provider_errors(self):
        chunk = {
            "source": "ls(1)",
            "text": "列出文件",
            "language": "en",
            "package": "coreutils",
            "package_version": "1",
            "license_file": "/usr/share/doc/coreutils/copyright",
        }
        chunks = [
            {**chunk, "chunk_id": f"{index:08d}-1111-5111-8111-111111111111"} for index in range(21)
        ]
        with patch("rag.__main__.prepare_manual", return_value=chunks):
            import_manuals(["ls"], self.embedding, self.store)
        self.assertEqual(Handler.batch_sizes, [10, 10, 1])
        with self.assertRaises(ValueError):
            self.embedding.embed(["x"] * 11)
        for status, message in ((401, "认证"), (403, "权限"), (429, "限流")):
            Handler.response_status = status
            with self.assertRaisesRegex(ServiceError, message) as error:
                self.embedding.embed(["test"])
            self.assertNotIn("secret should not leak", str(error.exception))
        with patch("rag.services.http_opener") as opener:
            opener.return_value.open.side_effect = TimeoutError()
            with self.assertRaisesRegex(ServiceError, "超时"):
                self.embedding.embed(["test"])
        Handler.response_status = 200
        v4 = Embedding(self.url + "/v1", "text-embedding-v4", "test-key")
        with self.assertRaisesRegex(ServiceError, "1024"):
            v4.embed(["test"])

    def test_missing_response_is_reported(self):
        with patch("rag.services.request_json", return_value=None):
            with self.assertRaisesRegex(ServiceError, "未返回响应"):
                self.embedding.embed(["test"])
        with patch.object(self.store, "_call", return_value=None):
            with self.assertRaisesRegex(ServiceError, "未返回检索结果"):
                self.store.search([1.0], 5)
            with self.assertRaisesRegex(ServiceError, "未返回来源检查结果"):
                self.store.verify_sources([{"source": "ls(1)", "chunk_id": "id"}])
        with patch.object(self.store, "_call", side_effect=[None, {"status": "ok"}, None]):
            with self.assertRaisesRegex(ServiceError, "创建后未返回集合信息"):
                self.store.ensure_collection(3, create=True)

    def test_bad_embedding_and_unavailable_service(self):
        Handler.embeddings_invalid = True
        with self.assertRaises(ServiceError):
            self.embedding.embed(["test"])
        with self.assertRaises(ServiceError):
            Qdrant(
                "http://127.0.0.1:1", "manual", "model", self.url + "/v1", timeout=0.1
            ).ensure_collection(3)
        with self.assertRaises(ValueError):
            search_linux_manual("  ", self.embedding, self.store)


if __name__ == "__main__":
    unittest.main()
