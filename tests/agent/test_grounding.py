"""查询意图保留和检索证据约束，不调用真实模型。"""

import unittest
import json
from pathlib import Path
import tempfile
from unittest.mock import AsyncMock

from langchain_core.messages import AIMessage, SystemMessage, ToolMessage

from ragent.agent import RETRIEVAL_INSTRUCTION, answer


class InspectingModel:
    def __init__(self, tool_query):
        self.tool_query = tool_query
        self.contexts = []

    async def ainvoke(self, messages, allowed):
        self.contexts.append(list(messages))
        if len(self.contexts) == 1:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "search_linux_manual",
                        "args": {"query": self.tool_query},
                        "id": "search-1",
                    }
                ],
            )
        return AIMessage(content="根据证据回答")


class GroundingTests(unittest.IsolatedAsyncioTestCase):
    async def test_agent_preserves_original_intent_and_tool_history(self):
        query = "grep 的退出状态 0、1、2 分别表示什么？"
        retrieve = AsyncMock(return_value={"text": "grep(1)：退出状态说明", "sources": []})
        model = InspectingModel("grep")
        await answer(query, "agent", model, retriever=retrieve)
        retrieve.assert_awaited_once_with(query + "\n检索重点：grep")
        context = model.contexts[-1]
        self.assertEqual(context[0].content, query)
        instructions = [m.content for m in context if isinstance(m, SystemMessage)]
        self.assertEqual(instructions, [RETRIEVAL_INSTRUCTION])
        self.assertIsInstance(context[-1], ToolMessage)
        self.assertEqual(context[-1].tool_call_id, "search-1")
        self.assertEqual(context[-2].tool_calls[0]["args"], {"query": "grep"})

    async def test_refined_query_is_kept_and_identical_query_not_duplicated(self):
        query = "如何查看服务状态？"
        for tool_query, expected in (
            (query, query),
            ("systemctl status", query + "\n检索重点：systemctl status"),
        ):
            retrieve = AsyncMock(return_value={"text": "证据", "sources": []})
            await answer(query, "agent", InspectingModel(tool_query), retriever=retrieve)
            retrieve.assert_awaited_once_with(expected)

    async def test_query_limit_keeps_original_question(self):
        query = "问" * 4096
        retrieve = AsyncMock(return_value={"text": "证据不足", "sources": []})
        await answer(query, "agent", InspectingModel("grep"), retriever=retrieve)
        retrieve.assert_awaited_once_with(query)

    async def test_empty_tool_query_remains_a_failure(self):
        retrieve = AsyncMock()
        result = await answer("原问题", "agent", InspectingModel(" "), retriever=retrieve)
        retrieve.assert_not_awaited()
        self.assertEqual(result["actions"][0]["status"], "failed")

    async def test_answer_cache_from_other_pipeline_is_rejected_before_model_call(self):
        from tools.compare_rag import Budget
        from tools.evaluate_context import compare_answers

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "answer-pipeline.json").write_text(json.dumps({"stage": "older"}))
            budget = Budget(directory / "budget.json")
            with self.assertRaisesRegex(ValueError, "不同"):
                await compare_answers([], {}, directory, budget)
            self.assertEqual(budget.counts["model_calls"], 0)

    async def test_rag_constrained_and_chat_unaffected(self):
        contexts = []

        class Model:
            async def ainvoke(self, messages, allowed):
                contexts.append(list(messages))
                return AIMessage(content="结束")

        retrieve = AsyncMock(return_value={"text": "证据不足", "sources": []})
        await answer("原问题", "rag", Model(), retriever=retrieve)
        self.assertTrue(any(isinstance(m, SystemMessage) for m in contexts[-1]))
        retrieve.assert_awaited_once_with("原问题")
        await answer("原问题", "chat", Model(), retriever=retrieve)
        self.assertFalse(any(isinstance(m, SystemMessage) for m in contexts[-1]))
        self.assertEqual(retrieve.await_count, 1)


if __name__ == "__main__":
    unittest.main()
