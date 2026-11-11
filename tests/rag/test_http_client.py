"""HTTP 客户端按线程复用，TLS 保持证书验证。"""

from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ssl
import threading
import unittest

from unittest.mock import Mock, patch

from rag.services import Qdrant, ServiceError, TLS_CONTEXT, http_opener
from rag.services import initialize_http_clients, request_json


class HttpClientTests(unittest.TestCase):
    def test_all_proxy_is_honored_and_no_proxy_uses_direct_connection(self):
        clients = []
        response = Mock(status_code=200)
        response.iter_bytes.return_value = (b'{"ok":true}',)
        try:
            with (
                ThreadPoolExecutor(
                    max_workers=1,
                    initializer=initialize_http_clients,
                    initargs=(clients, threading.Lock()),
                ) as executor,
                patch(
                    "rag.services.urllib.request.getproxies", return_value={"all": "http://proxy"}
                ),
                patch("rag.services.urllib.request.proxy_bypass", return_value=False) as bypass,
                patch("rag.services.httpx2.Client") as create,
                patch("rag.services.direct_response", return_value=b'{"ok":true}') as direct,
            ):
                create.return_value.stream.return_value.__enter__.return_value = response
                url = "https://embedding.example.test/v1/embeddings"
                self.assertEqual(executor.submit(request_json, "GET", url).result(), {"ok": True})
                direct.assert_not_called()
                create.assert_called_once()
                bypass.return_value = True
                self.assertEqual(executor.submit(request_json, "GET", url).result(), {"ok": True})
                direct.assert_called_once()
        finally:
            for client in clients:
                client.close()

    def test_local_connection_reuse_and_recovery_after_invalid_response(self):
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format, *args):
                pass

            def do_GET(self):
                status = 404 if self.path == "/missing" else 200
                body = b'{"ok": true}'
                if self.path == "/invalid":
                    body = b"invalid"
                elif self.path == "/large":
                    body = b"x" * 100
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                if self.path == "/large":
                    self.send_header("Connection", "close")
                    self.close_connection = True
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        serving = threading.Thread(target=server.serve_forever, daemon=True)
        serving.start()
        clients = []
        try:
            with ThreadPoolExecutor(
                max_workers=1,
                initializer=initialize_http_clients,
                initargs=(clients, threading.Lock()),
            ) as executor:

                def check():
                    url = f"http://127.0.0.1:{server.server_port}"
                    self.assertEqual(request_json("GET", url), {"ok": True})
                    first_socket = clients[0].sock
                    self.assertEqual(request_json("GET", url), {"ok": True})
                    self.assertIs(clients[0].sock, first_socket)
                    self.assertIsNone(request_json("GET", url + "/missing", allow_missing=True))
                    with self.assertRaisesRegex(ServiceError, "非法 JSON"):
                        request_json("GET", url + "/invalid")
                    with patch("rag.services.MAX_RESPONSE", 32):
                        with self.assertRaisesRegex(ServiceError, "大小限制"):
                            request_json("GET", url + "/large")
                    self.assertIsNone(clients[0].sock)
                    self.assertEqual(request_json("GET", url), {"ok": True})
                    self.assertEqual(len(clients), 1)

                executor.submit(check).result(timeout=10)
        finally:
            for client in clients:
                client.close()
            server.shutdown()
            server.server_close()
            serving.join(5)

    def test_opener_is_reused_within_thread_and_isolated_between_threads(self):
        main = http_opener("http://127.0.0.1")
        self.assertIs(main, http_opener("http://127.0.0.1"))
        remote = http_opener("https://example.com")
        self.assertIsNot(main, remote)
        self.assertIs(remote, http_opener("https://example.com"))
        barrier = threading.Barrier(2)

        def worker():
            first = http_opener("http://127.0.0.1")
            barrier.wait(timeout=5)
            self.assertIs(first, http_opener("http://127.0.0.1"))
            return first

        with ThreadPoolExecutor(2) as executor:
            first = executor.submit(worker)
            second = executor.submit(worker)
            a, b = first.result(), second.result()
        self.assertIsNot(a, b)
        self.assertIsNot(a, main)
        self.assertTrue(TLS_CONTEXT.check_hostname)
        self.assertEqual(TLS_CONTEXT.verify_mode, ssl.CERT_REQUIRED)


if __name__ == "__main__":
    unittest.main()


class QdrantBatchTests(unittest.TestCase):
    def test_collection_identity_is_verified_once(self):
        store = Qdrant("http://127.0.0.1:6333", "manual", "model", "https://example.com")
        info = {
            "result": {
                "config": {
                    "params": {"vectors": {"size": 2, "distance": "Cosine"}},
                    "metadata": {
                        "ragent_embedding_model": "model",
                        "ragent_embedding_endpoint_sha256": store.embedding_identity,
                    },
                }
            }
        }
        store._call = Mock(return_value=info)
        store.ensure_collection(2)
        store.ensure_collection(2)
        store._call.assert_called_once_with("GET", allow_missing=True)
        with self.assertRaises(ServiceError):
            store.ensure_collection(3)

    def test_batch_rejects_missing_or_malformed_results(self):
        store = Qdrant("http://127.0.0.1:6333", "manual", "model", "https://example.com")
        for response in (None, {}, {"result": []}, {"result": [{"points": []}, {}]}):
            with self.subTest(response=response):
                store._call = Mock(return_value=response)
                with self.assertRaisesRegex(ServiceError, "批量检索响应格式错误"):
                    store.search_multigrain([1.0], 20)
