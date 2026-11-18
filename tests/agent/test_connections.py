"""在线检索复用HTTP连接，并在关闭线程池后释放连接。"""

import asyncio
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from rag.services import ServiceError, request_json
from ragent.retrieval import ManualRetriever


class ConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_keepalive_boundaries_and_cleanup(self):
        ports = set()

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                ports.add(self.client_address[1])
                status = {
                    "/limited": 429,
                    "/missing": 404,
                    "/redirect": 302,
                }.get(self.path, 200)
                data: dict[str, bool | str] = {"ok": True}
                if self.path == "/large":
                    data["padding"] = "a" * 100
                body = json.dumps(data).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        retriever = ManualRetriever(SimpleNamespace(), SimpleNamespace(), workers=1)
        url = f"http://127.0.0.1:{server.server_port}"

        def search(query, *args):
            result = request_json("GET", url + query, allow_missing=True)
            if query == "/missing":
                self.assertIsNone(result)
            return []

        try:
            with patch("ragent.retrieval.search_linux_manual", side_effect=search):
                await retriever("/ok")
                await retriever("/ok")
                self.assertEqual(len(ports), 1)
                for path, message in (("/limited", "429"), ("/redirect", "302")):
                    with self.assertRaisesRegex(ServiceError, message):
                        await retriever(path)
                await retriever("/missing")
                with patch("rag.services.MAX_RESPONSE", 32):
                    with self.assertRaisesRegex(ServiceError, "大小限制"):
                        await retriever("/large")
                self.assertEqual(len(retriever.http_clients), 1)
        finally:
            try:
                await retriever.close()
                for client in retriever.http_clients:
                    if isinstance(client, http.client.HTTPConnection):
                        self.assertIsNone(client.sock)
                    else:
                        self.assertTrue(client.is_closed)
            finally:
                await asyncio.to_thread(server.shutdown)
                server.server_close()
                thread.join()
