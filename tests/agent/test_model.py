"""受控 OpenAI 兼容 HTTP 服务验证模型适配。"""

import os
import asyncio
import threading
import unittest
from unittest.mock import AsyncMock, Mock, patch
from types import SimpleNamespace

from aiohttp import web
from aiohttp.test_utils import TestServer
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_openai import ChatOpenAI
from openai import DefaultAsyncHttpxClient, DefaultHttpxClient

from ragent.errors import ModelError
from ragent.model import DeepSeekChat, DeepSeekModel


class ModelTests(unittest.IsolatedAsyncioTestCase):
    async def test_bound_tools_are_reused_without_reusing_request_messages(self):
        with patch.dict(
            os.environ,
            {
                "RAGENT_API_KEY": "fixture",
                "RAGENT_MODEL": "fixture",
                "RAGENT_BASE_URL": "https://fixture.invalid",
            },
        ):
            model = DeepSeekModel(2)
        await model.close()

        async def respond(messages):
            return AIMessage(content=messages[-1].content)

        runnable = SimpleNamespace(ainvoke=AsyncMock(side_effect=respond))
        model.client = SimpleNamespace(bind_tools=Mock(return_value=runnable))
        first, second = await asyncio.gather(
            model.ainvoke([HumanMessage(content="第一个请求")], ["diagnose"]),
            model.ainvoke([HumanMessage(content="第二个请求")], ["diagnose"]),
        )
        model.client.bind_tools.assert_called_once()
        self.assertEqual(runnable.ainvoke.await_count, 2)
        self.assertEqual(first.content, "第一个请求")
        self.assertEqual(second.content, "第二个请求")

    async def test_json_path_preserves_sdk_payload_and_tool_response(self):
        requests = []

        async def handle(request):
            requests.append(await request.json())
            return web.json_response(
                {
                    "id": "fixture",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "fixture",
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "tool_calls",
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call-1",
                                        "type": "function",
                                        "function": {
                                            "name": "diagnose",
                                            "arguments": '{"target":"memory"}',
                                        },
                                    }
                                ],
                            },
                        }
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                }
            )

        app = web.Application()
        app.router.add_post("/chat/completions", handle)
        async with TestServer(app) as server:
            results = []
            from ragent.agent import TOOLS

            for implementation in (ChatOpenAI, DeepSeekChat):
                client = implementation(
                    model="fixture",
                    api_key="fixture",
                    base_url=str(server.make_url("/")),
                    max_retries=0,
                    max_completion_tokens=2048,
                    extra_body={"thinking": {"type": "disabled"}},
                    http_async_client=DefaultAsyncHttpxClient(),
                    http_client=DefaultHttpxClient(),
                )
                try:
                    runnable = client.bind_tools([TOOLS["diagnose"]])
                    result = await runnable.ainvoke([HumanMessage(content="检查内存")])
                    results.append(result)
                finally:
                    await client.root_async_client.close()
                    client.root_client.close()
        self.assertEqual(requests[0], requests[1])
        self.assertEqual(results[0].tool_calls, results[1].tool_calls)
        self.assertEqual(results[0].usage_metadata, results[1].usage_metadata)

    async def test_budget_wait_does_not_block_event_loop(self):
        entered = threading.Event()
        release = threading.Event()

        def reserve():
            entered.set()
            release.wait(2)

        with patch.dict(
            os.environ,
            {
                "RAGENT_API_KEY": "fixture",
                "RAGENT_MODEL": "fixture",
                "RAGENT_BASE_URL": "https://fixture.invalid",
            },
        ):
            model = DeepSeekModel(1, before_call=reserve)
        await model.close()
        client = SimpleNamespace(ainvoke=AsyncMock(return_value=AIMessage(content="完成")))
        model.client = client
        task = asyncio.create_task(model.ainvoke([HumanMessage(content="问题")], []))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            self.assertFalse(task.done())
            client.ainvoke.assert_not_awaited()
            release.set()
            self.assertEqual((await task).content, "完成")
            self.assertEqual(model.active, 0)
        finally:
            release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_invalid_concurrency_does_not_create_http_clients(self):
        with (
            patch.dict(
                os.environ,
                {
                    "RAGENT_API_KEY": "fixture",
                    "RAGENT_MODEL": "fixture",
                    "RAGENT_BASE_URL": "https://model.example",
                },
            ),
            patch("ragent.model.ChatOpenAI") as client,
        ):
            with self.assertRaises(ValueError):
                DeepSeekModel(concurrency=0)
            client.assert_not_called()

    async def test_close_releases_sync_client_after_async_failure(self):
        model = DeepSeekModel.__new__(DeepSeekModel)
        asynchronous = SimpleNamespace(close=AsyncMock(side_effect=OSError("受控关闭失败")))
        synchronous = SimpleNamespace(close=Mock())
        model.client = SimpleNamespace(root_async_client=asynchronous, root_client=synchronous)
        with self.assertRaisesRegex(OSError, "受控关闭失败"):
            await model.close()
        asynchronous.close.assert_awaited_once()
        synchronous.close.assert_called_once()

    async def test_http_round_trip_and_errors(self):
        status = 200
        requests = []

        async def handle(request):
            body = await request.json()
            requests.append(body)
            if status == -1:
                await asyncio.sleep(1.5)
            if status == -2:
                return web.Response(text="invalid", content_type="application/json")
            if status > 0 and status != 200:
                return web.json_response({"error": {"message": "受控错误"}}, status=status)
            message = {"role": "assistant", "content": "" if status == -3 else "完成"}
            if body.get("tools") and body["messages"][-1]["role"] != "tool":
                message = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "diagnose", "arguments": '{"target":"memory"}'},
                        }
                    ],
                }
            return web.json_response(
                {
                    "id": "test",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "test",
                    "choices": [
                        {
                            "index": 0,
                            "message": message,
                            "finish_reason": "length" if status == -4 else "stop",
                        }
                    ],
                }
            )

        app = web.Application()
        app.router.add_post("/chat/completions", handle)
        async with TestServer(app) as server:
            with patch.dict(
                os.environ,
                {
                    "RAGENT_API_KEY": "test",
                    "RAGENT_MODEL": "test",
                    "RAGENT_BASE_URL": "https://example.com",
                },
            ):
                model = DeepSeekModel()
            await model.close()
            model.client = DeepSeekChat(
                model="test",
                api_key="test",
                base_url=str(server.make_url("/")),
                max_retries=0,
                timeout=1,
            )
            self.addAsyncCleanup(model.close)
            messages = [HumanMessage(content="问题")]
            response = await model.ainvoke(messages, [])
            self.assertEqual(response.content, "完成")
            self.assertNotIn("tools", requests[-1])
            response = await model.ainvoke(messages, ["diagnose"])
            self.assertEqual(response.tool_calls[0]["id"], "call-1")
            await model.ainvoke(
                messages + [response, ToolMessage(content="模拟结果", tool_call_id="call-1")],
                ["diagnose"],
            )
            self.assertEqual(requests[-1]["messages"][-1]["tool_call_id"], "call-1")
            for code, expected in (
                (-1, "UPSTREAM_TIMEOUT"),
                (-2, "UPSTREAM_UNAVAILABLE"),
                (-3, "UPSTREAM_UNAVAILABLE"),
                (-4, "UPSTREAM_UNAVAILABLE"),
                (401, "UPSTREAM_UNAVAILABLE"),
                (402, "UPSTREAM_UNAVAILABLE"),
                (429, "SERVER_BUSY"),
                (500, "UPSTREAM_UNAVAILABLE"),
            ):
                status = code
                with self.assertRaises(ModelError) as raised:
                    await model.ainvoke(messages, [])
                self.assertEqual(raised.exception.code, expected)
                if code == 402:
                    self.assertIn("余额不足", str(raised.exception))
