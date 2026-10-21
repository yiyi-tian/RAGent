"""第一阶段黑盒验收：独立构造线上帧，不复用客户端或服务器编解码实现。"""

import concurrent.futures
import errno
import json
import os
import select
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest

SERVER, CLIENT = map(os.path.abspath, sys.argv[1:3])
del sys.argv[1:3]
MAGIC = 0x52414754
LIMIT = 1024 * 1024
HEADER = struct.Struct("!IHHIQ")


def dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


def frame(kind=1, request_id=1, body=None):
    if body is None:
        body = dump({"query": "hello"})
    return HEADER.pack(MAGIC, 1, kind, len(body), request_id) + body


def receive_exact(connection, length):
    data = bytearray()
    while len(data) < length:
        chunk = connection.recv(length - len(data))
        if not chunk:
            raise AssertionError("响应尚未接收完整，连接已经关闭")
        data.extend(chunk)
    return bytes(data)


def receive(connection):
    magic, version, kind, length, request_id = HEADER.unpack(receive_exact(connection, 20))
    assert magic == MAGIC and version == 1 and length <= LIMIT
    return kind, request_id, json.loads(receive_exact(connection, length))


class RunningServer:
    def __init__(self, *arguments):
        self.log = tempfile.TemporaryFile(mode="w+")
        self.process = subprocess.Popen(
            [SERVER, "--port", "0", *arguments], stdout=subprocess.PIPE,
            stderr=self.log, text=True,
        )
        if not select.select([self.process.stdout], [], [], 10)[0]:
            self.close()
            raise AssertionError("服务器启动超时")
        line = self.process.stdout.readline().strip()
        if not line.startswith("Listening on 127.0.0.1:"):
            self.close()
            raise AssertionError(f"服务器启动失败: {line}")
        self.port = int(line.rsplit(":", 1)[1])

    def connect(self):
        connection = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        connection.settimeout(5)
        return connection

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
                raise AssertionError("服务器未正常停止")
        self.process.stdout.close()
        self.log.close()


class NetworkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = RunningServer()

    @classmethod
    def tearDownClass(cls):
        cls.server.close()
        assert cls.server.process.returncode == 0

    def client(self, *arguments, input=None, timeout=10):
        return subprocess.run(
            [CLIENT, "--port", str(self.server.port), *arguments], input=input,
            text=True, capture_output=True, timeout=timeout,
        )

    def assert_closed(self, connection):
        try:
            self.assertEqual(connection.recv(1), b"")
        except ConnectionResetError:
            pass

    def test_wire_example_and_64_bit_id(self):
        expected = bytes.fromhex("5241475400010001000000110000000000000001")
        self.assertEqual(frame()[:20], expected)
        with self.server.connect() as connection:
            request_id = 0x12345678ABCDEF01
            connection.sendall(frame(request_id=request_id))
            self.assertEqual(receive(connection), (100, request_id, {"text": "server received: hello", "sources": []}))

    def test_modes_unicode_and_unknown_fields(self):
        with self.server.connect() as connection:
            for kind in (1, 2, 3, 4):
                connection.sendall(frame(kind, kind, dump({"query": "  你好，epoll！\n", "extra": [1, 2]})))
                self.assertEqual(receive(connection), (100, kind, {"text": "server received:   你好，epoll！\n", "sources": []}))

    def test_fragmented_frames(self):
        data = frame()
        with self.server.connect() as connection:
            for request_id, cut in enumerate(range(1, len(data)), start=1):
                data = frame(request_id=request_id)
                connection.sendall(data[:cut])
                # 确认服务器没有在半帧时提前回复。
                self.assertFalse(select.select([connection], [], [], 0.002)[0])
                connection.sendall(data[cut:])
                self.assertEqual(receive(connection)[0:2], (100, request_id))

    def test_coalesced_frames(self):
        with self.server.connect() as connection:
            connection.sendall(b"".join(frame(request_id=i) for i in range(1, 101)))
            results = [receive(connection) for _ in range(100)]
            self.assertEqual({result[1] for result in results}, set(range(1, 101)))
            self.assertTrue(all(result[0] == 100 for result in results))

    def test_ping(self):
        with self.server.connect() as connection:
            connection.sendall(frame(200, 1, b"{}"))
            self.assertEqual(receive(connection), (201, 1, {}))

    def test_invalid_bodies_keep_connection(self):
        invalid = [b"", b"[]", b"null", b"{}", b'{"query":2}',
                   b'{"query":""}', b'{"query":" \\t\\n\\r"}',
                   b'{"query":"\xff"}', b'\xef\xbb\xbf{"query":"hello"}',
                   b'{"query":"hello"}trailing', b'{"query":']
        with self.server.connect() as connection:
            for request_id, body in enumerate(invalid, start=1):
                connection.sendall(frame(request_id=request_id, body=body))
                kind, response_id, result = receive(connection)
                self.assertEqual((kind, response_id, result["code"]), (101, request_id, "INVALID_REQUEST"))
            connection.sendall(frame(request_id=100))
            self.assertEqual(receive(connection)[0:2], (100, 100))

    def test_unknown_and_wrong_direction_types(self):
        with self.server.connect() as connection:
            for request_id, kind in enumerate((0, 99, 100, 101, 201, 65535), start=1):
                connection.sendall(frame(kind, request_id))
                self.assertEqual(receive(connection)[2]["code"], "UNSUPPORTED_MESSAGE_TYPE")

    def test_invalid_headers_close_immediately(self):
        headers = [(0, 1, 1, 0, 1), (MAGIC, 2, 1, 0, 1),
                   (MAGIC, 1, 1, LIMIT + 1, 1)]
        for header in headers:
            with self.subTest(header=header), self.server.connect() as connection:
                connection.sendall(HEADER.pack(*header))
                self.assert_closed(connection)

    def test_zero_duplicate_and_decreasing_ids(self):
        with self.server.connect() as connection:
            connection.sendall(frame(request_id=0))
            self.assert_closed(connection)
        for second_id in (1, 2):
            with self.server.connect() as connection:
                connection.sendall(frame(request_id=2))
                receive(connection)
                connection.sendall(frame(request_id=second_id))
                self.assert_closed(connection)

    def test_body_size_boundary(self):
        body = dump({"query": "hello", "padding": ""})
        body = dump({"query": "hello", "padding": "x" * (LIMIT - len(body))})
        self.assertEqual(len(body), LIMIT)
        with self.server.connect() as connection:
            connection.sendall(frame(body=body))
            self.assertEqual(receive(connection)[0], 100)

    def test_response_size_boundary_and_overflow(self):
        overhead = len(dump({"text": "server received: ", "sources": []}))
        with self.server.connect() as connection:
            for request_id, extra in ((1, 0), (2, 1)):
                query = "x" * (LIMIT - overhead + extra)
                connection.sendall(frame(request_id=request_id, body=dump({"query": query})))
                kind, _, result = receive(connection)
                if extra == 0:
                    self.assertEqual(kind, 100)
                    self.assertEqual(result["text"], "server received: " + query)
                    self.assertEqual(len(dump(result)), LIMIT)
                else:
                    self.assertEqual((kind, result["code"]), (101, "INTERNAL_ERROR"))

    def test_ten_clients_connected_at_once(self):
        barrier = threading.Barrier(10)

        def run(index):
            with self.server.connect() as connection:
                barrier.wait(timeout=5)
                for request_id in range(1, 11):
                    query = f"client-{index}-request-{request_id}"
                    connection.sendall(frame((index % 3) + 1, request_id, dump({"query": query})))
                    self.assertEqual(receive(connection), (100, request_id, {"text": "server received: " + query, "sources": []}))

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            list(executor.map(run, range(10)))

    def test_disconnect_mid_header_and_body(self):
        for cut in (5, 25):
            with self.server.connect() as connection:
                connection.sendall(frame()[:cut])
        with self.server.connect() as connection:
            connection.sendall(frame())
            self.assertEqual(receive(connection)[0], 100)

    def test_slow_reader_large_response(self):
        query = "x" * 400000
        with self.server.connect() as connection:
            connection.sendall(frame(body=dump({"query": query})))
            # 服务端每轮最多写64 KiB；大响应需要保留偏移并重复处理EPOLLOUT。
            time.sleep(0.03)
            self.assertEqual(receive(connection)[2]["text"], "server received: " + query)

    def test_client_single_request_modes(self):
        for mode in ("agent", "chat", "rag", "cmd"):
            result = self.client("--mode", mode, "--query", "你好")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "server received: 你好")
        result = self.client("--mode", "chat", "--query", "--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "server received: --help")

    def test_client_repl(self):
        script = "普通输入\n/help\n/chat\nfirst\n/rag one-shot\nsecond\n/back\n/cmd\n/list\nthird\n/back\n/status\n/exit\n"
        result = self.client(input=script)
        self.assertEqual(result.returncode, 0, result.stderr)
        for query in ("普通输入", "first", "one-shot", "second", "third"):
            self.assertIn("server received: " + query, result.stdout)
        self.assertIn("默认使用 Agent 模式", result.stdout)
        self.assertIn("agent>", result.stdout)
        self.assertIn("chat>", result.stdout)
        self.assertIn("未检查依赖服务", result.stdout)

    def test_agent_is_default_repl_mode(self):
        result = self.client(input="问题直接发给Agent\n/exit\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("server received: 问题直接发给Agent", result.stdout)

    def test_client_usage_errors(self):
        for args in (("--mode", "rag"), ("--query", "q"), ("--port", "-1"),
                     ("--mode", "unknown", "--query", "q"), ("--mode", "chat", "--query", "  ")):
            self.assertEqual(self.client(*args).returncode, 2)

    def test_send_buffer_limit_and_server_survival(self):
        server = RunningServer("--max-pending-bytes", str(LIMIT + 20))
        try:
            with server.connect() as connection:
                request = dump({"query": "x" * 1000})

                def flood():
                    try:
                        connection.sendall(b"".join(frame(request_id=i, body=request) for i in range(1, 15001)))
                    except (OSError, TimeoutError):
                        pass

                sender = threading.Thread(target=flood, daemon=True)
                sender.start()
                # 不读取响应，让内核发送缓冲耗尽，再触发用户态缓冲上限。
                expires = time.monotonic() + 8
                reason = "发送缓冲达到上限".encode()
                while time.monotonic() < expires:
                    if reason in os.pread(server.log.fileno(), 4 * 1024 * 1024, 0):
                        break
                    time.sleep(0.02)
                else:
                    self.fail("慢客户端未触发发送缓冲上限")
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError as error:
                    if error.errno != errno.ENOTCONN:
                        raise
                sender.join(timeout=2)
                self.assertFalse(sender.is_alive())
            with server.connect() as connection:
                connection.sendall(frame())
                self.assertEqual(receive(connection)[0], 100)
        finally:
            server.close()

    def test_server_signal_shutdown_with_connections(self):
        server = RunningServer()
        try:
            with server.connect():
                server.process.send_signal(signal.SIGINT)
                self.assertEqual(server.process.wait(timeout=5), 0)
        finally:
            server.close()

    def test_connection_limit(self):
        server = RunningServer("--max-connections", "1")
        try:
            with server.connect() as first:
                first.sendall(frame())
                receive(first)
                with server.connect() as second:
                    self.assert_closed(second)
                first.sendall(frame(request_id=2))
                self.assertEqual(receive(first)[0:2], (100, 2))
        finally:
            server.close()

    def test_config_and_command_line_override(self):
        with tempfile.NamedTemporaryFile(mode="w+", suffix=".json") as config:
            json.dump({"host": "127.0.0.1", "port": 9000, "max_connections": 1,
                       "max_pending_bytes": 2097152}, config)
            config.flush()
            # RunningServer的--port 0在--config之前，仍然应覆盖配置中的9000。
            server = RunningServer("--config", config.name)
            try:
                with server.connect() as connection:
                    connection.sendall(frame())
                    self.assertEqual(receive(connection)[0], 100)
            finally:
                server.close()

    def test_client_interrupt_while_waiting_for_input(self):
        process = subprocess.Popen([CLIENT, "--port", str(self.server.port)],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)
        try:
            self.assertTrue(select.select([process.stdout], [], [], 5)[0])
            process.send_signal(signal.SIGINT)
            process.communicate(timeout=5)
            self.assertEqual(process.returncode, 0)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()


class ClientPeerTests(unittest.TestCase):
    def run_peer(self, response, timeout_ms=500, mode="chat", expected_type=1):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            process = subprocess.Popen(
                [CLIENT, "--port", str(listener.getsockname()[1]), "--timeout-ms", str(timeout_ms),
                 "--mode", mode, "--query", "hello"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            listener.settimeout(5)
            with listener.accept()[0] as connection:
                connection.settimeout(5)
                self.assertEqual(receive(connection)[0:2], (expected_type, 1))
                if response is not None:
                    # 按字节发送，验证客户端也处理半包。
                    for byte in response:
                        try:
                            connection.sendall(bytes([byte]))
                        except (BrokenPipeError, ConnectionResetError):
                            break
                stdout, stderr = process.communicate(timeout=5)
            return process.returncode, stdout, stderr

    def test_fragmented_response(self):
        result = self.run_peer(frame(100, 1, dump({"text": "answer", "sources": []})))
        self.assertEqual(result[:2], (0, "answer\n"))

    def test_agent_client_uses_agent_request_type(self):
        result = self.run_peer(frame(100, 1, dump({"text": "answer", "sources": []})),
                               mode="agent", expected_type=4)
        self.assertEqual(result[:2], (0, "answer\n"))

    def test_default_repl_sends_agent_request_type(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            process = subprocess.Popen(
                [CLIENT, "--port", str(listener.getsockname()[1])],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            try:
                listener.settimeout(5)
                with listener.accept()[0] as connection:
                    process.stdin.write("直接提问\n")
                    process.stdin.flush()
                    kind, request_id, body = receive(connection)
                    self.assertEqual((kind, request_id, body),
                                     (4, 1, {"query": "直接提问"}))
                    connection.sendall(frame(100, 1, dump({"text": "answer", "sources": []})))
                    process.stdin.write("/exit\n")
                    process.stdin.flush()
                stdout, stderr = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 0, stderr)
                self.assertIn("answer", stdout)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()

    def test_remote_error_exit_code(self):
        result = self.run_peer(frame(101, 1, dump({"code": "INVALID_REQUEST", "message": "失败"})))
        self.assertEqual(result[0], 3)
        self.assertIn("INVALID_REQUEST", result[2])

    def test_wrong_id_and_type(self):
        for reply in (frame(100, 2, dump({"text": "answer", "sources": []})), frame(1, 1)):
            self.assertEqual(self.run_peer(reply)[0], 1)

    def test_timeout_without_retry(self):
        result = self.run_peer(None, timeout_ms=100)
        self.assertEqual(result[0], 1)
        self.assertIn("不自动重试", result[2])


if __name__ == "__main__":
    unittest.main(verbosity=2)
