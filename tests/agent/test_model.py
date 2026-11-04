"""受控 OpenAI 兼容 HTTP 服务验证模型适配。"""

import os
import asyncio
import unittest
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestServer
from langchain_core.messages import HumanMessage, ToolMessage
from langchain_openai import ChatOpenAI

from ragent.errors import ModelError
from ragent.model import DeepSeekModel


class ModelTests(unittest.IsolatedAsyncioTestCase):
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
            model.client = ChatOpenAI(
                model="test",
                api_key="test",
                base_url=str(server.make_url("/")),
                max_retries=0,
                timeout=1,
            )
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
                (429, "SERVER_BUSY"),
                (500, "UPSTREAM_UNAVAILABLE"),
            ):
                status = code
                with self.assertRaises(ModelError) as raised:
                    await model.ainvoke(messages, [])
                self.assertEqual(raised.exception.code, expected)
