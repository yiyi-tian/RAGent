"""从环境变量读取运行配置。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from ragent.errors import ConfigurationError


def _read_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"环境变量 {name} 必须是整数。") from exc
    if not minimum <= value <= maximum:
        raise ConfigurationError(
            f"环境变量 {name} 必须位于 {minimum} 到 {maximum} 之间。"
        )
    return value


def _read_float(name: str, default: float, minimum: float, maximum: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"环境变量 {name} 必须是数字。") from exc
    if not minimum <= value <= maximum:
        raise ConfigurationError(
            f"环境变量 {name} 必须位于 {minimum:g} 到 {maximum:g} 之间。"
        )
    return value


def _first_environment_value(*names: str) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value is not None and value.strip():
            return value.strip()
    return None


@dataclass(frozen=True)
class Settings:
    """项目运行参数；API 密钥不进入对象表示。"""

    model: str = "deepseek-chat"
    base_url: str = "https://api.deepseek.com"
    api_key: str | None = field(default=None, repr=False)
    temperature: float = 0.2
    max_output_tokens: int = 2048
    model_timeout_seconds: float = 60.0
    host: str = "127.0.0.1"
    port: int = 9000
    max_connections: int = 128
    max_concurrency: int = 4
    queue_timeout_seconds: float = 5.0
    max_input_chars: int = 16000
    history_turns: int = 6

    @classmethod
    def from_env(cls) -> "Settings":
        model = os.getenv("RAGENT_MODEL", "deepseek-chat").strip()
        base_url = os.getenv("RAGENT_BASE_URL", "https://api.deepseek.com").strip()
        if not model:
            raise ConfigurationError("环境变量 RAGENT_MODEL 不能为空。")
        if not base_url.startswith(("http://", "https://")):
            raise ConfigurationError("环境变量 RAGENT_BASE_URL 必须是 HTTP(S) 地址。")

        return cls(
            model=model,
            base_url=base_url.rstrip("/"),
            api_key=_first_environment_value(
                "RAGENT_API_KEY",
                "DEEPSEEK_API_KEY",
                "OPENAI_API_KEY",
            ),
            temperature=_read_float("RAGENT_TEMPERATURE", 0.2, 0.0, 2.0),
            max_output_tokens=_read_int(
                "RAGENT_MAX_OUTPUT_TOKENS", 2048, minimum=1, maximum=32768
            ),
            model_timeout_seconds=_read_float(
                "RAGENT_MODEL_TIMEOUT_SECONDS", 60.0, minimum=1.0, maximum=600.0
            ),
            host=os.getenv("RAGENT_HOST", "127.0.0.1").strip(),
            port=_read_int("RAGENT_PORT", 9000, minimum=1, maximum=65535),
            max_connections=_read_int(
                "RAGENT_MAX_CONNECTIONS", 128, minimum=1, maximum=10000
            ),
            max_concurrency=_read_int(
                "RAGENT_MAX_CONCURRENCY", 4, minimum=1, maximum=256
            ),
            queue_timeout_seconds=_read_float(
                "RAGENT_QUEUE_TIMEOUT_SECONDS", 5.0, minimum=0.1, maximum=300.0
            ),
            max_input_chars=_read_int(
                "RAGENT_MAX_INPUT_CHARS", 16000, minimum=1, maximum=1000000
            ),
            history_turns=_read_int(
                "RAGENT_HISTORY_TURNS", 6, minimum=0, maximum=100
            ),
        )

    def require_api_key(self) -> str:
        if self.api_key is None:
            raise ConfigurationError(
                "未配置模型密钥。请设置 RAGENT_API_KEY 或 DEEPSEEK_API_KEY。"
            )
        return self.api_key
