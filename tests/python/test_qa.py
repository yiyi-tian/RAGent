from __future__ import annotations

import unittest

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from ragent.errors import AgentError
from ragent.qa import QAEngine


class RecordingChatModel(BaseChatModel):
    captured_messages: list[list[BaseMessage]] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "recording"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager=None,
        **kwargs,
    ) -> ChatResult:
        self.captured_messages.append(list(messages))
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content="这是回答"))]
        )


class QAEngineTests(unittest.TestCase):
    def test_answer_uses_history_and_prompt(self) -> None:
        model = RecordingChatModel()
        engine = QAEngine(model)

        answer = engine.answer(
            "第二个问题",
            [HumanMessage(content="第一个问题"), AIMessage(content="第一个回答")],
        )

        self.assertEqual(answer, "这是回答")
        messages = model.captured_messages[0]
        self.assertIsInstance(messages[0], SystemMessage)
        self.assertEqual(
            [message.content for message in messages[1:]],
            ["第一个问题", "第一个回答", "第二个问题"],
        )

    def test_blank_and_oversized_questions_are_rejected(self) -> None:
        engine = QAEngine(RecordingChatModel(), max_input_chars=3)
        with self.assertRaises(AgentError) as blank_error:
            engine.answer("   ")
        self.assertEqual(blank_error.exception.code, "INVALID_REQUEST")

        with self.assertRaises(AgentError) as long_error:
            engine.answer("问题太长")
        self.assertEqual(long_error.exception.code, "INVALID_REQUEST")


if __name__ == "__main__":
    unittest.main()
