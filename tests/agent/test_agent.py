"""Mock 编排的行为测试。"""

import asyncio
import unittest
from langchain_core.messages import AIMessage
from ragent.agent import answer
from ragent.errors import ModelError


class Script:
    def __init__(self, replies):
        self.replies = iter(replies)

    async def ainvoke(self, messages, allowed):
        return next(self.replies)


class AgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_modes(self):
        for mode, count in [("chat", 0), ("rag", 1), ("cmd", 1), ("agent", 2)]:
            result = await answer("测试", mode)
            self.assertEqual(len(result["actions"]), count)

    async def test_invalid_tool_and_partial_failure(self):
        calls = [
            {"name": "diagnose", "args": {"target": "invalid"}, "id": "1"},
            {"name": "unknown", "args": {}, "id": "2"},
        ]
        model = Script([AIMessage(content="", tool_calls=calls), AIMessage(content="结束")])
        result = await answer("测试", "agent", model)
        self.assertEqual([a["status"] for a in result["actions"]], ["failed", "failed"])

    async def test_limit(self):
        model = Script(
            [
                AIMessage(
                    content="",
                    tool_calls=[{"name": "diagnose", "args": {"target": "memory"}, "id": "1"}],
                )
            ]
        )
        self.assertIn("限制", (await answer("测试", "agent", model, rounds=1))["text"])

    async def test_timeout(self):
        class Slow:
            async def ainvoke(self, messages, allowed):
                await asyncio.sleep(1)

        with self.assertRaises(ModelError) as raised:
            await answer("测试", "chat", Slow(), timeout=0.01)
        self.assertEqual(raised.exception.code, "UPSTREAM_TIMEOUT")

    async def test_multiple_rounds_and_context(self):
        class Model:
            def __init__(self):
                self.count = 0

            async def ainvoke(self, messages, allowed):
                self.count += 1
                if self.count == 3:
                    self_outer.assertEqual(sum(m.type == "tool" for m in messages), 2)
                    return AIMessage(content="结束")
                return AIMessage(
                    content="",
                    tool_calls=[
                        {"name": "diagnose", "args": {"target": "memory"}, "id": str(self.count)}
                    ],
                )

        self_outer = self
        result = await answer("测试", "agent", Model())
        self.assertEqual(len(result["actions"]), 2)
        self.assertEqual((await answer("另一个请求", "chat"))["actions"], [])

    async def test_mode_permission(self):
        model = Script(
            [
                AIMessage(
                    content="",
                    tool_calls=[{"name": "diagnose", "args": {"target": "memory"}, "id": "1"}],
                ),
                AIMessage(content="结束"),
            ]
        )
        result = await answer("测试", "chat", model)
        self.assertEqual(result["actions"][0]["status"], "failed")

    async def test_success_survives_failure(self):
        calls = [
            {"name": "diagnose", "args": {"target": "memory"}, "id": "1"},
            {"name": "diagnose", "args": {"target": 1}, "id": "2"},
        ]
        model = Script([AIMessage(content="", tool_calls=calls), AIMessage(content="结束")])
        result = await answer("测试", "agent", model)
        self.assertCountEqual(
            [action["status"] for action in result["actions"]], ["success", "failed"]
        )

    async def test_duplicate_and_excessive_calls(self):
        for count, unique in ((2, False), (5, True)):
            calls = []
            for index in range(count):
                calls.append(
                    {
                        "name": "diagnose",
                        "args": {"target": "memory"},
                        "id": str(index) if unique else "same",
                    }
                )
            result = await answer(
                "测试", "agent", Script([AIMessage(content="", tool_calls=calls)])
            )
            self.assertIn("限制", result["text"])
            self.assertEqual(result["actions"], [])

    async def test_retrieval_parallel_and_empty(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        active = 0

        async def retrieve(query):
            nonlocal active
            active += 1
            if active == 2:
                entered.set()
            await release.wait()
            return {"text": "未找到相关资料。", "sources": []}

        calls = []
        for index in range(2):
            calls.append(
                {"name": "search_linux_manual", "args": {"query": "测试"}, "id": str(index)}
            )
        model = Script([AIMessage(content="", tool_calls=calls), AIMessage(content="结束")])
        task = asyncio.create_task(answer("测试", "agent", model, retriever=retrieve))
        try:
            await asyncio.wait_for(entered.wait(), 1)
        finally:
            release.set()
        result = await task
        self.assertEqual(result["sources"], [])
        self.assertEqual(len(result["actions"]), 2)
