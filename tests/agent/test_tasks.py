"""命令提案及结果续行，不在服务端启动Shell或执行器。"""

import json
import unittest
from typing import Literal

from langchain_core.messages import AIMessage, ToolMessage

from ragent.tasks import advance_task, new_task


class Script:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.messages = []

    async def ainvoke(self, messages, allowed):
        self.messages.append(list(messages))
        return next(self.replies)


class TaskTests(unittest.IsolatedAsyncioTestCase):
    def task(self, mode: Literal["cmd", "agent"] = "cmd"):
        return new_task("查看项目再修改", mode, {"name": "project", "cwd": "."})

    async def test_pause_and_resume_preserves_tool_identity(self):
        model = Script(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {"name": "run_command", "args": {"command": "pwd"}, "id": "original-id"}
                    ],
                ),
                AIMessage(content="结果已确认"),
            ]
        )
        proposal = await advance_task(self.task(), model)
        self.assertEqual(
            proposal["commands"], [{"id": "original-id", "command": "pwd", "cwd": "."}]
        )
        self.assertEqual(len(model.messages), 1)
        state = json.loads(json.dumps(proposal["state"]))
        final = await advance_task(
            state,
            model,
            results=[
                {"id": "original-id", "status": "success", "effect": "none", "text": "/project"}
            ],
        )
        self.assertEqual(final["status"], "completed")
        message = model.messages[1][-1]
        self.assertIsInstance(message, ToolMessage)
        self.assertEqual(message.tool_call_id, "original-id")
        self.assertIn("/project", message.content)

    async def test_retrieval_and_command_results_are_complete(self):
        model = Script(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {"name": "run_command", "args": {"command": "pwd"}, "id": "command"},
                        {"name": "search_linux_manual", "args": {"query": "pwd"}, "id": "search"},
                    ],
                ),
                AIMessage(content="有证据的说明"),
            ]
        )

        async def retrieve(query):
            self.assertIn("查看项目再修改", query)
            return {"text": "手册证据", "sources": [{"source": "pwd(1)"}]}

        proposal = await advance_task(self.task("agent"), model, retrieve)
        final = await advance_task(
            proposal["state"],
            model,
            retrieve,
            [{"id": "command", "status": "success", "effect": "none", "text": "/project"}],
        )
        results = [message for message in model.messages[1] if isinstance(message, ToolMessage)]
        self.assertEqual([message.tool_call_id for message in results], ["command", "search"])
        self.assertEqual(final["sources"], [{"source": "pwd(1)"}])

    async def test_cancel_never_calls_model_again(self):
        model = Script(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {"name": "run_command", "args": {"command": "rm file"}, "id": "delete"}
                    ],
                )
            ]
        )
        proposal = await advance_task(self.task(), model)
        final = await advance_task(
            proposal["state"],
            model,
            results=[{"id": "delete", "status": "cancelled", "effect": "none", "text": "用户拒绝"}],
        )
        self.assertEqual(final["status"], "cancelled")
        self.assertEqual(len(model.messages), 1)

    async def test_unknown_effect_stops_without_retry(self):
        model = Script(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {"name": "run_command", "args": {"command": "touch file"}, "id": "write"}
                    ],
                )
            ]
        )
        proposal = await advance_task(self.task(), model)
        final = await advance_task(
            proposal["state"],
            model,
            results=[{"id": "write", "status": "failed", "effect": "unknown", "text": "连接中断"}],
        )
        self.assertEqual(final["status"], "incomplete")
        self.assertEqual(len(model.messages), 1)

    async def test_mismatched_result_rejected(self):
        model = Script(
            [
                AIMessage(
                    content="",
                    tool_calls=[{"name": "run_command", "args": {"command": "pwd"}, "id": "one"}],
                )
            ]
        )
        proposal = await advance_task(self.task(), model)
        with self.assertRaises(ValueError):
            await advance_task(
                proposal["state"],
                model,
                results=[{"id": "other", "status": "success", "effect": "none", "text": "fake"}],
            )
        self.assertEqual(len(model.messages), 1)

    async def test_retrieval_failure_stops_related_commands(self):
        from rag.services import ServiceError

        async def unavailable(query):
            raise ServiceError("受控依赖故障")

        model = Script(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {"name": "search_linux_manual", "args": {"query": "ls"}, "id": "search"},
                        {"name": "run_command", "args": {"command": "touch file"}, "id": "write"},
                    ],
                )
            ]
        )
        state = new_task("先检索，再操作", "agent", {"name": "fixture"})
        state["actions"].append(
            {"tool": "run_command", "status": "success", "summary": "此前已完成写入"}
        )
        final = await advance_task(state, model, unavailable)
        self.assertEqual(final["status"], "incomplete")
        self.assertNotIn("commands", final)
        self.assertEqual(final["actions"][0]["status"], "success")
        self.assertEqual(final["actions"][1]["status"], "failed")
        self.assertEqual(len(model.messages), 1)

    async def test_round_count_survives_resume(self):
        calls = [
            AIMessage(
                content="",
                tool_calls=[{"name": "run_command", "args": {"command": "pwd"}, "id": str(index)}],
            )
            for index in range(4)
        ]
        model = Script(calls)
        state = self.task()
        proposal = await advance_task(state, model)
        for index in range(4):
            proposal = await advance_task(
                proposal["state"],
                model,
                results=[
                    {"id": str(index), "status": "success", "effect": "none", "text": "/project"}
                ],
            )
        self.assertEqual(proposal["status"], "incomplete")
        self.assertEqual(len(model.messages), 4)


if __name__ == "__main__":
    unittest.main()
