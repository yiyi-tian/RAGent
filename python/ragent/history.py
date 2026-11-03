"""本地交互模式使用的小型有界对话历史。"""

from __future__ import annotations

from collections import deque

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage


class ConversationHistory:
    def __init__(self, max_turns: int) -> None:
        self._messages: deque[BaseMessage] = deque(maxlen=max_turns * 2)

    def messages(self) -> tuple[BaseMessage, ...]:
        return tuple(self._messages)

    def append(self, question: str, answer: str) -> None:
        self._messages.extend((HumanMessage(content=question), AIMessage(content=answer)))

    def clear(self) -> None:
        self._messages.clear()
