"""真实rootless挂载隔离；测试夹具直接调用C边界，不提供生产自动批准入口。"""

import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest

FIXTURE = str(Path(sys.argv.pop(1)).resolve())
SOCKET = f"/run/user/{os.getuid()}/ragent-sandbox-docker.sock"


@unittest.skipUnless(os.environ.get("RAGENT_TEST_SANDBOX"), "显式启用rootless容器验证")
class ContainerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='ragent-目录,quote"-')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / ".env").write_text("SECRET-SENTINEL")

    def run_command(self, command, authorization=None):
        arguments = [FIXTURE, str(self.root), command, SOCKET]
        if authorization is not None:
            arguments.append(json.dumps(authorization))
        process = subprocess.run(arguments, capture_output=True, text=True, timeout=75)
        self.assertEqual(process.returncode, 0, process.stderr)
        result = json.loads(process.stdout)
        self.assertFalse(result["cleanup_failed"], result)
        return result

    def test_credentials_are_explicit_and_not_ambient(self):
        result = self.run_command(
            "python3 -c \"import os; print(os.environ.get('TEST_TOKEN')); "
            "print(os.environ.get('RAGENT_API_KEY'))\"",
            {"environment": {"TEST_TOKEN": "LOCAL-CREDENTIAL-SENTINEL"}},
        )
        self.assertEqual(result["status"], 0, result)
        self.assertEqual(result["output"], "LOCAL-CREDENTIAL-SENTINEL\nNone\n")

    def test_network_requires_exact_grants_and_tls_name(self):
        script = r"""import json,socket,ssl,urllib.request
proxy=urllib.request.ProxyHandler({'http':'http://127.0.0.1:3128',
                                  'https':'http://127.0.0.1:3128'})
http=urllib.request.build_opener(proxy)
checks={}
with http.open('https://example.com',timeout=15) as result:
    checks['allowed']=result.status==200 and b'Example Domain' in result.read(65536)
for name,url in [('unknown','https://not-authorized.invalid'),
                 ('ip','http://127.0.0.1:9001'),('port','https://example.com:444')]:
    try:
        http.open(url,timeout=5).close()
        checks[name]=False
    except (OSError,urllib.error.URLError):
        checks[name]=True
for name,sni in [('different_sni','not-authorized.invalid'),('missing_sni',None)]:
    connection=socket.create_connection(('127.0.0.1',3128),timeout=5)
    connection.sendall(b'CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n')
    response=b''
    while b'\r\n\r\n' not in response:
        response+=connection.recv(1)
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname=False
    context.verify_mode=ssl.CERT_NONE
    try:
        context.wrap_socket(connection,server_hostname=sni).close()
        checks[name]=False
    except OSError:
        checks[name]=True
    finally:
        connection.close()
print('RAGENT_NETWORK_CHECKS='+json.dumps(checks))
"""
        result = self.run_command(
            "python3 - <<'PY'\n" + script + "PY",
            {"network": ["https://example.com:443"]},
        )
        self.assertEqual(result["status"], 0, result)
        prefix = "RAGENT_NETWORK_CHECKS="
        for line in result["output"].splitlines():
            if line.startswith(prefix):
                checks = json.loads(line[len(prefix) :])
                self.assertTrue(all(checks.values()), checks)
                return
        self.fail("未收到网络检查结果：" + result["output"])

    def test_ssh_transport_uses_only_approved_endpoint(self):
        script = r"""import socket
connection=socket.create_connection(('127.0.0.1',3128),timeout=12)
connection.sendall(b'CONNECT ssh.github.com:443 HTTP/1.1\r\nHost: ssh.github.com:443\r\n\r\n')
header=b''
while b'\r\n\r\n' not in header:
    block=connection.recv(1)
    assert block and len(header)<8192
    header+=block
assert b'200' in header.splitlines()[0],header
banner=connection.recv(512)
assert banner.startswith(b'SSH-2.0-'),banner
connection.close()
print('SSH_BANNER_OK')
"""
        result = self.run_command(
            "python3 - <<'PY'\n" + script + "PY",
            {"network": ["ssh://ssh.github.com:443"]},
        )
        self.assertEqual(result["status"], 0, result)
        self.assertIn("SSH_BANNER_OK", result["output"])

    def test_real_writeback_and_ownership(self):
        result = self.run_command("mkdir notes; printf hello > notes/a; cat notes/a")
        self.assertEqual(result["status"], 0, result)
        self.assertEqual(result["output"], "hello")
        self.assertEqual((self.root / "notes/a").read_text(), "hello")
        self.assertEqual((self.root / "notes/a").stat().st_uid, os.getuid())

    def test_secret_and_hardlink_are_inaccessible(self):
        (self.root / "alias").hardlink_to(self.root / ".env")
        for name in (".env", "alias"):
            result = self.run_command(f"python3 -c \"print(open('{name}').read())\"")
            self.assertTrue(result["invoked"], result)
            self.assertNotEqual(result["status"], 0)
            self.assertNotIn("SECRET-SENTINEL", result["output"])

    def test_host_socket_is_masked(self):
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(str(self.root / "service.sock"))
            listener.listen()
            result = self.run_command(
                'python3 -c "import socket; s=socket.socket(socket.AF_UNIX);'
                "s.connect('service.sock')\""
            )
            self.assertTrue(result["invoked"], result)
            self.assertNotEqual(result["status"], 0)

    def test_outside_host_data_and_network_are_not_available(self):
        with tempfile.NamedTemporaryFile(mode="w") as outside:
            outside.write("OUTSIDE-SENTINEL")
            outside.flush()
            (self.root / "escape").symlink_to(outside.name)
            result = self.run_command("python3 -c \"print(open('escape').read())\"")
            self.assertNotEqual(result["status"], 0)
            self.assertNotIn("OUTSIDE-SENTINEL", result["output"])
        result = self.run_command(
            'python3 -c "import socket;' "socket.create_connection(('8.8.8.8',443),1)\""
        )
        self.assertTrue(result["invoked"], result)
        self.assertNotEqual(result["status"], 0)
        self.assertIn("Network is unreachable", result["output"])
        result = self.run_command("touch /etc/ragent-forbidden")
        self.assertNotEqual(result["status"], 0)
        self.assertIn("Read-only file system", result["output"])

    def test_cancel_reaps_container_before_late_write(self):
        process = subprocess.Popen(
            [FIXTURE, str(self.root), "touch started; sleep 30; touch MUST-NOT", SOCKET],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.monotonic() + 15
            while not (self.root / "started").exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue((self.root / "started").exists())
            process.send_signal(signal.SIGTERM)
            output, error = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, error)
            result = json.loads(output)
            self.assertTrue(result["invoked"])
            self.assertNotEqual(result["status"], 0)
            self.assertFalse(result["cleanup_failed"])
            self.assertFalse((self.root / "MUST-NOT").exists())
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()


if __name__ == "__main__":
    unittest.main()
