"""独立模拟TCP对端，核验C压测客户端的帧边界与失败处理。"""

import json
import socket
import struct
import subprocess
import sys
import threading
import time
import unittest

PROGRAM = sys.argv.pop(1)
HEADER = struct.Struct("!IHHIQ")


def receive_exact(connection, length):
    data = bytearray()
    while len(data) < length:
        part = connection.recv(length - len(data))
        if not part:
            raise ConnectionError("请求帧未接收完整")
        data.extend(part)
    return data


class NativeProbeTests(unittest.TestCase):
    def run_peer(self, failure=None):
        errors = []
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            listener.settimeout(10)

            def serve():
                try:
                    connection, _ = listener.accept()
                    with connection:
                        connection.settimeout(10)
                        for identity in (1, 2, 3):
                            header = HEADER.unpack(receive_exact(connection, 20))
                            if failure == "error_json" or (
                                failure and failure.startswith("business-")
                            ):
                                expected_kind = 4 if failure == "business-agent" else 2
                                self.assertEqual(
                                    header, (0x52414754, 2, expected_kind, 16, identity)
                                )
                                self.assertEqual(
                                    json.loads(receive_exact(connection, header[3])),
                                    {"query": "test"},
                                )
                            else:
                                self.assertEqual(header, (0x52414754, 2, 200, 2, identity))
                                self.assertEqual(receive_exact(connection, 2), b"{}")
                            returned = identity + 1 if failure == "id" else identity
                            payload = b"[]" if failure == "body" else b"{}"
                            kind = 201
                            if failure and failure.startswith("business-"):
                                kind = 100
                                data = {
                                    "text": "完整回答",
                                    "sources": [{"source": "fixture"}],
                                    "actions": [{"status": "success"}],
                                }
                                if failure == "business-partial":
                                    data["actions"] = [{"status": "failed"}]
                                if failure == "business-empty":
                                    data["sources"] = []
                                if failure in ("business-error", "business-false-ok"):
                                    kind = 101
                                    data = {
                                        "code": (
                                            "UPSTREAM_TIMEOUT"
                                            if failure == "business-error"
                                            else "OK"
                                        )
                                    }
                                payload = json.dumps(data).encode()
                            if failure == "error_json":
                                kind = 101
                                payload = b'{"code":"SERVER_BUSY"} trailing'
                            response = (
                                HEADER.pack(0x52414754, 2, kind, len(payload), returned) + payload
                            )
                            if failure == "eof":
                                connection.sendall(response[:9])
                                return
                            for part in (response[:7], response[7:20], response[20:]):
                                connection.sendall(part)
                                time.sleep(0.01)
                            if failure:
                                return
                except Exception as error:
                    errors.append(error)

            thread = threading.Thread(target=serve)
            thread.start()
            mode = "ping"
            if failure == "error_json":
                mode = "overload"
            elif failure and failure.startswith("business-"):
                mode = "business"
            settings = {
                "port": listener.getsockname()[1],
                "count": 1,
                "mode": mode,
                "query": "test",
            }
            if failure and failure.startswith("business-"):
                settings["requests"] = [
                    {"kind": 4 if failure == "business-agent" else 2, "query": "test"}
                ]
            result = subprocess.run(
                [PROGRAM], input=json.dumps(settings), text=True, capture_output=True, timeout=15
            )
            thread.join(timeout=12)
            self.assertFalse(thread.is_alive())
            self.assertFalse(errors, errors)
            return result

    def test_fragmented_frames_and_increasing_ids(self):
        result = self.run_peer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["ping_success"], 3)

    def test_invalid_response_and_eof_fail(self):
        for failure in ("id", "body", "eof", "error_json"):
            with self.subTest(failure=failure):
                result = self.run_peer(failure)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")

    def test_parallel_overload_aggregates_original_samples(self):
        errors = []
        workers = []
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            listener.settimeout(10)

            def handle(connection):
                try:
                    with connection:
                        connection.settimeout(10)
                        header = HEADER.unpack(receive_exact(connection, 20))
                        body = json.loads(receive_exact(connection, header[3]))
                        if header[2] == 2:
                            self.assertEqual(body, {"query": "test"})
                            time.sleep(0.01 * header[4])
                            kind = 101
                            payload = b'{"code":"SERVER_BUSY"}'
                        else:
                            self.assertEqual((header[2], body), (200, {}))
                            kind = 201
                            payload = b"{}"
                        response = HEADER.pack(0x52414754, 2, kind, len(payload), header[4])
                        connection.sendall(response + payload)
                except Exception as error:
                    errors.append(error)

            def serve():
                try:
                    for _ in range(6):
                        connection, _ = listener.accept()
                        worker = threading.Thread(target=handle, args=(connection,))
                        workers.append(worker)
                        worker.start()
                except Exception as error:
                    errors.append(error)

            thread = threading.Thread(target=serve)
            thread.start()
            settings = {
                "port": listener.getsockname()[1],
                "count": 4,
                "mode": "overload",
                "query": "test",
            }
            result = subprocess.run(
                [PROGRAM], input=json.dumps(settings), text=True, capture_output=True, timeout=15
            )
            thread.join(timeout=12)
            for worker in workers:
                worker.join(timeout=12)
                self.assertFalse(worker.is_alive())
            self.assertFalse(thread.is_alive())
            self.assertFalse(errors, errors)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["rejected"], 4)
            self.assertEqual(report["client_threads"], 2)
            self.assertEqual(len(report["rows"]), 4)
            self.assertAlmostEqual(
                report["reject_p95_s"], max(r["seconds"] for r in report["rows"])
            )

    def test_business_success_partial_and_failures(self):
        for case, code in (
            ("business-good", "OK"),
            ("business-agent", "OK"),
            ("business-partial", "PARTIAL"),
            ("business-empty", "NO_EVIDENCE"),
            ("business-error", "UPSTREAM_TIMEOUT"),
            ("business-false-ok", "CLIENT_ERROR"),
        ):
            with self.subTest(case=case):
                result = self.run_peer(case)
                self.assertEqual(result.returncode, 0, result.stderr)
                rows = json.loads(result.stdout)["rows"]
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["code"], code)
                self.assertEqual(rows[0]["mode"], "agent" if case == "business-agent" else "rag")

    def test_invalid_settings(self):
        for settings in (
            {},
            {"port": 0, "count": 1, "mode": "ping"},
            {"port": 9000, "count": 2049, "mode": "ping"},
        ):
            with self.subTest(settings=settings):
                result = subprocess.run(
                    [PROGRAM], input=json.dumps(settings), text=True, capture_output=True, timeout=5
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
