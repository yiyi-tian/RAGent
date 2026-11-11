"""验证断连跳过排队调用并中止占用工作线程的HTTP传输。"""

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

HEADER = struct.Struct("!IHHIQ")


def receive(connection):
    def exact(size):
        data = bytearray()
        while len(data) < size:
            chunk = connection.recv(size - len(data))
            if not chunk:
                raise ConnectionError("响应中途断开")
            data.extend(chunk)
        return data

    header = HEADER.unpack(exact(20))
    return header, json.loads(exact(header[3]))


@unittest.skipUnless(os.environ.get("RAGENT_SERVER"), "需要C服务")
class DisconnectTests(unittest.TestCase):
    def test_queued_request_is_not_executed_after_disconnect(self):
        entered = threading.Event()
        release = threading.Event()
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append(data["query"])
                if data["query"] == "blocked":
                    entered.set()
                    release.wait(8)
                body = b'{"text":"ok","sources":[]}'
                try:
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    # 断连场景的预期结果，不影响检查后续请求是否被执行。
                    pass

            def log_message(self, *args):
                pass

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=upstream.serve_forever)
        thread.start()
        process = None
        try:
            with tempfile.TemporaryDirectory() as directory:
                config = Path(directory) / "server.json"
                config.write_text(
                    json.dumps(
                        {
                            "port": 0,
                            "worker_threads": 1,
                            "agent_url": f"http://127.0.0.1:{upstream.server_port}/agent",
                            "agent_timeout_ms": 10000,
                        }
                    )
                )
                with (Path(directory) / "server.log").open("w") as log:
                    process = subprocess.Popen(
                        [os.environ["RAGENT_SERVER"], "--config", str(config)],
                        stdout=subprocess.PIPE,
                        stderr=log,
                        text=True,
                    )
                    port = int(process.stdout.readline().strip().rsplit(":", 1)[1])
                    with socket.create_connection(("127.0.0.1", port), timeout=3) as first:
                        first.settimeout(3)
                        body = b'{"query":"blocked"}'
                        first.sendall(HEADER.pack(0x52414754, 1, 1, len(body), 1) + body)
                        self.assertTrue(entered.wait(3))
                        body = b'{"query":"must-not-run"}'
                        first.sendall(
                            HEADER.pack(0x52414754, 1, 1, len(body), 2)
                            + body
                            + HEADER.pack(0x52414754, 1, 200, 2, 3)
                            + b"{}"
                        )
                        self.assertEqual(receive(first)[0][2], 201)
                    started = time.monotonic()
                    with socket.create_connection(("127.0.0.1", port), timeout=3) as second:
                        second.settimeout(3)
                        body = b'{"query":"new"}'
                        second.sendall(HEADER.pack(0x52414754, 1, 1, len(body), 1) + body)
                        header, response = receive(second)
                        self.assertEqual(header[2], 100)
                        self.assertEqual(response["text"], "ok")
                    self.assertLess(time.monotonic() - started, 3)
                    self.assertEqual(requests, ["blocked", "new"])
        finally:
            release.set()
            if process is not None:
                process.terminate()
                process.communicate(timeout=5)
            upstream.shutdown()
            upstream.server_close()
            thread.join()
