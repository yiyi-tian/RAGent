"""计量预算与模型并发限制，不访问云端。"""

import json
import asyncio
from concurrent.futures import ThreadPoolExecutor
import os
import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock, patch, call
from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage
from ragent.errors import ModelError
from ragent.model import DeepSeekModel
from tools.benchmark_agent import Budget, percentile, measured_retriever, stage_summary


class BenchmarkTests(unittest.IsolatedAsyncioTestCase):
    async def test_initial_credits_are_persisted_and_share_the_phase_limit(self):
        from tools.benchmark_workers import Coordinator, RemoteBudget

        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "budget.json"
            budget = Budget(100, 100, ledger, phase_model=10)
            coordinator = Coordinator(budget, 2, expected_model=20, expected_embedding=10)
            try:
                self.assertEqual(
                    json.loads(ledger.read_text())["counts"], {"model": 10, "embedding": 10}
                )
                clients = []
                for hook in coordinator.hooks:
                    clients.append(
                        RemoteBudget(hook.connection, hook.aborted, hook.messages, hook.credits)
                    )
                for client in clients:
                    for _ in range(5):
                        await client.before_model()
                    with self.assertRaises(ModelError):
                        await client.before_model()
                self.assertEqual(sum(client.issued["model"] for client in clients), 10)
                self.assertEqual(budget.counts["model"], 10)
            finally:
                coordinator.close()
                budget.close()

    async def test_worker_reservations_respect_phase_limit_and_stop_unused_credits(self):
        from tools.benchmark_workers import Coordinator, RemoteBudget

        with tempfile.TemporaryDirectory() as directory:
            budget = Budget(100, 100, Path(directory) / "budget.json", phase_model=10)
            coordinator = Coordinator(budget, 2)
            clients = []
            for hook in coordinator.hooks:
                clients.append(RemoteBudget(hook.connection, hook.aborted, hook.messages))
            try:
                clients[0].take("model")
                clients[1].take("model")
                self.assertEqual(budget.counts["model"], 10)
                with patch(
                    "tools.benchmark_workers.asyncio.to_thread",
                    side_effect=AssertionError("已预留额度不应再次切换线程"),
                ):
                    await clients[0].before_model()
                clients[1].take("model")
                with self.assertRaises(ModelError):
                    clients[1].take("model")
                coordinator.aborted.value = True
                with self.assertRaises(ModelError):
                    clients[0].take("model")
                self.assertEqual(clients[0].issued["model"], 2)
                self.assertEqual(budget.counts["model"], 10)
            finally:
                coordinator.close()
                budget.close()

    async def test_second_extension_and_phase_cap_preserve_global_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "budget.json"
            saved = {
                "limits": {"model": 6000, "embedding": 6000},
                "counts": {"model": 3749, "embedding": 3108},
            }
            ledger.write_text(json.dumps(saved))
            budget = Budget(9000, 9000, ledger, extend=True, phase_model=1, phase_embedding=2)
            try:
                budget.take("model")
                with self.assertRaises(ModelError):
                    budget.take("model")
                self.assertEqual(budget.counts["model"], 3750)
                self.assertEqual(
                    json.loads(ledger.with_suffix(".before-9000.json").read_text()), saved
                )
            finally:
                budget.close()
            resumed = Budget(9000, 9000, ledger)
            try:
                self.assertEqual(resumed.counts["model"], 3750)
            finally:
                resumed.close()

    async def test_extension_preserves_counts_and_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "budget.json"
            saved = {
                "limits": {"model": 2000, "embedding": 2000},
                "counts": {"model": 450, "embedding": 370},
            }
            ledger.write_text(json.dumps(saved))
            budget = Budget(6000, 6000, ledger, extend=True)
            try:
                self.assertEqual(budget.counts, saved["counts"])
                self.assertEqual(json.loads(ledger.read_text())["limits"], budget.limits)
                self.assertEqual(
                    json.loads(ledger.with_suffix(".before-extension.json").read_text()), saved
                )
            finally:
                budget.close()

    async def test_budget_resumes_without_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "budget.json"
            first = Budget(1, 1, ledger)
            first.take("model")
            with self.assertRaisesRegex(ValueError, "其他压测进程"):
                Budget(1, 1, ledger)
            first.close()
            resumed = Budget(1, 1, ledger)
            self.assertEqual(resumed.counts["model"], 1)
            with self.assertRaises(ModelError):
                resumed.take("model")
            resumed.close()
            with self.assertRaises(ValueError):
                Budget(2, 1, ledger)

    async def test_budget_is_atomic(self):
        budget = Budget(5, 3)

        def consume(_):
            try:
                budget.take("model")
                return True
            except ModelError:
                return False

        with ThreadPoolExecutor(8) as executor:
            results = list(executor.map(consume, range(20)))
        self.assertEqual(sum(results), 5)
        self.assertEqual(budget.counts["model"], 5)
        self.assertEqual(percentile([3, 1, 2], 0.5), 2)
        self.assertIsNone(percentile([], 0.95))
        self.assertEqual(percentile(list(range(1, 9)), 0.95), 8)

    async def test_measured_retrieval_uses_multigrain_production_path(self):
        match = {"source": "grep(1)", "chunk_id": "test", "text": "证据", "score": 0.1}
        coarse, fine = [match], [match]
        search = Mock(side_effect=[coarse, fine])
        batch_search = Mock(return_value=(coarse, fine))
        fusion = Mock(return_value=[match])
        index = SimpleNamespace(multigrain=True, select=Mock(), select_multigrain=fusion)
        store = SimpleNamespace(
            retrieval_strategy="context",
            search_index=index,
            prepare_search=Mock(),
            ensure_collection=Mock(),
            search=search,
            search_multigrain=batch_search,
        )
        embedding_call = Mock(return_value=[[1.0]])
        embedding = SimpleNamespace(embed=embedding_call)
        budget = Budget(10, 10)
        events = []

        def observe(stage, wait, duration):
            events.append({"request": "1", "stage": stage, "wait_s": wait, "value": duration})

        retrieve, _ = measured_retriever(embedding, store, budget, observe)
        self.addAsyncCleanup(retrieve.close)
        result = await retrieve("问题")
        embedding_call.assert_called_once_with(["问题"])
        self.assertEqual(budget.counts["embedding"], 1)
        batch_search.assert_called_once_with([1.0], 20)
        fusion.assert_called_once_with(coarse, fine, 5)
        self.assertIn("[1] grep(1)", result["text"])
        summary = stage_summary(events, {"1"})
        self.assertEqual(summary["qdrant"]["requests"], 1)
        self.assertEqual(sum(event["stage"] == "qdrant_query" for event in events), 1)
        self.assertIn("retrieval", summary)

    async def test_stage_totals_are_per_request_and_ignore_token_payload(self):
        events = [
            {"request": "1", "stage": "qdrant_verify", "wait_s": 0, "value": 1},
            {"request": "1", "stage": "qdrant_query", "wait_s": 0, "value": 2},
            {"request": "1", "stage": "qdrant_query", "wait_s": 0, "value": 3},
            {"request": "1", "stage": "model", "wait_s": 1, "value": 4},
            {"request": "1", "stage": "model", "wait_s": 2, "value": 5},
            {"request": "1", "stage": "tokens", "wait_s": 0, "value": {"input_tokens": 100}},
            {"request": "2", "stage": "model", "wait_s": 0, "value": 100},
        ]
        summary = stage_summary(events, {"1"})
        self.assertEqual(summary["qdrant"]["mean_s"], 6)
        self.assertEqual(summary["model"]["mean_s"], 9)
        self.assertEqual(summary["model_wait"]["mean_s"], 3)
        self.assertNotIn("tokens", summary)

    async def test_concurrency_and_metrics(self):
        active = 0
        maximum = 0
        events = []

        class Client:
            async def ainvoke(self, messages):
                nonlocal active, maximum
                active += 1
                maximum = max(maximum, active)
                await asyncio.sleep(0.01)
                active -= 1
                return AIMessage(content="完成")

        with patch.dict(
            os.environ,
            {
                "RAGENT_API_KEY": "test",
                "RAGENT_MODEL": "test",
                "RAGENT_BASE_URL": "https://example.com",
            },
        ):
            model = DeepSeekModel(2, observe=lambda *event: events.append(event))
        await model.close()
        self.enterContext(patch.object(model, "client", Client()))
        await asyncio.gather(*(model.ainvoke([HumanMessage(content="测试")], []) for _ in range(6)))
        self.assertEqual(maximum, 2)
        self.assertEqual(sum(event[0] == "model" for event in events), 6)
        with patch.dict(
            os.environ,
            {
                "RAGENT_API_KEY": "test",
                "RAGENT_MODEL": "test",
                "RAGENT_BASE_URL": "https://example.com",
            },
        ):
            with self.assertRaises(ValueError):
                DeepSeekModel(0)


class SocketProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_fragmented_response_and_invalid_frames(self):
        from tools.performance_common import HEADER, connect_socket, socket_exchange

        for response_id, payload in ((1, b"{}"), (2, b"{}"), (1, b"[]")):
            with self.subTest(response_id=response_id, payload=payload):

                async def peer(reader, writer):
                    try:
                        header = HEADER.unpack(await reader.readexactly(20))
                        await reader.readexactly(header[3])
                        response = HEADER.pack(0x52414754, 1, 201, len(payload), response_id)
                        writer.write(response[:7])
                        await writer.drain()
                        await asyncio.sleep(0.01)
                        writer.write(response[7:] + payload)
                        await writer.drain()
                    finally:
                        writer.close()
                        await writer.wait_closed()

                server = await asyncio.start_server(peer, "127.0.0.1", 0)
                async with server:
                    connection = await connect_socket(server.sockets[0].getsockname()[1])
                    try:
                        if response_id != 1 or payload != b"{}":
                            with self.assertRaises(ValueError):
                                await socket_exchange(connection, 200, 1, {})
                        else:
                            kind, body = await socket_exchange(connection, 200, 1, {})
                            self.assertEqual((kind, body), (201, {}))
                    finally:
                        connection.close()

    async def test_eof_before_complete_frame(self):
        from tools.performance_common import connect_socket, socket_exchange

        async def peer(reader, writer):
            await reader.readexactly(22)
            writer.write(b"RAG")
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(peer, "127.0.0.1", 0)
        async with server:
            connection = await connect_socket(server.sockets[0].getsockname()[1])
            try:
                with self.assertRaises(ConnectionError):
                    await socket_exchange(connection, 200, 1, {})
            finally:
                connection.close()
