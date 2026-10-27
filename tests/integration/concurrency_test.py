"""使用仅测试版本的可控慢任务验证并发与过载。"""
import os
import signal
import subprocess
import time
import unittest

os.environ["RAGENT_TEST_DELAY_MS"] = "700"
from network_test import SERVER, RunningServer, dump, frame, receive


def wait_for_log(server, text):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if text.encode() in os.pread(server.log.fileno(), 1024 * 1024, 0):
            return
        time.sleep(0.01)
    raise AssertionError(f"未观察到日志: {text}")


class ConcurrencyTests(unittest.TestCase):
    def test_overload_ping_and_disconnect(self):
        server = RunningServer("--worker-threads", "1", "--task-capacity", "1", "--result-capacity", "1")
        try:
            with server.connect() as first:
                first.sendall(frame(request_id=1))
                wait_for_log(server, "stage=task_start req=1")
                first.sendall(frame(request_id=2) + frame(request_id=3) + frame(200, 4, b"{}"))
                responses = {response[1]: response for response in (receive(first), receive(first))}
                self.assertEqual(responses[3][0], 101)
                self.assertEqual(responses[3][2]["code"], "SERVER_BUSY")
                self.assertEqual(responses[4], (201, 4, {}))
                # 原连接断开后，已接纳任务的完成结果不可进入新连接。
            wait_for_log(server, "stage=close reason=连接断开")
            with server.connect() as second:
                second.sendall(frame(200, 1, b"{}"))
                self.assertEqual(receive(second), (201, 1, {}))
                wait_for_log(server, "stage=discard reason=disconnect")
                second.sendall(frame(request_id=2, body=dump({"query": "new"})))
                self.assertEqual(receive(second), (100, 2, {"text": "server received: new", "sources": []}))
        finally:
            server.close()

    def test_out_of_order_completions(self):
        os.environ["RAGENT_TEST_DELAY_FIRST_ONLY"] = "1"
        try:
            server = RunningServer("--worker-threads", "2")
        finally:
            del os.environ["RAGENT_TEST_DELAY_FIRST_ONLY"]
        try:
            with server.connect() as connection:
                connection.sendall(frame(request_id=1))
                wait_for_log(server, "stage=task_start req=1")
                connection.sendall(frame(request_id=2))
                self.assertEqual(receive(connection)[1], 2)
                self.assertEqual(receive(connection)[1], 1)
        finally:
            server.close()

    def test_invalid_pool_options(self):
        for option, value in (("--worker-threads", "0"), ("--worker-threads", "65"),
                              ("--task-capacity", "0"), ("--result-capacity", "4097")):
            result = subprocess.run([SERVER, option, value], capture_output=True, text=True, timeout=3)
            self.assertEqual(result.returncode, 2)

    def test_shutdown_with_waiting_work(self):
        server = RunningServer("--worker-threads", "1", "--task-capacity", "1")
        try:
            with server.connect() as connection:
                connection.sendall(frame())
                wait_for_log(server, "stage=task_start req=1")
                connection.sendall(frame(request_id=2))
                server.process.send_signal(signal.SIGTERM)
                self.assertEqual(server.process.wait(timeout=3), 0)
        finally:
            server.close()


if __name__ == "__main__":
    unittest.main()
