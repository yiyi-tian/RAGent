"""兼容现有 C 客户端的异步 TCP Agent 服务。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from ragent.config import Settings
from ragent.errors import AgentError, protocol_error_for
from ragent.protocol import (
    CMD_REQUEST,
    PING,
    PONG,
    RAG_REQUEST,
    SUPPORTED_REQUEST_TYPES,
    FrameHeaderError,
    InvalidRequest,
    decode_json_object,
    encode_error,
    encode_frame,
    encode_response,
    read_frame,
    require_query,
)

LOGGER = logging.getLogger(__name__)


class PythonAgentServer:
    def __init__(
        self,
        settings: Settings,
        answer_question: Callable[[str], str],
    ) -> None:
        self._settings = settings
        self._answer_question = answer_question
        self._model_slots = asyncio.Semaphore(settings.max_concurrency)
        self._active_connections = 0

    async def create_server(self) -> asyncio.AbstractServer:
        return await asyncio.start_server(
            self._handle_client,
            self._settings.host,
            self._settings.port,
            limit=1024 * 1024 + 20,
        )

    async def serve_forever(self) -> None:
        server = await self.create_server()
        addresses = ", ".join(str(sock.getsockname()) for sock in server.sockets or [])
        LOGGER.info("Python Agent 正在监听 %s", addresses)
        async with server:
            await server.serve_forever()

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        if self._active_connections >= self._settings.max_connections:
            writer.close()
            await writer.wait_closed()
            return

        self._active_connections += 1
        peer = writer.get_extra_info("peername")
        last_request_id = 0
        try:
            while True:
                try:
                    frame = await read_frame(reader)
                except (asyncio.IncompleteReadError, ConnectionResetError):
                    break
                except FrameHeaderError as exc:
                    LOGGER.warning("关闭协议非法连接 %s: %s", peer, exc)
                    break

                if frame.request_id <= last_request_id:
                    LOGGER.warning("关闭重复或倒退 request_id 的连接 %s", peer)
                    break
                last_request_id = frame.request_id

                response = await self._handle_frame(frame.message_type, frame.request_id, frame.body)
                try:
                    writer.write(response)
                    await writer.drain()
                except (ConnectionResetError, BrokenPipeError):
                    break
        finally:
            self._active_connections -= 1
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionResetError, BrokenPipeError):
                pass

    async def _handle_frame(
        self,
        message_type: int,
        request_id: int,
        body: bytes,
    ) -> bytes:
        if message_type == PING:
            try:
                decode_json_object(body)
            except InvalidRequest as exc:
                return encode_error(request_id, "INVALID_REQUEST", str(exc))
            return encode_frame(PONG, request_id, {})
        if message_type not in SUPPORTED_REQUEST_TYPES:
            if message_type in (RAG_REQUEST, CMD_REQUEST):
                return encode_error(
                    request_id,
                    "INVALID_REQUEST",
                    "当前服务仅支持 CHAT_REQUEST 和 AGENT_REQUEST。",
                )
            return encode_error(
                request_id,
                "UNSUPPORTED_MESSAGE_TYPE",
                "不支持的消息类型。",
            )

        try:
            query = require_query(decode_json_object(body))
        except InvalidRequest as exc:
            return encode_error(request_id, "INVALID_REQUEST", str(exc))

        try:
            await asyncio.wait_for(
                self._model_slots.acquire(),
                timeout=self._settings.queue_timeout_seconds,
            )
        except TimeoutError:
            return encode_error(request_id, "SERVER_BUSY", "模型请求队列已满，请稍后重试。")

        try:
            text = await asyncio.to_thread(self._answer_question, query)
            return encode_response(request_id, text)
        except Exception as exc:
            mapped = protocol_error_for(exc)
            if mapped.code == "INTERNAL_ERROR":
                LOGGER.exception("处理请求时发生未映射异常")
            else:
                LOGGER.warning("模型请求失败: %s", mapped.code)
            return encode_error(request_id, mapped.code, mapped.message)
        finally:
            self._model_slots.release()
