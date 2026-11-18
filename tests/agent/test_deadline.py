"""验证 C 排队预算与 Python 剩余时限，不调用外部服务。"""

import asyncio
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from aiohttp.test_utils import TestClient, TestServer
from ragent.__main__ import create_app
from test_http import available_port

HEADER = struct.Struct(">IHHIQ")


class DeadlineHandler(BaseHTTPRequestHandler):
    requests = []

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).requests.append(data)
        time.sleep(0.35)
        body = b'{"text":"ok","sources":[]}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *args):
        pass


class DeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_python_caps_remaining_budget(self):
        class SlowModel:
            async def ainvoke(self, messages, allowed):
                await asyncio.sleep(1)

        async with TestClient(TestServer(create_app(model=SlowModel(), timeout=2))) as client:
            payload = {"request_id": "1", "mode": "chat", "query": "测试", "timeout_ms": 20}
            response = await client.post("/agent", json=payload)
            self.assertEqual((await response.json())["code"], "UPSTREAM_TIMEOUT")
            for invalid in (0, -1, True, "20", 600001):
                payload["timeout_ms"] = invalid
                response = await client.post("/agent", json=payload)
                self.assertEqual(response.status, 400)

    @unittest.skipUnless(os.environ.get("RAGENT_SERVER"), "需设置 C 服务路径")
    async def test_c_queue_deadline(self):
        await asyncio.to_thread(self.run_c_cases)

    def run_c_cases(self):
        http = ThreadingHTTPServer(("127.0.0.1", 0), DeadlineHandler)
        thread = threading.Thread(target=http.serve_forever)
        thread.start()
        try:
            for expired in (False, True):
                DeadlineHandler.requests = []
                port = available_port()
                config = {
                    "port": port,
                    "worker_threads": 1,
                    "agent_timeout_ms": 600,
                    "agent_url": f"http://127.0.0.1:{http.server_port}/agent",
                }
                server = Path(os.environ["RAGENT_SERVER"])
                env = dict(os.environ)
                if expired:
                    server = server.with_name("agent-server-test")
                    env["RAGENT_TEST_DELAY_MS"] = "700"
                    env["RAGENT_TEST_DELAY_FIRST_ONLY"] = "1"
                with (
                    tempfile.NamedTemporaryFile(mode="w+") as file,
                    tempfile.TemporaryFile() as log,
                ):
                    json.dump(config, file)
                    file.flush()
                    process = subprocess.Popen(
                        [str(server), "--config", file.name], env=env, stdout=log, stderr=log
                    )
                    try:
                        for _ in range(100):
                            try:
                                connection = socket.create_connection(
                                    ("127.0.0.1", port), timeout=2
                                )
                                break
                            except OSError:
                                if process.poll() is not None:
                                    log.seek(0)
                                    self.fail(log.read().decode())
                                time.sleep(0.02)
                        else:
                            self.fail("C 服务启动失败")
                        with connection, connection.makefile("rb") as stream:
                            body = b'{"query":"test"}'
                            frames = []
                            for request_id in (1, 2):
                                frames.append(
                                    HEADER.pack(0x52414754, 2, 1, len(body), request_id) + body
                                )
                            frames.append(HEADER.pack(0x52414754, 2, 200, 2, 3) + b"{}")
                            connection.sendall(b"".join(frames))
                            results = {}
                            for _ in range(3):
                                _, _, kind, size, request_id = HEADER.unpack(stream.read(20))
                                results[request_id] = (kind, json.loads(stream.read(size)))
                            self.assertEqual(results[3], (201, {}))
                            self.assertEqual(results[2][1]["code"], "UPSTREAM_TIMEOUT")
                            if expired:
                                self.assertEqual(DeadlineHandler.requests, [])
                                self.assertEqual(results[1][1]["code"], "UPSTREAM_TIMEOUT")
                            else:
                                self.assertEqual(results[1][0], 100)
                                first, second = DeadlineHandler.requests
                                self.assertLess(second["timeout_ms"], first["timeout_ms"])
                                self.assertLess(second["timeout_ms"], 400)
                    finally:
                        process.terminate()
                        process.wait(timeout=5)
        finally:
            http.shutdown()
            http.server_close()
            thread.join()
