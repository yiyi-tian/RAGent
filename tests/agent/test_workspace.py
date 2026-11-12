"""使用独立rootless Docker验证文件操作、会话与Agent闭环。"""

import asyncio
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
import uuid

from aiohttp.test_utils import TestServer
from aiohttp import ClientSession, TCPConnector
from unittest.mock import patch
from ragent.workers import WorkerGroup
from langchain_core.messages import AIMessage, HumanMessage

from ragent.__main__ import create_app
from ragent.agent import answer
from ragent.tools.commands import CommandError
from ragent.tools.workspace import SandboxExecutor, SessionContext


class FileModel:
    async def ainvoke(self, messages, allowed):
        if not any(isinstance(message, AIMessage) for message in messages):
            query = next(message for message in messages if isinstance(message, HumanMessage))
            calls = json.loads(str(query.content))
            for index, call in enumerate(calls):
                call["id"] = str(index)
            return AIMessage(content="", tool_calls=calls)
        return AIMessage(content=f"已根据实际执行结果完成 pid={os.getpid()}")


def use_file_model(app, retriever, model):
    import ragent.agent as agent_module

    replacement = patch.object(agent_module, "MockModel", FileModel)
    replacement.start()

    def close():
        replacement.stop()

    return close


@unittest.skipUnless(os.environ.get("RAGENT_TEST_SANDBOX"), "显式启用rootless沙箱集成测试")
class WorkspaceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state = Path(self.directory.name) / "state.json"
        self.executor = SandboxExecutor(
            "build/ragent-executor",
            "build/ragent-sandbox",
            "/run/user/1000/ragent-sandbox-docker.sock",
            str(self.state),
        )
        self.addAsyncCleanup(self.executor.close)
        await self.executor.probe()
        self.server_id = uuid.uuid4().hex
        self.sequence = 0
        self.snapshot = {
            "server_id": self.server_id,
            "pid": os.getpid(),
            "start": Path("/proc/self/stat").read_text().rsplit(")", 1)[1].split()[19],
            "boot": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "sequence": 1,
            "through": "8",
            "active": ["1", "2"],
        }
        await self.sync(["1", "2"])
        self.addAsyncCleanup(self.sync, [])

        async def heartbeat():
            while True:
                await asyncio.sleep(1)
                await self.sync(list(self.snapshot["active"]))

        task = asyncio.create_task(heartbeat())

        async def stop_heartbeat():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        self.addAsyncCleanup(stop_heartbeat)

    async def sync(self, active):
        self.sequence += 1
        self.snapshot["sequence"] = self.sequence
        self.snapshot["active"] = active
        await self.executor.sync(self.snapshot)

    async def file(self, name, args, cid="1"):
        return await self.executor.invoke_file(name, args, SessionContext(self.server_id, cid))

    async def test_crud_isolation_denials_and_cleanup(self):
        await self.file("workspace_mkdir", {"path": "notes"})
        await self.file("workspace_write", {"path": "notes/a", "content": "你好"})
        self.assertEqual((await self.file("workspace_read", {"path": "notes/a"}))["text"], "你好")
        await self.file("workspace_copy", {"source": "notes/a", "destination": "b"})
        await self.file("workspace_move", {"source": "b", "destination": "c"})
        for args, cid in (
            ({"path": "notes/a"}, "2"),
            ({"path": "../escape"}, "1"),
            ({"path": "/etc/passwd"}, "1"),
        ):
            with self.assertRaises(CommandError):
                await self.file("workspace_read", args, cid)
        with self.assertRaises(CommandError):
            await self.file("workspace_remove", {"path": "notes"})
        await self.file("workspace_remove", {"path": "c"})
        await self.file("workspace_remove", {"path": "notes/a"})
        await self.file("workspace_remove", {"path": "notes"})
        await self.sync([])
        self.assertEqual(json.loads(self.state.read_text())["sessions"], {})
        with self.assertRaises(CommandError):
            await self.file("workspace_list", {"path": "."})

    async def test_agent_order_and_modes(self):
        calls = [
            {"name": "workspace_mkdir", "args": {"path": "ordered"}},
            {"name": "workspace_write", "args": {"path": "ordered/a", "content": "ordered"}},
            {"name": "workspace_read", "args": {"path": "ordered/a"}},
        ]
        result = await answer(
            json.dumps(calls),
            "cmd",
            FileModel(),
            executor=self.executor,
            session=SessionContext(self.server_id, "1"),
            timeout=30,
        )
        self.assertEqual([a["status"] for a in result["actions"]], ["success"] * 3)
        for mode in ("chat", "rag"):

            async def retrieve(query):
                return {"text": "测试证据", "sources": []}

            denied = await answer(
                json.dumps(calls[:1]),
                mode,
                FileModel(),
                retriever=retrieve,
                executor=self.executor,
                session=SessionContext(self.server_id, "1"),
            )
            self.assertEqual(denied["actions"][-1]["status"], "failed")

    async def test_invalid_arguments_do_not_log_content(self):
        calls = [
            {
                "name": "workspace_write",
                "args": {"path": ["SECRET-DOCUMENT"], "content": "SECRET-DOCUMENT"},
            }
        ]
        with self.assertLogs("ragent.agent", level="WARNING") as logs:
            result = await answer(
                json.dumps(calls),
                "cmd",
                FileModel(),
                executor=self.executor,
                session=SessionContext(self.server_id, "1"),
            )
        self.assertEqual(result["actions"][0]["status"], "failed")
        self.assertNotIn("SECRET-DOCUMENT", "\n".join(logs.output))

    async def test_concurrent_clients_and_capacity(self):
        await self.sync([str(i) for i in range(1, 9)])
        # 4个执行槽共用状态文件，不同连接不会相互覆盖状态。
        results = await asyncio.gather(
            *(
                self.file("workspace_write", {"path": "same", "content": str(i)}, str(i))
                for i in range(1, 9)
            )
        )
        self.assertEqual(len(results), 8)
        for i in range(1, 9):
            self.assertEqual(
                (await self.file("workspace_read", {"path": "same"}, str(i)))["text"], str(i)
            )
        self.snapshot["through"] = "9"
        await self.sync([str(i) for i in range(1, 10)])
        with self.assertRaises(CommandError) as raised:
            await self.file("workspace_list", {"path": "."}, "9")
        self.assertEqual(raised.exception.code, "SERVER_BUSY")

    async def test_four_workers_share_workspace_and_limits(self):
        options = {
            "workers": 4,
            "model": "mock",
            "retrieval": "mock",
            "timeout": 30,
            "commands": "sandbox",
            "executor_path": "build/ragent-executor",
            "model_concurrency": 4,
            "retrieval_workers": 4,
        }
        group = WorkerGroup("127.0.0.1", 0, options, [use_file_model] * 4)
        try:
            with patch.dict(os.environ, {"RAGENT_SANDBOX_STATE": str(self.state)}):
                port = await asyncio.to_thread(group.start)
            pids = set()
            async with ClientSession(connector=TCPConnector(force_close=True)) as client:
                for index in range(12):
                    tool = "workspace_write" if index == 0 else "workspace_read"
                    args = {"path": "workers"}
                    if index == 0:
                        args["content"] = "shared"
                    body = {
                        "mode": "cmd",
                        "query": json.dumps([{"name": tool, "args": args}]),
                        "request_id": str(index + 1),
                        "session": {"server_id": self.server_id, "connection_id": "1"},
                    }
                    async with client.post(f"http://127.0.0.1:{port}/agent", json=body) as response:
                        result = await response.json()
                        self.assertEqual(response.status, 200)
                        self.assertEqual(result["actions"][0]["status"], "success")
                        pids.add(result["text"].rsplit("=", 1)[1])
            self.assertGreater(len(pids), 1)
        finally:
            await asyncio.to_thread(group.stop)

    async def test_execution_failure_closes_workspace_and_readonly_survives(self):
        await self.file("workspace_write", {"path": "private", "content": "SECRET-BODY"})
        name = next(iter(json.loads(self.state.read_text())["sessions"]))
        process = await asyncio.create_subprocess_exec(
            "docker",
            "--host",
            "unix://" + self.executor.socket,
            "pause",
            name,
            stdout=asyncio.subprocess.DEVNULL,
        )
        self.assertEqual(await process.wait(), 0)
        with self.assertRaises(CommandError) as raised:
            await self.file(
                "workspace_write", {"path": "private", "content": "changed", "overwrite": True}
            )
        self.assertEqual(raised.exception.effect, "unknown")
        with self.assertRaises(CommandError):
            await self.file("workspace_list", {"path": "."})
        self.assertIn("Mem:", (await self.executor.ainvoke("memory"))["text"])
        self.assertNotIn("SECRET-BODY", self.state.with_suffix(".json.audit").read_text())
        old = dict(self.snapshot, sequence=1, active=[])
        await self.executor.sync(old)
        self.assertIn(name, json.loads(self.state.read_text())["sessions"])

    async def test_tcp_session_is_assigned_and_disconnect_cleans(self):
        app = create_app(model=FileModel(), executor=self.executor)
        async with TestServer(app) as backend:
            config = Path(self.directory.name) / "agent.json"
            config.write_text(
                json.dumps(
                    {
                        "port": 0,
                        "worker_threads": 3,
                        "agent_url": str(backend.make_url("/agent")),
                        "agent_timeout_ms": 15000,
                    }
                )
            )
            server = await asyncio.create_subprocess_exec(
                "build/agent-server",
                "--config",
                str(config),
                "--sandbox-sessions",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            writers = []
            try:
                assert server.stdout is not None
                line = await asyncio.wait_for(server.stdout.readline(), 10)
                if not line:
                    assert server.stderr is not None
                    await server.wait()
                    self.fail(f"exit={server.returncode}: " + (await server.stderr.read()).decode())
                port = int(line.strip().rsplit(b":", 1)[1])

                async def request(reader, writer, calls, request_id):
                    payload = json.dumps(
                        {
                            "query": json.dumps(calls),
                            "session": {"server_id": self.server_id, "connection_id": "1"},
                        }
                    ).encode()
                    writer.write(struct.pack("!IHHIQ", 0x52414754, 1, 4, len(payload), request_id))
                    writer.write(payload)
                    await writer.drain()
                    header = await asyncio.wait_for(reader.readexactly(20), 20)
                    _, _, kind, size, identity = struct.unpack("!IHHIQ", header)
                    self.assertEqual((kind, identity), (100, request_id))
                    return json.loads(await reader.readexactly(size))

                first, first_writer = await asyncio.open_connection("127.0.0.1", port)
                writers.append(first_writer)
                second, second_writer = await asyncio.open_connection("127.0.0.1", port)
                writers.append(second_writer)
                created = await request(
                    first,
                    first_writer,
                    [
                        {
                            "name": "workspace_write",
                            "args": {"path": "private", "content": "connection one"},
                        }
                    ],
                    1,
                )
                self.assertEqual(created["actions"][0]["status"], "success")
                for reader, writer, status in (
                    (first, first_writer, "success"),
                    (second, second_writer, "failed"),
                ):
                    result = await request(
                        reader, writer, [{"name": "workspace_read", "args": {"path": "private"}}], 2
                    )
                    self.assertEqual(result["actions"][0]["status"], status)
                for writer in writers:
                    writer.close()
                    await writer.wait_closed()
                for _ in range(100):
                    await asyncio.sleep(0.1)
                    if not json.loads(self.state.read_text())["sessions"]:
                        break
                self.assertEqual(json.loads(self.state.read_text())["sessions"], {})
                third, third_writer = await asyncio.open_connection("127.0.0.1", port)
                writers.append(third_writer)
                result = await request(
                    third,
                    third_writer,
                    [
                        {
                            "name": "workspace_write",
                            "args": {"path": "after", "content": "kill-test"},
                        }
                    ],
                    1,
                )
                self.assertEqual(result["actions"][0]["status"], "success")
                server.kill()
                await server.wait()
                for _ in range(100):
                    await asyncio.sleep(0.1)
                    if not json.loads(self.state.read_text())["sessions"]:
                        break
                self.assertEqual(json.loads(self.state.read_text())["sessions"], {})
            finally:
                for writer in writers:
                    writer.close()
                if server.returncode is None:
                    server.terminate()
                await asyncio.wait_for(server.wait(), 10)


if __name__ == "__main__":
    unittest.main()
