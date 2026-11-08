"""真实检索适配路径与 HTTP 请求错误。"""

import unittest
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer
from rag.services import ServiceError
from ragent.__main__ import create_app
from ragent.retrieval import create_retriever


class RetrievalTests(unittest.IsolatedAsyncioTestCase):
    async def test_projection_and_empty(self):
        class Embedding:
            def embed(self, texts):
                return [[1.0]]

        class Store:
            def ensure_collection(self, dimension):
                pass

            def search(self, vector, limit):
                return [
                    {
                        "source": "ls(1)",
                        "chunk_id": "1",
                        "text": "正文",
                        "score": 1.0,
                        "license_file": "/private/path",
                    }
                ]

        store = Store()
        with patch("ragent.retrieval.configured_services", return_value=(Embedding(), store)):
            retrieve = create_retriever()
        self.addAsyncCleanup(retrieve.close)
        result = await retrieve("查询")
        self.assertNotIn("license_file", result["sources"][0])
        self.assertIn("[1] ls(1)\n正文", result["text"])
        with patch.object(store, "search", return_value=[]):
            self.assertEqual((await retrieve("查询"))["sources"], [])

    async def test_http_errors(self):
        async def unavailable(query):
            raise ServiceError("受控故障")

        async with TestClient(TestServer(create_app(unavailable))) as client:
            response = await client.post("/agent", data="invalid")
            self.assertEqual(response.status, 400)
            response = await client.post(
                "/agent", json={"request_id": "1", "mode": "rag", "query": "查询"}
            )
            self.assertEqual(response.status, 502)
            self.assertEqual((await response.json())["code"], "UPSTREAM_UNAVAILABLE")
            response = await client.post(
                "/agent", json={"request_id": "2", "mode": "chat", "query": "查询"}
            )
            self.assertEqual(response.status, 200)
