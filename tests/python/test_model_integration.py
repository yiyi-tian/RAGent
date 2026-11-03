from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ragent.config import Settings
from ragent.model import create_chat_model
from ragent.qa import QAEngine


class OpenAICompatibleHandler(BaseHTTPRequestHandler):
    request_body: dict[str, object] = {}
    authorization: str | None = None

    def do_POST(self) -> None:
        content_length = int(self.headers["Content-Length"])
        type(self).request_body = json.loads(self.rfile.read(content_length))
        type(self).authorization = self.headers.get("Authorization")
        response = {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 1,
            "model": "test-model",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "本地假模型回答"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
            },
        }
        encoded = json.dumps(response, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args) -> None:
        return


class ModelIntegrationTests(unittest.TestCase):
    def test_langchain_chat_openai_round_trip(self) -> None:
        http_server = ThreadingHTTPServer(("127.0.0.1", 0), OpenAICompatibleHandler)
        thread = threading.Thread(target=http_server.serve_forever, daemon=True)
        thread.start()
        try:
            port = http_server.server_address[1]
            settings = Settings(
                model="test-model",
                base_url=f"http://127.0.0.1:{port}/v1",
                api_key="local-test-key",
                temperature=0,
                max_output_tokens=32,
                model_timeout_seconds=5,
            )
            engine = QAEngine(create_chat_model(settings))

            answer = engine.answer("测试问题")

            self.assertEqual(answer, "本地假模型回答")
            self.assertEqual(OpenAICompatibleHandler.authorization, "Bearer local-test-key")
            self.assertEqual(OpenAICompatibleHandler.request_body["model"], "test-model")
            messages = OpenAICompatibleHandler.request_body["messages"]
            self.assertEqual(messages[-1], {"role": "user", "content": "测试问题"})
        finally:
            http_server.shutdown()
            http_server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
