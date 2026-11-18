"""SNI辅助进程的完整行协议与拒绝分支。"""

from pathlib import Path
import subprocess
import sys
import unittest

HELPER = str(Path(sys.argv.pop(1)).resolve())


class SniTests(unittest.TestCase):
    def test_matching_and_invalid_requests(self):
        lines = [
            b"example.com example.com -\n",
            b"EXAMPLE.com example.com -\n",
            b"example.com. example.com -\n",
            b"evil.example.com example.com -\n",
            b"- example.com -\n",
            b"example.com example.com - extra\n",
            b"example.com example.com\n",
            b"e\xffample.com example.com -\n",
            b"x" * 2048 + b"\n",
            b"example.com example.com -\n",
        ]
        result = subprocess.run([HELPER], input=b"".join(lines), capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.splitlines(), [b"OK"] * 3 + [b"ERR"] * 6 + [b"OK"])


if __name__ == "__main__":
    unittest.main()
