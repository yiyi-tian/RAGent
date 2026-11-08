"""验收工具必须把失败传递为非零退出状态。"""

import sys
import unittest
from unittest.mock import AsyncMock, patch

from tools.reliability import main


class ReliabilityCliTests(unittest.TestCase):
    def test_report_controls_exit_status(self):
        cases = [
            ({"passed": True}, 0),
            ({"passed": False}, 1),
            ({"passed": False, "full_acceptance": False, "checks": {"drained": True}}, 0),
            ({"passed": False, "full_acceptance": False, "checks": {"drained": False}}, 1),
        ]
        for report, expected in cases:
            with self.subTest(report=report):
                with (
                    patch.object(sys, "argv", ["reliability", "--output", "unused"]),
                    patch("tools.reliability.main_async", AsyncMock(return_value=report)),
                ):
                    if expected:
                        with self.assertRaises(SystemExit) as error:
                            main()
                        self.assertEqual(error.exception.code, expected)
                    else:
                        main()
