"""C HTTP 适配器的网络失败和响应边界。"""

import json
import os
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from test_http import available_port


class Handler(BaseHTTPRequestHandler):
    payload = b"invalid"
    status = 200
    delay = 0

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        time.sleep(self.delay)
        self.send_response(self.status)
        self.send_header("Content-Length", str(len(self.payload)))
        self.end_headers()
        try:
            self.wfile.write(self.payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *args):
        pass


@unittest.skipUnless(os.environ.get("RAGENT_SERVER"), "需设置 RAGENT_SERVER")
class UpstreamTests(unittest.TestCase):
    def test_failure_mapping(self):
        http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=http.serve_forever)
        thread.start()
        try:
            cases = [
                (200, b"invalid", 0, "UPSTREAM_UNAVAILABLE"),
                (200, b"{}", 0, "UPSTREAM_UNAVAILABLE"),
                (500, b'{"code":"UNKNOWN","message":"error"}', 0, "UPSTREAM_UNAVAILABLE"),
                (200, b"x" * (1024 * 1024 + 1), 0, "UPSTREAM_UNAVAILABLE"),
                (503, b'{"code":"SERVER_BUSY","message":"busy"}', 0, "SERVER_BUSY"),
                (200, b"{}", 0.4, "UPSTREAM_TIMEOUT"),
            ]
            for status, payload, delay, expected in cases:
                Handler.status, Handler.payload, Handler.delay = status, payload, delay
                self.check_response(http.server_port, expected)
            self.check_response(available_port(), "UPSTREAM_UNAVAILABLE")
        finally:
            http.shutdown()
            http.server_close()
            thread.join()

    def check_response(self, upstream_port, expected):
        from test_http import available_port
        import struct

        port = available_port()
        with tempfile.NamedTemporaryFile(mode="w+") as config, tempfile.TemporaryFile() as log:
            json.dump(
                {
                    "port": port,
                    "agent_url": f"http://127.0.0.1:{upstream_port}/agent",
                    "agent_timeout_ms": 100,
                },
                config,
            )
            config.flush()
            process = subprocess.Popen(
                [os.environ["RAGENT_SERVER"], "--config", config.name], stdout=log, stderr=log
            )
            try:
                for _ in range(100):
                    try:
                        connection = socket.create_connection(("127.0.0.1", port), timeout=1)
                        break
                    except OSError:
                        if process.poll() is not None:
                            log.seek(0)
                            self.fail(log.read().decode())
                        time.sleep(0.02)
                else:
                    self.fail("C 服务启动超时")
                with connection, connection.makefile("rb") as stream:
                    body = b'{"query":"test"}'
                    connection.sendall(struct.pack(">IHHIQ", 0x52414754, 2, 1, len(body), 1) + body)
                    connection.sendall(struct.pack(">IHHIQ", 0x52414754, 2, 200, 2, 2) + b"{}")
                    responses = {}
                    for _ in range(2):
                        _, _, kind, size, request_id = struct.unpack(">IHHIQ", stream.read(20))
                        responses[request_id] = (kind, json.loads(stream.read(size)))
                    self.assertEqual(responses[1][1]["code"], expected)
                    self.assertEqual(responses[2], (201, {}))
            finally:
                process.terminate()
                process.wait(timeout=5)
