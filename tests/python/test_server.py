from __future__ import annotations

import asyncio
import unittest
from dataclasses import replace

from ragent.config import Settings
from ragent.errors import AgentError
from ragent.protocol import (
    AGENT_REQUEST,
    CHAT_REQUEST,
    ERROR,
    PING,
    PONG,
    RESPONSE,
    RAG_REQUEST,
    decode_json_object,
    encode_frame,
    read_frame,
)
from ragent.server import PythonAgentServer


class ServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        settings = replace(
            Settings(),
            host="127.0.0.1",
            port=0,
            max_concurrency=1,
            queue_timeout_seconds=0.1,
        )
        self.server = PythonAgentServer(settings, self._answer)
        self.asyncio_server = await self.server.create_server()
        self.port = self.asyncio_server.sockets[0].getsockname()[1]

    async def asyncTearDown(self) -> None:
        self.asyncio_server.close()
        await self.asyncio_server.wait_closed()

    @staticmethod
    def _answer(question: str) -> str:
        if question == "上游失败":
            raise AgentError("UPSTREAM_TIMEOUT", "模型服务响应超时。")
        return f"回答: {question}"

    async def test_chat_agent_and_ping(self) -> None:
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        writer.write(encode_frame(CHAT_REQUEST, 1, {"query": "问题一"}))
        message_type, request_id, payload = await self._read_message(reader)
        self.assertEqual((message_type, request_id), (RESPONSE, 1))
        self.assertEqual(payload, {"text": "回答: 问题一", "sources": []})

        # 将帧拆成两段发送，验证服务端处理 TCP 半包。
        frame = encode_frame(AGENT_REQUEST, 2, {"query": "问题二"})
        writer.write(frame[:8])
        await writer.drain()
        writer.write(frame[8:])
        message_type, request_id, payload = await self._read_message(reader)
        self.assertEqual((message_type, request_id), (RESPONSE, 2))
        self.assertEqual(payload["text"], "回答: 问题二")

        writer.write(encode_frame(PING, 3, {}))
        message_type, request_id, payload = await self._read_message(reader)
        self.assertEqual((message_type, request_id, payload), (PONG, 3, {}))

        writer.close()
        await writer.wait_closed()

    async def test_invalid_json_returns_error_and_connection_continues(self) -> None:
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        writer.write(encode_frame(CHAT_REQUEST, 1, {"query": 1}))
        message_type, request_id, payload = await self._read_message(reader)
        self.assertEqual((message_type, request_id), (ERROR, 1))
        self.assertEqual(payload["code"], "INVALID_REQUEST")

        writer.write(encode_frame(CHAT_REQUEST, 2, {"query": "继续"}))
        message_type, request_id, payload = await self._read_message(reader)
        self.assertEqual((message_type, request_id), (RESPONSE, 2))
        self.assertEqual(payload["text"], "回答: 继续")

        writer.close()
        await writer.wait_closed()

    async def test_ping_body_must_be_a_json_object(self) -> None:
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        writer.write(encode_frame(PING, 1, []))
        message_type, request_id, payload = await self._read_message(reader)
        self.assertEqual((message_type, request_id), (ERROR, 1))
        self.assertEqual(payload["code"], "INVALID_REQUEST")

        writer.close()
        await writer.wait_closed()

    async def test_unavailable_mode_and_upstream_error(self) -> None:
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        writer.write(encode_frame(RAG_REQUEST, 1, {"query": "知识库"}))
        _, _, payload = await self._read_message(reader)
        self.assertEqual(payload["code"], "INVALID_REQUEST")

        writer.write(encode_frame(CHAT_REQUEST, 2, {"query": "上游失败"}))
        _, _, payload = await self._read_message(reader)
        self.assertEqual(payload["code"], "UPSTREAM_TIMEOUT")

        writer.close()
        await writer.wait_closed()

    async def test_duplicate_request_id_closes_connection(self) -> None:
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        writer.write(encode_frame(CHAT_REQUEST, 1, {"query": "第一次"}))
        await self._read_message(reader)
        writer.write(encode_frame(CHAT_REQUEST, 1, {"query": "重复"}))
        self.assertEqual(await reader.read(), b"")
        writer.close()
        await writer.wait_closed()

    @staticmethod
    async def _read_message(
        reader: asyncio.StreamReader,
    ) -> tuple[int, int, dict[str, object]]:
        frame = await read_frame(reader)
        return (
            frame.message_type,
            frame.request_id,
            decode_json_object(frame.body),
        )


if __name__ == "__main__":
    unittest.main()
