"""固定HTTP工作进程的就绪、故障联动与退出回收，不调用云端。"""

from concurrent.futures import ThreadPoolExecutor
import http.client
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import tempfile
import time
import unittest


class WorkerIntegrationTests(unittest.TestCase):
    def run_case(self, fail_worker):
        with tempfile.TemporaryFile() as log:
            env = dict(os.environ, RAGENT_MODEL_CONCURRENCY="8", RAGENT_RETRIEVAL_WORKERS="4")
            process = subprocess.Popen(
                [sys.executable, "-m", "ragent", "--workers", "4", "--port", "0"],
                env=env,
                stdout=subprocess.PIPE,
                stderr=log,
            )
            children = []
            try:
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ)
                    self.assertTrue(selector.select(120), "工作进程未就绪")
                line = process.stdout.readline().decode()
                self.assertIn("workers=4", line)
                port = int(line.split()[2].rsplit(":", 1)[1])
                children = (
                    Path(f"/proc/{process.pid}/task/{process.pid}/children").read_text().split()
                )
                children = [
                    child
                    for child in children
                    if b"spawn_main" in Path(f"/proc/{child}/cmdline").read_bytes()
                ]
                self.assertEqual(len(children), 4)

                def request(identity):
                    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                    try:
                        body = {"request_id": str(identity), "mode": "agent", "query": "测试问题"}
                        connection.request(
                            "POST", "/agent", json.dumps(body), {"Content-Type": "application/json"}
                        )
                        response = connection.getresponse()
                        self.assertEqual(response.status, 200)
                        self.assertIn("模拟回答", json.loads(response.read())["text"])
                    finally:
                        connection.close()

                with ThreadPoolExecutor(max_workers=8) as executor:
                    list(executor.map(request, range(16)))
                if fail_worker:
                    os.kill(int(children[0]), signal.SIGKILL)
                    self.assertNotEqual(process.wait(timeout=45), 0)
                else:
                    process.terminate()
                    self.assertEqual(process.wait(timeout=45), 0)
                for child in children:
                    self.assertFalse(Path(f"/proc/{child}").exists(), f"工作进程{child}未回收")
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=45)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                process.stdout.close()
                for child in children:
                    if Path(f"/proc/{child}").exists():
                        os.kill(int(child), signal.SIGKILL)

    def test_ready_and_shutdown(self):
        self.run_case(False)

    def test_worker_failure_stops_group(self):
        self.run_case(True)


if __name__ == "__main__":
    unittest.main()
