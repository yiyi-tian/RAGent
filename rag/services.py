"""Embedding 和 Qdrant 的有界 HTTP 接口。"""

import hashlib
import http.client
from email.message import Message
import json
import math
import os
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import httpx2

from .runtime import RequestExpired, remaining_timeout

MAX_RESPONSE = 8 * 1024 * 1024
# TLS 配置初始化后只读共享，opener 则按线程持有，避免每次加载证书和构造处理器。
TLS_CONTEXT = ssl.create_default_context()
HTTP_STATE = threading.local()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def http_opener(url):
    local = urllib.parse.urlsplit(url).hostname in ("127.0.0.1", "localhost", "::1")
    name = "local_opener" if local else "remote_opener"
    opener = getattr(HTTP_STATE, name, None)
    if opener is None:
        handlers: list[urllib.request.BaseHandler] = [
            NoRedirect(),
            urllib.request.HTTPSHandler(context=TLS_CONTEXT),
        ]
        if local:
            # 环回请求直接连接；外部请求仍遵守启动时的系统代理配置。
            handlers.append(urllib.request.ProxyHandler({}))
        opener = urllib.request.build_opener(*handlers)
        setattr(HTTP_STATE, name, opener)
    return opener


class ServiceError(Exception):
    pass


def initialize_http_clients(clients, lock):
    """检索线程独占连接，创建者在线程退出后统一关闭。"""
    HTTP_STATE.clients = {}
    HTTP_STATE.direct_clients = {}
    HTTP_STATE.owner = clients
    HTTP_STATE.owner_lock = lock


def persistent_response(request, timeout):
    address = urllib.parse.urlsplit(request.full_url)
    local = address.hostname in ("127.0.0.1", "localhost", "::1")
    proxies = urllib.request.getproxies()
    proxy = proxies.get(address.scheme) or proxies.get("all")
    if local or not proxy or urllib.request.proxy_bypass(address.netloc):
        return direct_response(request, address, timeout, 2 if local else 30)
    origin = (address.scheme, address.hostname, address.port)
    client = HTTP_STATE.clients.get(origin)
    if client is None:
        client = httpx2.Client(
            verify=TLS_CONTEXT,
            trust_env=True,
            follow_redirects=False,
            limits=httpx2.Limits(
                max_connections=1,
                max_keepalive_connections=1,
                keepalive_expiry=30,
            ),
        )
        HTTP_STATE.clients[origin] = client
        with HTTP_STATE.owner_lock:
            HTTP_STATE.owner.append(client)
    with client.stream(
        request.method,
        request.full_url,
        content=request.data,
        headers=dict(request.header_items()),
        timeout=timeout,
    ) as response:
        if response.status_code >= 300:
            raise urllib.error.HTTPError(
                request.full_url, response.status_code, "HTTP status", Message(), None
            )
        chunks = []
        size = 0
        for chunk in response.iter_bytes():
            remaining_timeout(timeout)
            size += len(chunk)
            if size > MAX_RESPONSE:
                raise ServiceError("HTTP 响应超过大小限制")
            chunks.append(chunk)
        return b"".join(chunks)


def direct_response(request, address, timeout, keepalive_seconds):
    origin = (address.scheme, address.hostname, address.port)
    saved = HTTP_STATE.direct_clients.get(origin)
    if saved is None:
        if address.scheme == "https":
            client = http.client.HTTPSConnection(
                address.hostname, address.port, timeout=timeout, context=TLS_CONTEXT
            )
        else:
            client = http.client.HTTPConnection(address.hostname, address.port, timeout=timeout)
        with HTTP_STATE.owner_lock:
            HTTP_STATE.owner.append(client)
        HTTP_STATE.direct_clients[origin] = (client, 0)
    else:
        client, used = saved
        # Qdrant默认空闲连接时限较短，提前淘汰，不在失败后自动重试。
        if time.monotonic() - used >= keepalive_seconds:
            client.close()
        client.timeout = timeout
        if client.sock is not None:
            client.sock.settimeout(timeout)
    path = urllib.parse.urlunsplit(("", "", address.path or "/", address.query, ""))
    try:
        client.request(request.method, path, request.data, dict(request.header_items()))
        with client.getresponse() as response:
            if response.status >= 300:
                raise urllib.error.HTTPError(
                    request.full_url, response.status, "HTTP status", response.headers, None
                )
            chunks = []
            size = 0
            while size <= MAX_RESPONSE:
                remaining_timeout(timeout)
                chunk = response.read1(min(65536, MAX_RESPONSE + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
            raw = b"".join(chunks)
            if size > MAX_RESPONSE:
                raise ServiceError("HTTP 响应超过大小限制")
        HTTP_STATE.direct_clients[origin] = (client, time.monotonic())
        return raw
    except BaseException:
        client.close()
        raise


def request_json(method, url, data=None, headers=None, timeout: float = 20, allow_missing=False):
    payload = (
        None if data is None else json.dumps(data, ensure_ascii=False, allow_nan=False).encode()
    )
    request = urllib.request.Request(
        url,
        data=payload,
        method=method,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        timeout = remaining_timeout(timeout)
        if hasattr(HTTP_STATE, "clients"):
            raw = persistent_response(request, timeout)
        else:
            with http_opener(url).open(request, timeout=timeout) as response:
                chunks = []
                size = 0
                while size <= MAX_RESPONSE:
                    remaining_timeout(timeout)
                    chunk = response.read1(min(65536, MAX_RESPONSE + 1 - size))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                raw = b"".join(chunks)
        remaining_timeout(timeout)
    except RequestExpired:
        raise
    except httpx2.TimeoutException as error:
        raise ServiceError("HTTP 服务超时") from error
    except httpx2.TransportError as error:
        raise ServiceError("HTTP 服务不可用") from error
    except http.client.HTTPException as error:
        raise ServiceError("HTTP 服务响应非法或连接中断") from error
    except urllib.error.HTTPError as error:
        try:
            if allow_missing and error.code == 404:
                return None
            if error.code in (401, 403):
                raise ServiceError(f"HTTP 服务认证或权限失败（{error.code}）") from error
            if error.code == 429:
                raise ServiceError("HTTP 服务限流（429）") from error
            raise ServiceError(f"HTTP 服务返回状态 {error.code}") from error
        finally:
            error.close()
    except (OSError, TimeoutError) as error:
        reason = getattr(error, "reason", error)
        if isinstance(reason, (TimeoutError, socket.timeout)):
            raise ServiceError("HTTP 服务超时") from error
        raise ServiceError("HTTP 服务不可用") from error
    if len(raw) > MAX_RESPONSE:
        raise ServiceError("HTTP 响应超过大小限制")
    try:
        result = json.loads(raw)
    except (ValueError, UnicodeError) as error:
        raise ServiceError("HTTP 服务返回非法 JSON") from error
    if not isinstance(result, dict):
        raise ServiceError("HTTP 服务响应必须是 JSON 对象")
    return result


def _url(value, name):
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"{name} 必须是无凭据的 HTTP 地址")
    return value.rstrip("/")


class Embedding:
    def __init__(self, base_url, model, api_key, timeout: float = 20):
        self.base_url = _url(base_url, "Embedding 地址")
        address = urllib.parse.urlsplit(self.base_url)
        if address.scheme != "https" and address.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError("非本机 Embedding 地址必须使用 HTTPS")
        if not model or not api_key:
            raise ValueError("必须配置 Embedding 模型名和 API Key")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout

    def embed(self, texts):
        if not isinstance(texts, list) or not 1 <= len(texts) <= 10:
            raise ValueError("Embedding 每批须包含 1 至 10 条文本")
        if any(
            not isinstance(text, str) or not text.strip() or len(text.encode("utf-8")) > 8192
            for text in texts
        ):
            raise ValueError("Embedding 文本不能为空且 UTF-8 编码不得超过 8192 字节")
        response = request_json(
            "POST",
            self.base_url + "/embeddings",
            {"model": self.model, "input": texts},
            {"Authorization": "Bearer " + self.api_key},
            self.timeout,
        )
        if response is None:
            raise ServiceError("Embedding 服务未返回响应")
        data = response.get("data")
        if not isinstance(data, list) or len(data) != len(texts):
            raise ServiceError("Embedding 响应数量不匹配")
        vectors: list[list[float] | None] = [None] * len(texts)
        for item in data:
            if not isinstance(item, dict):
                raise ServiceError("Embedding 响应格式错误")
            index, vector = item.get("index"), item.get("embedding")
            if type(index) is not int or not 0 <= index < len(texts) or vectors[index] is not None:
                raise ServiceError("Embedding 响应索引错误")
            if (
                not isinstance(vector, list)
                or not vector
                or any(type(x) not in (int, float) or not math.isfinite(x) for x in vector)
            ):
                raise ServiceError("Embedding 向量非法")
            vectors[index] = vector
        if any(vector is None for vector in vectors):
            raise ServiceError("Embedding 响应缺少向量")
        ordered_vectors = [vector for vector in vectors if vector is not None]
        if len({len(vector) for vector in ordered_vectors}) != 1:
            raise ServiceError("Embedding 向量维度不一致")
        if self.model == "text-embedding-v4" and len(ordered_vectors[0]) != 1024:
            raise ServiceError("text-embedding-v4 返回维度不是默认的 1024")
        return ordered_vectors


class Qdrant:
    def __init__(
        self, base_url, collection, model, embedding_url, timeout: float = 20, strategy="vector"
    ):
        self.base_url = _url(base_url, "Qdrant 地址")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", collection):
            raise ValueError("集合名称格式错误")
        if strategy not in ("vector", "context"):
            raise ValueError("检索策略必须为 vector 或 context")
        self.retrieval_strategy = strategy
        self.search_index = None
        self.index_lock = threading.Lock()
        self.verified_collection = None
        self.url = self.base_url + "/collections/" + collection
        self.model = model
        self.embedding_identity = hashlib.sha256(
            _url(embedding_url, "Embedding 地址").encode()
        ).hexdigest()
        self.timeout = timeout

    def _call(self, method, path="", data=None, allow_missing=False, timeout=None):
        headers = {}
        if os.environ.get("RAGENT_QDRANT_API_KEY"):
            headers["api-key"] = os.environ["RAGENT_QDRANT_API_KEY"]
        result = request_json(
            method,
            self.url + path,
            data,
            headers,
            self.timeout if timeout is None else timeout,
            allow_missing,
        )
        if result is not None and result.get("status") != "ok":
            raise ServiceError("Qdrant 返回错误状态")
        return result

    def ensure_collection(self, dimension, create=False):
        identity = (self.url, self.model, self.embedding_identity, dimension)
        if self.verified_collection == identity:
            return
        info = self._call("GET", allow_missing=True)
        if info is None:
            if not create:
                raise ServiceError("Qdrant 集合不存在，请先导入")
            self._call(
                "PUT",
                data={
                    "vectors": {"size": dimension, "distance": "Cosine"},
                    "metadata": {
                        "ragent_embedding_model": self.model,
                        "ragent_embedding_endpoint_sha256": self.embedding_identity,
                    },
                },
            )
            info = self._call("GET")
            if info is None:
                raise ServiceError("Qdrant 创建后未返回集合信息")
        self._validate_collection(info, dimension)
        self.verified_collection = identity

    def _validate_collection(self, info, dimension):
        try:
            config = info["result"]["config"]
            vectors = config["params"]["vectors"]
            metadata = config["metadata"]
            if (
                type(vectors["size"]) is not int
                or vectors["size"] <= 0
                or vectors["size"] != dimension
                or vectors["distance"].lower() != "cosine"
                or metadata["ragent_embedding_model"] != self.model
                or metadata["ragent_embedding_endpoint_sha256"] != self.embedding_identity
            ):
                raise ServiceError("Qdrant 集合模型或维度不匹配")
        except (KeyError, TypeError, AttributeError) as error:
            raise ServiceError("Qdrant 集合缺少可信的模型或维度信息") from error

    def check_ready(self):
        info = self._call("GET", timeout=2)
        try:
            dimension = info["result"]["config"]["params"]["vectors"]["size"]
        except (KeyError, TypeError) as error:
            raise ServiceError("集合未返回有效的向量配置") from error
        expected = dimension if self.verified_collection is None else self.verified_collection[-1]
        if self.model == "text-embedding-v4":
            expected = 1024
        self._validate_collection(info, expected)
        if self.retrieval_strategy == "context" and self.search_index is None:
            raise ServiceError("检索索引尚未加载")

    def verify_sources(self, chunks):
        by_id = {chunk["chunk_id"]: chunk for chunk in chunks}
        expected = {}
        for chunk in chunks:
            expected.setdefault(chunk["source"], set()).add(chunk["chunk_id"])
        for source, ids in expected.items():
            offset = None
            while True:
                request = {
                    "filter": {"must": [{"key": "source", "match": {"value": source}}]},
                    "limit": 128,
                    "with_payload": True,
                    "with_vector": False,
                }
                if offset is not None:
                    request["offset"] = offset
                result = self._call("POST", "/points/scroll", request)
                if result is None:
                    raise ServiceError("Qdrant 未返回来源检查结果")
                try:
                    page = result["result"]
                    existing_ids = {str(point["id"]) for point in page["points"]}
                    next_offset = page["next_page_offset"]
                except (KeyError, TypeError) as error:
                    raise ServiceError("Qdrant 来源检查响应格式错误") from error
                if not existing_ids.issubset(ids):
                    raise ServiceError(f"{source} 手册内容或版本已变化，请使用新集合")
                for point in page["points"]:
                    try:
                        actual = point["payload"]
                        wanted = by_id[str(point["id"])]
                        keys = ("language", "package", "package_version", "license_file")
                        if any(actual.get(key) != wanted.get(key) for key in keys):
                            raise ServiceError(f"{source} 来源版本已变化，请使用新集合")
                    except (KeyError, TypeError, AttributeError) as error:
                        raise ServiceError("Qdrant 来源元数据格式错误") from error
                if next_offset is None:
                    break
                if next_offset == offset:
                    raise ServiceError("Qdrant 来源检查分页异常")
                offset = next_offset

    def upsert(self, chunks, vectors):
        points = [
            {"id": chunk["chunk_id"], "vector": vector, "payload": chunk}
            for chunk, vector in zip(chunks, vectors, strict=True)
        ]
        self._call("PUT", "/points?wait=true", {"points": points})

    @staticmethod
    def _point_payload(point):
        payload = point["payload"]
        if not isinstance(payload, dict) or any(
            not isinstance(payload.get(key), str) for key in ("source", "chunk_id", "text")
        ):
            raise ValueError
        if payload.get("chunking_version") in ("paragraph-v2", "entry-v3"):
            start, end = payload.get("start"), payload.get("end")
            body, digest = payload.get("body"), payload.get("document_sha256")
            if (
                type(start) is not int
                or type(end) is not int
                or start < 0
                or end <= start
                or not isinstance(body, str)
                or len(body) != end - start
                or not isinstance(payload.get("section"), str)
                or not isinstance(digest, str)
                or re.fullmatch(r"[a-f0-9]{64}", digest) is None
            ):
                raise ValueError
        if payload.get("chunking_version") == "entry-v3":
            left, right = payload.get("entry_start"), payload.get("entry_end")
            if (
                type(left) is not int
                or type(right) is not int
                or not 0 <= left <= payload["start"] < payload["end"] <= right
                or not isinstance(payload.get("entry"), str)
                or not payload["entry"].strip()
            ):
                raise ValueError
        return payload

    def prepare_search(self):
        if self.retrieval_strategy != "context" or self.search_index is not None:
            return
        from .context import ManualContext

        with self.index_lock:
            if self.search_index is not None:
                return
            info = self._call("GET")
            try:
                dimension = info["result"]["config"]["params"]["vectors"]["size"]
            except (KeyError, TypeError) as error:
                raise ServiceError("Qdrant 索引缺少向量配置") from error
            self.ensure_collection(dimension)
            chunks = []
            offset = None
            while True:
                request = {"limit": 128, "with_payload": True, "with_vector": False}
                if offset is not None:
                    request["offset"] = offset
                response = self._call("POST", "/points/scroll", request)
                try:
                    page = response["result"]
                    for point in page["points"]:
                        chunks.append(self._point_payload(point))
                    next_offset = page["next_page_offset"]
                except (KeyError, TypeError, ValueError) as error:
                    raise ServiceError("Qdrant 索引载荷格式错误") from error
                if next_offset is None:
                    break
                if next_offset == offset:
                    raise ServiceError("Qdrant 索引分页异常")
                offset = next_offset
            self.search_index = ManualContext(chunks)

    def search(self, vector, limit, chunking_version=None):
        request = {"query": vector, "limit": limit, "with_payload": True}
        if chunking_version is not None:
            request["filter"] = {
                "must": [{"key": "chunking_version", "match": {"value": chunking_version}}]
            }
        result = self._call("POST", "/points/query", request)
        if result is None:
            raise ServiceError("Qdrant 未返回检索结果")
        try:
            return self._matches(result["result"]["points"])
        except (KeyError, TypeError, ValueError) as error:
            raise ServiceError("Qdrant 检索响应格式错误") from error

    def search_multigrain(self, vector, limit):
        indexed = self.search_index is not None
        searches = []
        for version in ("paragraph-v2", "entry-v3"):
            searches.append(
                {
                    "query": vector,
                    "limit": limit,
                    # 正文已在启动时校验并加载，在线仅需实时命中ID和分数。
                    "with_payload": not indexed,
                    "filter": {"must": [{"key": "chunking_version", "match": {"value": version}}]},
                }
            )
        response = self._call("POST", "/points/query/batch", {"searches": searches})
        try:
            result = response["result"]
            if not isinstance(result, list) or len(result) != 2:
                raise ValueError
            return (
                self._matches(result[0]["points"], indexed),
                self._matches(result[1]["points"], indexed),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ServiceError("Qdrant 批量检索响应格式错误") from error

    def _matches(self, points, indexed=False):
        if not isinstance(points, list):
            raise ValueError
        matches = []
        for point in points:
            if indexed:
                payload = self.search_index.chunks[str(point["id"])]
            else:
                payload = self._point_payload(point)
            score = point["score"]
            if type(score) not in (int, float) or not math.isfinite(score):
                raise ValueError
            matches.append({**payload, "score": score})
        return matches


def configured_services():
    """从环境变量创建 Embedding 与 Qdrant 客户端，不读取或记录密钥文件。"""
    base_url = os.environ.get("RAGENT_EMBEDDING_BASE_URL", "")
    model = os.environ.get("RAGENT_EMBEDDING_MODEL", "text-embedding-v4")
    key = os.environ.get("RAGENT_EMBEDDING_API_KEY", "")
    embedding = Embedding(base_url, model, key)
    store = Qdrant(
        os.environ.get("RAGENT_QDRANT_URL", "http://127.0.0.1:6333"),
        os.environ.get("RAGENT_QDRANT_COLLECTION", "linux_manual_v4"),
        model,
        base_url,
        strategy=os.environ.get("RAGENT_RETRIEVAL_STRATEGY", "context"),
    )
    return embedding, store
