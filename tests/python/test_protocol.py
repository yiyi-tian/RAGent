from __future__ import annotations

import asyncio
import json
import unittest

from ragent.protocol import (
    CHAT_REQUEST,
    ERROR,
    HEADER,
    InvalidRequest,
    RequestFrame,
    decode_json_object,
    encode_error,
    encode_frame,
    read_frame,
    require_query,
)


class ProtocolTests(unittest.TestCase):
    def test_chat_header_matches_documented_example(self) -> None:
        encoded = encode_frame(CHAT_REQUEST, 1, {"query": "hello"})
        expected_header = bytes.fromhex(
            "52 41 47 54 00 01 00 01 00 00 00 11 00 00 00 00 00 00 00 01"
        )
        self.assertEqual(encoded[:20], expected_header)
        self.assertEqual(encoded[20:], b'{"query":"hello"}')

    def test_chinese_body_length_counts_utf8_bytes(self) -> None:
        encoded = encode_frame(CHAT_REQUEST, 9, {"query": "你好"})
        _, _, _, body_length, request_id = HEADER.unpack(encoded[:20])
        self.assertEqual(body_length, len('{"query":"你好"}'.encode("utf-8")))
        self.assertEqual(request_id, 9)

    def test_error_frame_has_stable_shape(self) -> None:
        encoded = encode_error(7, "UPSTREAM_TIMEOUT", "超时")
        message_type, request_id, payload = decode_frame_for_test(encoded)
        self.assertEqual(message_type, ERROR)
        self.assertEqual(request_id, 7)
        self.assertEqual(
            payload,
            {"code": "UPSTREAM_TIMEOUT", "message": "超时"},
        )

    def test_invalid_json_and_query_are_rejected(self) -> None:
        with self.assertRaises(InvalidRequest):
            decode_json_object(b"\xff")
        with self.assertRaises(InvalidRequest):
            decode_json_object(b"[]")
        with self.assertRaises(InvalidRequest):
            require_query({"query": " \r\n\t"})
        with self.assertRaises(InvalidRequest):
            require_query({"query": 1})


class AsyncProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_reader_accepts_body_split_across_writes(self) -> None:
        encoded = encode_frame(CHAT_REQUEST, 10, {"query": "分段"})
        reader = asyncio.StreamReader()
        reader.feed_data(encoded[:7])
        reader.feed_data(encoded[7:])
        reader.feed_eof()

        frame = await read_frame(reader)

        self.assertEqual(
            frame,
            RequestFrame(CHAT_REQUEST, 10, '{"query":"分段"}'.encode("utf-8")),
        )


def decode_frame_for_test(encoded: bytes) -> tuple[int, int, dict[str, object]]:
    _, _, message_type, body_length, request_id = HEADER.unpack(encoded[:20])
    body = encoded[20 : 20 + body_length]
    return message_type, request_id, json.loads(body)


if __name__ == "__main__":
    unittest.main()
