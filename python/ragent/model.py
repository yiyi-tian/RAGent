"""OpenAI 兼容聊天模型适配器。"""

from __future__ import annotations

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from ragent.config import Settings


def create_chat_model(settings: Settings) -> BaseChatModel:
    """创建 LangChain ChatOpenAI 模型，可直接用于 DeepSeek 或兼容服务。"""
    return ChatOpenAI(
        model=settings.model,
        api_key=SecretStr(settings.require_api_key()),
        base_url=settings.base_url,
        temperature=settings.temperature,
        max_tokens=settings.max_output_tokens,
        timeout=settings.model_timeout_seconds,
        max_retries=1,
        streaming=False,
    )
