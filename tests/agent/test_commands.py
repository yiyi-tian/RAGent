"""真实C只读执行器与Agent工具闭环，不调用云端模型。"""

import asyncio
import json
import os
from pathlib import Path
import tempfile
import time
import struct
import unittest

from aiohttp.test_utils import TestServer

from langchain_core.messages import AIMessage, ToolMessage

from rag.runtime import REQUEST_STATE, RequestState
from ragent.__main__ import create_app
from ragent.agent import answer
from ragent.tools.commands import CommandError, ReadOnlyExecutor


class ToolModel:
    def __init__(self, target="memory", tools=None):
        self.target = target
        self.tools = tools or ["diagnose"]
        self.calls = 0
        self.results = []

    async def ainvoke(self, messages, allowed):
        self.calls += 1
        if self.calls == 1:
            calls = []
            for index, tool in enumerate(self.tools):
                arguments = {"target": self.target}
                if tool == "search_linux_manual":
                    arguments = {"query": "Linux内存"}
                calls.append({"name": tool, "args": arguments, "id": str(index)})
            return AIMessage(content="", tool_calls=calls)
        self.results = [message for message in messages if isinstance(message, ToolMessage)]
        return AIMessage(content="依据实际工具结果完成说明")


@unittest.skipUnless(os.environ.get("RAGENT_EXECUTOR"), "需先构建C执行器")
class CommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.executor = ReadOnlyExecutor(os.environ["RAGENT_EXECUTOR"])
        self.addAsyncCleanup(self.executor.close)

    async def test_real_memory_disk_and_processes(self):
        for target, heading in (("memory", "Mem:"), ("disk", "Filesystem"), ("processes", "PID")):
            result = await self.executor.ainvoke(target)
            self.assertIn(heading, result["text"])
            self.assertNotIn("模拟", result["summary"])
        self.assertEqual(self.executor.processes, set())

    async def test_cmd_and_agent_preserve_real_tool_messages(self):
        for mode in ("cmd", "agent"):
            model = ToolModel()
            result = await answer("查看实际内存", mode, model, executor=self.executor)
            self.assertEqual(result["actions"][0]["status"], "success")
            self.assertIn("Mem:", model.results[0].content)
            self.assertEqual(model.results[0].tool_call_id, "0")
            self.assertEqual(result["sources"], [])

    async def test_rejected_operations_are_failed_and_other_tools_survive(self):
        for target in ("cat", "/etc/passwd", "memory;touch /tmp/denied", "memory\x00disk"):
            with self.assertRaises(CommandError) as raised:
                await self.executor.ainvoke(target)
            self.assertEqual(raised.exception.code, "COMMAND_NOT_ALLOWED")

        async def retrieve(query):
            return {"text": "真实检索接口的受控证据", "sources": []}

        model = ToolModel("not_allowed", ["diagnose", "search_linux_manual"])
        result = await answer(
            "检索并诊断", "agent", model, retriever=retrieve, executor=self.executor
        )
        self.assertCountEqual([a["status"] for a in result["actions"]], ["failed", "success"])
        self.assertIn("COMMAND_NOT_ALLOWED", model.results[0].content)

    async def test_chat_and_rag_cannot_invoke_diagnose(self):
        async def retrieve(query):
            return {"text": "受控证据", "sources": []}

        for mode in ("chat", "rag"):
            result = await answer(
                "查看内存", mode, ToolModel(), retriever=retrieve, executor=self.executor
            )
            failed = [a for a in result["actions"] if a["tool"] == "diagnose"]
            self.assertEqual(failed[0]["status"], "failed")
            self.assertEqual(self.executor.processes, set())

    @unittest.skipUnless(os.environ.get("RAGENT_SERVER"), "需先构建C网络服务")
    async def test_tcp_http_executor_roundtrip(self):
        for kind in (3, 4):
            model = ToolModel()
            app = create_app(model=model, executor=self.executor)
            async with TestServer(app) as backend:
                with tempfile.TemporaryDirectory() as directory:
                    config = Path(directory) / "agent.json"
                    config.write_text(
                        json.dumps(
                            {
                                "port": 0,
                                "worker_threads": 1,
                                "agent_url": str(backend.make_url("/agent")),
                                "agent_timeout_ms": 5000,
                            }
                        )
                    )
                    server = await asyncio.create_subprocess_exec(
                        os.environ["RAGENT_SERVER"],
                        "--config",
                        str(config),
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    try:
                        assert server.stdout is not None
                        line = await asyncio.wait_for(server.stdout.readline(), 5)
                        port = int(line.strip().rsplit(b":", 1)[1])
                        reader, writer = await asyncio.open_connection("127.0.0.1", port)
                        try:
                            body = json.dumps({"query": "查看当前实际内存"}).encode()
                            writer.write(struct.pack("!IHHIQ", 0x52414754, 2, kind, len(body), 1))
                            writer.write(body)
                            await writer.drain()
                            header = await asyncio.wait_for(reader.readexactly(20), 5)
                            _, _, response_kind, size, identity = struct.unpack("!IHHIQ", header)
                            payload = json.loads(await reader.readexactly(size))
                            self.assertEqual((response_kind, identity), (100, 1))
                            self.assertEqual(payload["actions"][0]["status"], "success")
                            self.assertIn("Mem:", model.results[0].content)
                        finally:
                            writer.close()
                            await writer.wait_closed()
                    finally:
                        if server.returncode is None:
                            server.terminate()
                        await asyncio.wait_for(server.wait(), 5)

    async def test_direct_executor_rejects_malformed_request(self):
        for request in ({"target": "memory"}, {"target": "memory", "timeout_ms": True}):
            process = await asyncio.create_subprocess_exec(
                self.executor.path, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE
            )
            output, _ = await process.communicate(json.dumps(request).encode())
            self.assertFalse(json.loads(output)["ok"])


class ExecutorBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def script(self, directory, text):
        path = Path(directory) / "executor"
        path.write_text("#!/usr/bin/python3\n" + text)
        path.chmod(0o700)
        return str(path)

    async def test_invalid_response_and_nonzero_exit(self):
        for script in (
            "print('not-json')\n",
            "print('{}')\n",
            "raise SystemExit(7)\n",
            "print('x' * (128 * 1024 + 1))\n",
        ):
            with tempfile.TemporaryDirectory() as directory:
                executor = ReadOnlyExecutor(self.script(directory, script))
                with self.assertRaises(CommandError) as raised:
                    await executor.ainvoke("memory")
                self.assertEqual(raised.exception.code, "COMMAND_FAILED")
                await executor.close()

    async def test_close_cancels_active_and_queued_invocations(self):
        with tempfile.TemporaryDirectory() as directory:
            executor = ReadOnlyExecutor(self.script(directory, "import time\ntime.sleep(8)\n"), 1)
            tasks = [asyncio.create_task(executor.ainvoke("memory")) for _ in range(3)]
            for _ in range(100):
                if executor.processes:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(len(executor.processes), 1)
            await executor.close()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            self.assertTrue(all(isinstance(result, asyncio.CancelledError) for result in results))
            self.assertEqual(executor.processes, set())
            self.assertEqual(executor.tasks, set())
            with self.assertRaises(CommandError):
                await executor.ainvoke("memory")

    async def test_deadline_and_unresponsive_executor_are_reaped(self):
        script = (
            "import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(8)\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            executor = ReadOnlyExecutor(self.script(directory, script))
            token = REQUEST_STATE.set(RequestState(time.monotonic() + 0.05))
            try:
                with self.assertRaises(CommandError) as raised:
                    await executor.ainvoke("memory")
                self.assertEqual(raised.exception.code, "COMMAND_TIMEOUT")
            finally:
                REQUEST_STATE.reset(token)
                await executor.close()
            self.assertEqual(executor.processes, set())

    async def test_ready_detects_removed_executor_without_running_it(self):
        with tempfile.TemporaryDirectory() as directory:
            executor = ReadOnlyExecutor(
                self.script(directory, "raise AssertionError('不可执行')\n")
            )
            async with TestServer(create_app(executor=executor)) as server:
                Path(executor.path).unlink()
                from aiohttp import ClientSession

                async with (
                    ClientSession() as session,
                    session.get(server.make_url("/ready")) as response,
                ):
                    self.assertEqual(response.status, 503)
                    self.assertEqual((await response.json())["commands"], "unavailable")
            await executor.close()

    async def test_cancellation_closes_process_and_releases_slot(self):
        with tempfile.TemporaryDirectory() as directory:
            executor = ReadOnlyExecutor(self.script(directory, "import time\ntime.sleep(8)\n"), 1)
            task = asyncio.create_task(executor.ainvoke("memory"))
            for _ in range(100):
                if executor.processes:
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(executor.processes)
            process = next(iter(executor.processes))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertIsNotNone(process.returncode)
            self.assertEqual(executor.processes, set())
            await executor.close()
