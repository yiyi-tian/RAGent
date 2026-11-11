"""CLI 创建的资源须关闭，外部注入的共享资源由调用者管理。"""

import asyncio
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from ragent.__main__ import create_app, main


class LifecycleTests(unittest.TestCase):
    def test_model_initialization_failure_closes_retriever(self):
        retriever = SimpleNamespace(close=AsyncMock())
        arguments = ["ragent-server", "--model", "deepseek", "--retrieval", "qdrant"]
        with (
            patch.object(sys, "argv", arguments),
            patch("ragent.__main__.create_retriever", return_value=retriever),
            patch("ragent.__main__.DeepSeekModel", side_effect=ValueError("受控配置错误")),
        ):
            with self.assertRaisesRegex(ValueError, "受控配置错误"):
                main()
        retriever.close.assert_awaited_once()

    def test_cleanup_closes_retriever_even_when_model_close_fails(self):
        retriever = SimpleNamespace(close=AsyncMock())
        model = SimpleNamespace(close=AsyncMock(side_effect=OSError("受控关闭失败")))
        arguments = ["ragent-server", "--model", "deepseek", "--retrieval", "qdrant"]

        def run(app, **options):
            self.assertTrue(options["handler_cancellation"])
            app.freeze()
            loop = options["loop"]
            try:
                loop.run_until_complete(app.cleanup())
            finally:
                loop.close()

        with (
            patch.object(sys, "argv", arguments),
            patch("ragent.__main__.create_retriever", return_value=retriever),
            patch("ragent.__main__.DeepSeekModel", return_value=model),
            patch("ragent.__main__.web.run_app", side_effect=run),
        ):
            with self.assertRaisesRegex(OSError, "受控关闭失败"):
                main()
        model.close.assert_awaited_once()
        retriever.close.assert_awaited_once()

    def test_app_does_not_close_injected_shared_resources(self):
        retriever = SimpleNamespace(close=AsyncMock())
        model = SimpleNamespace(close=AsyncMock())
        app = create_app(retriever, model)
        app.freeze()
        asyncio.run(app.cleanup())
        model.close.assert_not_awaited()
        retriever.close.assert_not_awaited()
