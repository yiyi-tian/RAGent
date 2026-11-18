"""宿主机观察执行器真实往返，拒绝写命令及受保护内容。"""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

EXECUTOR = str(Path(sys.argv.pop(1)).resolve())


class ObserveTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / "sample").write_text("first\nsecond\nthird\n")
        (self.root / ".env").write_text("SECRET-SENTINEL")

    def run_command(self, command):
        return subprocess.run(
            [EXECUTOR],
            input=json.dumps({"command": command, "workspace": str(self.root)}),
            text=True,
            capture_output=True,
            timeout=10,
        )

    def test_simple_chains_and_pipeline(self):
        result = self.run_command("pwd; cat sample | head -n 1; grep second sample")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, f"{self.root}\nfirst\nsecond\n")
        result = self.run_command("cat missing && echo wrong; cat missing || echo recovered")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "recovered\n")

    def test_file_count_pipeline(self):
        expected = sum(path.is_file() for path in self.root.iterdir())
        result = self.run_command("find . -maxdepth 1 -type f | wc -l")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(int(result.stdout), expected)

    def test_glob_and_quoted_literal_are_distinct(self):
        (self.root / "one.txt").write_text("one")
        (self.root / "two.txt").write_text("two")
        self.assertEqual(self.run_command("echo *.txt").stdout, "one.txt two.txt\n")
        self.assertEqual(self.run_command("echo '*.txt'").stdout, "*.txt\n")
        self.assertEqual(self.run_command(r"echo \*.txt").stdout, "*.txt\n")

    def test_rejects_entire_mixed_chain_before_observation(self):
        result = self.run_command("echo SHOULD-NOT-RUN; touch marker")
        self.assertEqual(result.returncode, 126)
        self.assertNotIn("SHOULD-NOT-RUN", result.stdout)
        self.assertFalse((self.root / "marker").exists())

    def test_protected_file_and_escape(self):
        for command in ("cat .env", "cat /etc/passwd", "cat ../outside"):
            result = self.run_command(command)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("SECRET-SENTINEL", result.stdout)
        (self.root / "escape").symlink_to("/etc/passwd")
        self.assertNotEqual(self.run_command("cat escape").returncode, 0)

    def test_git_staged_secret_is_not_returned(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        subprocess.run(["git", "-C", str(self.root), "add", "."], check=True)
        result = self.run_command("git diff --cached")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("first", result.stdout)
        self.assertNotIn("SECRET-SENTINEL", result.stdout)
        result = self.run_command("git status --short")
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
