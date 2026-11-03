"""项目错误及其到 TCP 错误码的映射。"""

from __future__ import annotations


class AgentError(Exception):
    """可安全返回给客户端的业务错误。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ConfigurationError(AgentError):
    """配置缺失或取值非法。"""

    def __init__(self, message: str) -> None:
        super().__init__("INTERNAL_ERROR", message)


def protocol_error_for(exception: Exception) -> AgentError:
    """把 LangChain/OpenAI 兼容服务的异常映射为稳定错误码。"""
    if isinstance(exception, AgentError):
        return exception

    try:
        import openai
    except ImportError:
        return AgentError("INTERNAL_ERROR", "Agent 内部处理失败。")

    if isinstance(exception, openai.AuthenticationError | openai.PermissionDeniedError):
        return AgentError("UPSTREAM_UNAVAILABLE", "模型服务认证失败，请检查配置。")
    if isinstance(exception, openai.RateLimitError):
        return AgentError("SERVER_BUSY", "模型服务繁忙，请稍后重试。")
    if isinstance(exception, openai.APITimeoutError):
        return AgentError("UPSTREAM_TIMEOUT", "模型服务响应超时。")
    if isinstance(exception, openai.APIConnectionError):
        return AgentError("UPSTREAM_UNAVAILABLE", "无法连接模型服务。")
    if isinstance(exception, openai.APIStatusError):
        return AgentError("UPSTREAM_UNAVAILABLE", "模型服务返回失败。")
    return AgentError("INTERNAL_ERROR", "Agent 内部处理失败。")
