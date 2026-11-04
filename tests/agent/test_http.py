"""独立启动服务验证真实 TCP 到 HTTP 链路。"""

import json
import os
import socket
import struct
import subprocess
import sys
import time
import tempfile
import unittest


def available_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


class Integration(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("RAGENT_SERVER"), "需设置 RAGENT_SERVER")
    def test_round_trip(self):
        python_port = available_port()
        server_port = available_port()
        log = tempfile.TemporaryFile()
        config = tempfile.NamedTemporaryFile(mode="w+", suffix=".json")
        json.dump(
            {"port": server_port, "agent_url": f"http://127.0.0.1:{python_port}/agent"}, config
        )
        config.flush()
        python = subprocess.Popen(
            [sys.executable, "-m", "ragent", "--port", str(python_port)], stdout=log, stderr=log
        )
        server = None
        try:
            for _ in range(300):
                try:
                    with socket.create_connection(("127.0.0.1", python_port), timeout=0.1):
                        break
                except OSError:
                    if python.poll() is not None:
                        log.seek(0)
                        self.fail(log.read().decode(errors="replace"))
                    time.sleep(0.05)
            else:
                log.seek(0)
                self.fail("Python Agent 启动超时：" + log.read().decode(errors="replace"))
            server = subprocess.Popen(
                [os.environ["RAGENT_SERVER"], "--config", config.name],
                stdout=log,
                stderr=log,
            )
            for _ in range(300):
                try:
                    connection = socket.create_connection(("127.0.0.1", server_port), timeout=2)
                    break
                except OSError:
                    time.sleep(0.05)
            else:
                self.fail("C 服务启动超时")
            if python.poll() is not None or server.poll() is not None:
                log.seek(0)
                self.fail(log.read().decode(errors="replace"))
            with connection:
                stream = connection.makefile("rb")
                for mode in range(1, 5):
                    body = json.dumps({"query": "测试"}).encode()
                    connection.sendall(
                        struct.pack(">IHHIQ", 0x52414754, 1, mode, len(body), mode) + body
                    )
                    _, _, kind, size, request_id = struct.unpack(">IHHIQ", stream.read(20))
                    payload = json.loads(stream.read(size))
                    self.assertEqual((kind, request_id), (100, mode))
                    self.assertIn("actions", payload)
        finally:
            for process in (server, python):
                if process:
                    process.terminate()
                    process.wait(timeout=15)
            config.close()
            log.close()
