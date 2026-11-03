"""LangChain 提示链和单次问答入口。"""

from __future__ import annotations

from collections.abc import Sequence

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import BaseMessage
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from ragent.errors import AgentError

SYSTEM_PROMPT = """你是 RAGent，一个使用中文回答问题的智能助手。
回答应准确、直接，并说明必要的前提或不确定性。
当前版本没有联网检索、知识库检索或系统命令执行能力。
不要声称已经查阅资料、执行命令或访问了用户未提供的上下文。"""


class QAEngine:
    """把查询、可选历史消息和最终文本连接成一条 LangChain 链。"""

    def __init__(
        self,
        model: BaseChatModel,
        *,
        system_prompt: str = SYSTEM_PROMPT,
        max_input_chars: int = 16000,
    ) -> None:
        prompt = ChatPromptTemplate.from_messages(
            [
                ("system", system_prompt),
                MessagesPlaceholder("history", optional=True),
                ("human", "{query}"),
            ]
        )
        self._chain = prompt | model | StrOutputParser()
        self._max_input_chars = max_input_chars

    def answer(
        self,
        query: str,
        history: Sequence[BaseMessage] = (),
    ) -> str:
        """回答一次问题；调用方负责决定是否保留历史消息。"""
        if not query.strip():
            raise AgentError("INVALID_REQUEST", "query 必须是非空白字符串。")
        if len(query) > self._max_input_chars:
            raise AgentError(
                "INVALID_REQUEST",
                f"query 不能超过 {self._max_input_chars} 个字符。",
            )

        text = self._chain.invoke(
            {"query": query, "history": list(history)},
            config={"run_name": "ragent_qa"},
        )
        if not text.strip():
            raise AgentError("UPSTREAM_UNAVAILABLE", "模型返回了空回答。")
        return text
