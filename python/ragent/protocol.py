"""RAGent TCP 协议 v1 的 Python 编解码。"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass
from typing import Any

import asyncio

MAGIC = 0x52414754
VERSION = 1
HEADER = struct.Struct(">IHHIQ")
HEADER_SIZE = HEADER.size
MAX_BODY_SIZE = 1024 * 1024

CHAT_REQUEST = 1
RAG_REQUEST = 2
CMD_REQUEST = 3
AGENT_REQUEST = 4
RESPONSE = 100
ERROR = 101
PING = 200
PONG = 201
SUPPORTED_REQUEST_TYPES = frozenset((CHAT_REQUEST, AGENT_REQUEST))


class FrameHeaderError(Exception):
    """消息头不可信，连接必须关闭。"""


class InvalidRequest(ValueError):
    """消息边界可信，但消息体不满足业务约定。"""


@dataclass(frozen=True)
class RequestFrame:
    message_type: int
    request_id: int
    body: bytes


def encode_frame(message_type: int, request_id: int, payload: dict[str, Any]) -> bytes:
    body = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(body) > MAX_BODY_SIZE:
        raise ValueError("消息体超过 1 MiB 限制。")
    return HEADER.pack(MAGIC, VERSION, message_type, len(body), request_id) + body


def encode_response(request_id: int, text: str) -> bytes:
    return encode_frame(RESPONSE, request_id, {"text": text, "sources": []})


def encode_error(request_id: int, code: str, message: str) -> bytes:
    return encode_frame(ERROR, request_id, {"code": code, "message": message})


async def read_frame(reader: asyncio.StreamReader) -> RequestFrame:
    header = await reader.readexactly(HEADER_SIZE)
    magic, version, message_type, body_length, request_id = HEADER.unpack(header)
    if magic != MAGIC:
        raise FrameHeaderError("magic 不匹配")
    if version != VERSION:
        raise FrameHeaderError("协议版本不支持")
    if body_length > MAX_BODY_SIZE:
        raise FrameHeaderError("消息体超过上限")
    body = await reader.readexactly(body_length)
    return RequestFrame(message_type, request_id, body)


def decode_json_object(body: bytes) -> dict[str, Any]:
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InvalidRequest("消息体不是合法 UTF-8。") from exc
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InvalidRequest("消息体不是合法 JSON。") from exc
    if not isinstance(value, dict):
        raise InvalidRequest("消息体必须是 JSON 对象。")
    return value


def require_query(payload: dict[str, Any]) -> str:
    query = payload.get("query")
    if not isinstance(query, str) or not query.strip():
        raise InvalidRequest("query 必须是非空白字符串。")
    return query
