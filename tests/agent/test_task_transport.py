"""真实C前端与任务接口往返；命令提案不能在服务端执行。"""

import asyncio
import json
import os
import pty
from pathlib import Path
import struct
import tempfile
import unittest

from aiohttp.test_utils import TestServer
from langchain_core.messages import AIMessage
from ragent.__main__ import create_app


class ProposalModel:
    def __init__(self, command="touch MUST-NOT-EXECUTE"):
        self.calls = 0
        self.command = command
        self.results = []

    async def ainvoke(self, messages, allowed):
        self.calls += 1
        if self.calls == 1:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "run_command",
                        "args": {"command": self.command},
                        "id": "write",
                    }
                ],
            )
        self.results = list(messages)
        return AIMessage(content="根据本地结果完成")


@unittest.skipUnless(os.environ.get("RAGENT_SERVER"), "需构建C前端")
class TaskTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.model = ProposalModel()
        self.backend = TestServer(create_app(model=self.model))
        await self.backend.start_server()
        self.addAsyncCleanup(self.backend.close)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        config = Path(self.directory.name) / "agent.json"
        config.write_text(
            json.dumps(
                {"port": 0, "worker_threads": 3, "agent_url": str(self.backend.make_url("/tasks"))}
            )
        )
        self.server = await asyncio.create_subprocess_exec(
            os.environ["RAGENT_SERVER"],
            "--config",
            str(config),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self.addAsyncCleanup(self.stop_server)
        assert self.server.stdout is not None
        line = await asyncio.wait_for(self.server.stdout.readline(), 10)
        self.port = int(line.strip().rsplit(b":", 1)[1])
        self.reader, self.writer = await asyncio.open_connection("127.0.0.1", self.port)
        self.addAsyncCleanup(self.close_client)

    async def interactive_client(self, command, replies):
        self.model.command = command
        master, slave = pty.openpty()
        os.set_blocking(master, False)
        client = Path(os.environ["RAGENT_SERVER"]).with_name("agent")
        process = await asyncio.create_subprocess_exec(
            str(client),
            "--port",
            str(self.port),
            "--mode",
            "cmd",
            "--query",
            "测试命令",
            cwd=self.directory.name,
            stdin=slave,
            stdout=slave,
            stderr=slave,
        )
        os.close(slave)
        output = bytearray()
        cursor = 0

        async def read_until(expected):
            nonlocal cursor
            deadline = asyncio.get_running_loop().time() + 25
            while expected.encode() not in output[cursor:]:
                if asyncio.get_running_loop().time() >= deadline:
                    self.fail("未出现确认提示：" + output.decode(errors="replace"))
                try:
                    block = os.read(master, 65536)
                except BlockingIOError:
                    await asyncio.sleep(0.02)
                    continue
                if not block:
                    self.fail("客户端提前关闭")
                output.extend(block)
            cursor = output.index(expected.encode(), cursor) + len(expected.encode())

        try:
            for prompt, reply in replies:
                await read_until(prompt)
                os.write(master, reply.encode() + b"\n")
                # 同名提示可能重复；保留输出但后续等待从当前末尾开始。
                if prompt in ("凭证环境变量名：", "允许的地址："):
                    output.extend(b"\n")
            await asyncio.wait_for(process.wait(), 35)
            while True:
                try:
                    block = os.read(master, 65536)
                except (BlockingIOError, OSError):
                    break
                if not block:
                    break
                output.extend(block)
            self.assertEqual(process.returncode, 0, output.decode(errors="replace"))
            return output.decode(errors="replace")
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
            os.close(master)

    @unittest.skipUnless(os.environ.get("RAGENT_TEST_SANDBOX"), "显式启用本地TTY与容器验证")
    async def test_real_tty_rejection_stops_task(self):
        output = await self.interactive_client(
            "touch rejected-file",
            [
                ("执行时限秒", ""),
                ("是否执行？[是/否]", "否"),
            ],
        )
        self.assertIn("touch rejected-file", output)
        self.assertFalse((Path(self.directory.name) / "rejected-file").exists())
        self.assertEqual(self.model.calls, 1)

    @unittest.skipUnless(os.environ.get("RAGENT_TEST_SANDBOX"), "显式启用本地TTY与容器验证")
    async def test_real_tty_approved_write_persists(self):
        await self.interactive_client(
            "touch approved-file",
            [
                ("执行时限秒", ""),
                ("是否执行？[是/否]", "是"),
            ],
        )
        self.assertTrue((Path(self.directory.name) / "approved-file").exists())
        self.assertEqual(self.model.calls, 2)

    @unittest.skipUnless(os.environ.get("RAGENT_TEST_SANDBOX"), "显式启用本地TTY与容器验证")
    async def test_file_credential_snapshot_stays_local(self):
        credential = Path(self.directory.name) / ".env"
        credential.write_text("FILE-CREDENTIAL-SENTINEL")
        output = await self.interactive_client(
            "cat /run/ragent/credentials/token",
            [
                ("执行时限秒", ""),
                ("允许的地址：", ""),
                ("凭证环境变量名：", ""),
                ("本机凭证文件绝对路径", str(credential)),
                ("容器内文件名", "token"),
                ("本机凭证文件绝对路径", ""),
                ("是否执行？[是/否]", "是"),
            ],
        )
        self.assertIn("FILE-CREDENTIAL-SENTINEL", output)
        self.assertNotIn("FILE-CREDENTIAL-SENTINEL", str(self.model.results))
        self.assertEqual(credential.read_text(), "FILE-CREDENTIAL-SENTINEL")

    @unittest.skipUnless(os.environ.get("RAGENT_TEST_SANDBOX"), "显式启用本地TTY与容器验证")
    async def test_credential_output_stays_local(self):
        output = await self.interactive_client(
            "python3 -c \"import os; print(os.environ['TEST_TOKEN'])\"",
            [
                ("执行时限秒", ""),
                ("允许的地址：", ""),
                ("凭证环境变量名：", "TEST_TOKEN"),
                ("凭证值（隐藏输入）：", "LOCAL-ONLY-SENTINEL"),
                ("凭证环境变量名：", ""),
                ("本机凭证文件绝对路径", ""),
                ("是否执行？[是/否]", "是"),
            ],
        )
        self.assertIn("LOCAL-ONLY-SENTINEL", output)
        self.assertNotIn("LOCAL-ONLY-SENTINEL", str(self.model.results))
        self.assertIn("仅在客户端本地展示", str(self.model.results))

    async def close_client(self):
        self.writer.close()
        await self.writer.wait_closed()

    async def stop_server(self):
        if self.server.returncode is None:
            self.server.terminate()
        await asyncio.wait_for(self.server.wait(), 10)

    async def exchange(self, kind, identity, payload, reader=None, writer=None):
        reader = reader or self.reader
        writer = writer or self.writer
        body = json.dumps(payload).encode()
        writer.write(struct.pack("!IHHIQ", 0x52414754, 2, kind, len(body), identity) + body)
        await writer.drain()
        header = await asyncio.wait_for(reader.readexactly(20), 5)
        _, _, returned, size, returned_id = struct.unpack("!IHHIQ", header)
        self.assertEqual(identity, returned_id)
        return returned, json.loads(await reader.readexactly(size))

    async def start_task(self):
        kind, _ = await self.exchange(7, 1, {"name": "local-project"})
        self.assertEqual(kind, 103)
        kind, proposal = await self.exchange(
            3, 2, {"query": "创建文件", "task_state": {"rounds": -100}}
        )
        self.assertEqual(kind, 102)
        self.assertNotIn("state", proposal)
        self.assertNotIn("task_state", proposal)
        self.assertEqual(proposal["commands"][0]["command"], "touch MUST-NOT-EXECUTE")
        return proposal

    async def test_real_client_observes_its_own_directory(self):
        self.model.command = "pwd"
        client = Path(os.environ["RAGENT_SERVER"]).with_name("agent")
        process = await asyncio.create_subprocess_exec(
            str(client),
            "--host",
            "127.0.0.1",
            "--port",
            str(self.port),
            "--mode",
            "cmd",
            "--query",
            "查看工作目录",
            cwd=self.directory.name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        output, errors = await asyncio.wait_for(process.communicate(), 15)
        self.assertEqual(process.returncode, 0, errors.decode())
        self.assertIn("根据本地结果完成", output.decode())
        from langchain_core.messages import ToolMessage

        messages = [m for m in self.model.results if isinstance(m, ToolMessage)]
        self.assertIn(self.directory.name, messages[0].content)

    async def test_real_client_without_tty_does_not_write(self):
        client = Path(os.environ["RAGENT_SERVER"]).with_name("agent")
        process = await asyncio.create_subprocess_exec(
            str(client),
            "--host",
            "127.0.0.1",
            "--port",
            str(self.port),
            "--mode",
            "cmd",
            "--query",
            "创建文件",
            cwd=self.directory.name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, errors = await asyncio.wait_for(process.communicate(), 15)
        self.assertEqual(process.returncode, 3, errors.decode())
        self.assertEqual(self.model.calls, 1)
        self.assertFalse((Path(self.directory.name) / "MUST-NOT-EXECUTE").exists())

    async def test_results_and_one_time_continuation(self):
        proposal = await self.start_task()
        bad, _ = await self.exchange(5, 2, {"continuation": "forged", "results": []})
        self.assertEqual(bad, 101)
        kind, heartbeat = await self.exchange(8, 2, {"continuation": proposal["continuation"]})
        self.assertEqual(kind, 104)
        self.assertTrue(heartbeat["active"])
        payload = {
            "continuation": proposal["continuation"],
            "results": [
                {"id": "write", "status": "success", "text": "实际本地完成", "effect": "committed"}
            ],
        }
        kind, result = await self.exchange(5, 2, payload)
        self.assertEqual(kind, 100)
        self.assertEqual(result["actions"][0]["status"], "success")
        self.assertEqual(self.model.calls, 2)
        replay, _ = await self.exchange(5, 2, payload)
        self.assertEqual(replay, 101)
        self.assertEqual(self.model.calls, 2)

    async def test_cancel_does_not_reenter_model(self):
        proposal = await self.start_task()
        kind, result = await self.exchange(6, 2, {"continuation": proposal["continuation"]})
        self.assertEqual(kind, 100)
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(self.model.calls, 1)

    async def test_other_connection_cannot_use_continuation(self):
        proposal = await self.start_task()
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        try:
            kind, _ = await self.exchange(
                5, 2, {"continuation": proposal["continuation"], "results": []}, reader, writer
            )
            self.assertEqual(kind, 101)
            self.assertEqual(self.model.calls, 1)
        finally:
            writer.close()
            await writer.wait_closed()


if __name__ == "__main__":
    unittest.main()
