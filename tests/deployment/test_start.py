"""隔离验证部署脚本，不安装软件、创建容器或调用云端服务。"""

import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import unittest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/start.sh"


def available_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        for folder in ("scripts", "config", "bin", "build", ".venv/bin", "rag"):
            (self.root / folder).mkdir(parents=True)
        shutil.copyfile(SCRIPT, self.root / "scripts/start.sh")
        self.ports = [available_port(), available_port()]
        config = {
            "host": "127.0.0.1",
            "port": self.ports[0],
            "agent_url": f"http://127.0.0.1:{self.ports[1]}/agent",
        }
        (self.root / "config/agent.json").write_text(json.dumps(config))
        (self.root / ".env").write_text(
            "\n".join(
                f"{name}=test"
                for name in (
                    "RAGENT_API_KEY",
                    "RAGENT_MODEL",
                    "RAGENT_BASE_URL",
                    "RAGENT_EMBEDDING_API_KEY",
                    "RAGENT_EMBEDDING_BASE_URL",
                )
            )
        )
        self.env = dict(os.environ, PATH=f"{self.root}/bin:{os.environ['PATH']}")
        self.env.pop("RAGENT_QDRANT_URL", None)
        self.program(
            "bin/python3",
            '#!/bin/bash\nif [[ $1 == -m && $2 == venv ]]; then exit 0; fi\nexec /usr/bin/python3 "$@"\n',
        )
        self.program(".venv/bin/python", '#!/bin/bash\nexec /usr/bin/python3 "$@"\n')
        self.program("bin/dpkg-query", "#!/bin/sh\nprintf 'install ok installed'\n")
        self.program(
            "bin/docker",
            """#!/bin/bash
printf '%s\n' "$*" >> docker.calls
if [[ $1 == container ]]; then exit 1; fi
""",
        )
        self.program(
            "bin/curl",
            """#!/bin/bash
if [[ ${TEST_NEW_QDRANT:-} == 1 && "$*" == *'--max-time 2'* ]]; then exit 1; fi
""",
        )
        self.program("bin/cmake", "#!/bin/sh\necho build >> build.calls\n")
        self.program(".venv/bin/pip", "#!/bin/sh\necho install >> install.calls\n")
        self.program(
            ".venv/bin/ragent-manual",
            """#!/bin/bash
if [[ $1 == import ]]; then echo import >> import.calls; fi
if [[ $1 == search ]]; then
    echo search >> search.calls
    result=${TEST_SEARCH_RESULT:-}
    if [[ -z $result ]]; then result='[{}]'; fi
    echo "$result"
fi
""",
        )
        service = """#!/usr/bin/python3
import json
import socket
import sys
if '--port' in sys.argv:
    port = int(sys.argv[sys.argv.index('--port') + 1])
else:
    with open('config/agent.json') as file:
        port = json.load(file)['port']
with socket.socket() as listener:
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(('127.0.0.1', port))
    listener.listen()
    while True:
        connection, _ = listener.accept()
        connection.close()
"""
        self.program(".venv/bin/ragent-server", service)
        self.program("build/agent-server", service)
        self.program("build/agent", "#!/bin/sh\nexit 0\n")
        (self.root / "rag/__init__.py").write_text("")
        (self.root / "rag/services.py").write_text("""
class ServiceError(Exception):
    pass

class Embedding:
    model = "text-embedding-v4"
    def embed(self, texts):
        raise AssertionError("日常启动不能调用 Embedding")

class Store:
    def _call(self, method, path="", data=None):
        if path:
            return {"result": {"count": 2}}
        return {"result": {"config": {"params": {"vectors": {"size": 1024}}}}}
    def ensure_collection(self, dimension):
        pass

def configured_services():
    return Embedding(), Store()
""")

    def program(self, name, text):
        path = self.root / name
        path.write_text(text)
        path.chmod(0o755)

    def test_start_and_cleanup(self):
        self.check_start(deploy=True)

    def test_daily_start_skips_install_build_and_cloud(self):
        self.check_start(deploy=False)

    def check_start(self, deploy):
        if deploy:
            self.env["TEST_NEW_QDRANT"] = "1"
        with tempfile.TemporaryFile() as output:
            process = subprocess.Popen(
                ["bash", "scripts/start.sh"] + (["--deploy"] if deploy else []),
                cwd=self.root,
                env=self.env,
                stdout=output,
                stderr=output,
            )
            try:
                for _ in range(200):
                    text = os.pread(output.fileno(), 65536, 0).decode()
                    if "服务已就绪" in text:
                        break
                    self.assertIsNone(process.poll(), text)
                    time.sleep(0.05)
                else:
                    self.fail("受控服务未启动")
                if deploy:
                    self.assertTrue((self.root / "import.calls").exists())
                    self.assertIn("volume create", (self.root / "docker.calls").read_text())
                    self.assertIn("run -d", (self.root / "docker.calls").read_text())
                else:
                    for name in ("import.calls", "search.calls", "build.calls", "install.calls"):
                        self.assertFalse((self.root / name).exists(), name)
                process.send_signal(signal.SIGTERM)
                self.assertEqual(process.wait(timeout=15), 143)
                for port in self.ports:
                    with socket.socket() as probe:
                        self.assertNotEqual(probe.connect_ex(("127.0.0.1", port)), 0)
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=15)

    def test_skip_import_rejects_empty_collection(self):
        self.env["TEST_SEARCH_RESULT"] = "[]"
        result = subprocess.run(
            ["bash", "scripts/start.sh", "--deploy", "--skip-import"],
            cwd=self.root,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("知识库为空", result.stderr)
        self.assertFalse((self.root / "import.calls").exists())

    def test_occupied_port_stops_before_import(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", self.ports[0]))
            listener.listen()
            result = subprocess.run(
                ["bash", "scripts/start.sh"],
                cwd=self.root,
                env=self.env,
                capture_output=True,
                text=True,
                timeout=10,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("已占用", result.stderr)
        self.assertFalse((self.root / "import.calls").exists())


if __name__ == "__main__":
    unittest.main()
