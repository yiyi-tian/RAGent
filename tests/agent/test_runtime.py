"""请求截止时间、线程协作取消及非付费就绪检查。"""

import asyncio
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer
from rag.runtime import REQUEST_STATE, RequestExpired, RequestState, remaining_timeout
from rag.services import Qdrant, ServiceError, request_json
from ragent.__main__ import create_app
from ragent.agent import answer
from ragent.retrieval import ManualRetriever


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_ready_rejects_wrong_dimension_before_first_query(self):
        store = Qdrant(
            "http://127.0.0.1:6333", "fixture", "text-embedding-v4", "https://embedding.example"
        )
        vectors = {"size": 512, "distance": "Cosine"}
        metadata = {
            "ragent_embedding_model": store.model,
            "ragent_embedding_endpoint_sha256": store.embedding_identity,
        }
        info = {"result": {"config": {"params": {"vectors": vectors}, "metadata": metadata}}}
        with patch.object(store, "_call", return_value=info):
            with self.assertRaises(ServiceError):
                store.check_ready()
            vectors["size"] = 1024
            store.check_ready()
            metadata["ragent_embedding_model"] = "other-model"
            with self.assertRaises(ServiceError):
                store.check_ready()

    async def test_deadline_and_cancel_before_http(self):
        state = RequestState(time.monotonic() + 0.05)
        token = REQUEST_STATE.set(state)
        try:
            self.assertLessEqual(remaining_timeout(20), 0.05)
            state.cancelled.set()
            with patch("rag.services.http_opener") as opener:
                with self.assertRaises(RequestExpired):
                    request_json("GET", "http://127.0.0.1:9")
                opener.assert_not_called()
        finally:
            REQUEST_STATE.reset(token)

    async def test_running_retrieval_cannot_start_next_call_after_cancel(self):
        started = threading.Event()
        release = threading.Event()
        stopped = threading.Event()
        observed = []

        def search(*args):
            state = REQUEST_STATE.get()
            self.assertIsNotNone(state)
            started.set()
            try:
                release.wait(3)
                try:
                    remaining_timeout(20)
                except RequestExpired:
                    observed.append("cancelled")
                    raise
                self.fail("取消后仍然继续检索")
            finally:
                stopped.set()

        retriever = ManualRetriever(SimpleNamespace(), SimpleNamespace(), workers=1)
        self.addAsyncCleanup(retriever.close)
        with patch("ragent.retrieval.search_linux_manual", side_effect=search):
            task = asyncio.create_task(answer("问题", "rag", retriever=retriever, timeout=2))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 2))
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                release.set()
                self.assertTrue(await asyncio.to_thread(stopped.wait, 2))
                self.assertEqual(observed, ["cancelled"])
            finally:
                release.set()
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    async def test_health_stays_live_and_readiness_recovers(self):
        class Dependency:
            failing = True

            async def check_ready(self):
                if self.failing:
                    raise ServiceError("受控依赖不可用")

        dependency = Dependency()
        async with TestClient(TestServer(create_app(dependency))) as client:
            response = await client.get("/health")
            self.assertEqual(response.status, 200)
            self.assertEqual(await response.json(), {"alive": True})
            response = await client.get("/ready")
            self.assertEqual(response.status, 503)
            self.assertFalse((await response.json())["cloud_checked"])
            dependency.failing = False
            response = await client.get("/ready")
            self.assertEqual(response.status, 200)
            self.assertTrue((await response.json())["ready"])
