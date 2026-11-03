from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from ragent.config import Settings
from ragent.errors import ConfigurationError


class SettingsTests(unittest.TestCase):
    def test_defaults_and_api_key_aliases(self) -> None:
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}, clear=True):
            settings = Settings.from_env()
        self.assertEqual(settings.model, "deepseek-chat")
        self.assertEqual(settings.port, 9000)
        self.assertEqual(settings.require_api_key(), "test-key")
        self.assertNotIn("test-key", repr(settings))

    def test_invalid_number_is_rejected(self) -> None:
        with patch.dict(os.environ, {"RAGENT_MAX_CONCURRENCY": "none"}, clear=True):
            with self.assertRaises(ConfigurationError):
                Settings.from_env()

    def test_missing_key_is_reported(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings.from_env()
        with self.assertRaises(ConfigurationError):
            settings.require_api_key()


if __name__ == "__main__":
    unittest.main()
