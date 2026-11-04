"""Embedding 和 Qdrant 的有界 HTTP 接口。"""
import hashlib
import json
import math
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request

MAX_RESPONSE = 8 * 1024 * 1024


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ServiceError(Exception):
    pass


def request_json(method, url, data=None, headers=None, timeout: float = 20, allow_missing=False):
    payload = None if data is None else json.dumps(data, ensure_ascii=False, allow_nan=False).encode()
    request = urllib.request.Request(url, data=payload, method=method,
                                     headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE + 1)
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
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
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
        if any(not isinstance(text, str) or not text.strip() or len(text.encode("utf-8")) > 8192 for text in texts):
            raise ValueError("Embedding 文本不能为空且 UTF-8 编码不得超过 8192 字节")
        response = request_json("POST", self.base_url + "/embeddings",
                                {"model": self.model, "input": texts},
                                {"Authorization": "Bearer " + self.api_key}, self.timeout)
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
            if not isinstance(vector, list) or not vector or any(type(x) not in (int, float) or not math.isfinite(x) for x in vector):
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
    def __init__(self, base_url, collection, model, embedding_url, timeout: float = 20):
        self.base_url = _url(base_url, "Qdrant 地址")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", collection):
            raise ValueError("集合名称格式错误")
        self.url = self.base_url + "/collections/" + collection
        self.model = model
        self.embedding_identity = hashlib.sha256(_url(embedding_url, "Embedding 地址").encode()).hexdigest()
        self.timeout = timeout

    def _call(self, method, path="", data=None, allow_missing=False):
        headers = {}
        if os.environ.get("RAGENT_QDRANT_API_KEY"):
            headers["api-key"] = os.environ["RAGENT_QDRANT_API_KEY"]
        result = request_json(method, self.url + path, data, headers, self.timeout, allow_missing)
        if result is not None and result.get("status") != "ok":
            raise ServiceError("Qdrant 返回错误状态")
        return result

    def ensure_collection(self, dimension, create=False):
        info = self._call("GET", allow_missing=True)
        if info is None:
            if not create:
                raise ServiceError("Qdrant 集合不存在，请先导入")
            self._call("PUT", data={"vectors": {"size": dimension, "distance": "Cosine"},
                                    "metadata": {"ragent_embedding_model": self.model, "ragent_embedding_endpoint_sha256": self.embedding_identity}})
            info = self._call("GET")
            if info is None:
                raise ServiceError("Qdrant 创建后未返回集合信息")
        try:
            config = info["result"]["config"]
            vectors = config["params"]["vectors"]
            metadata = config["metadata"]
            if vectors["size"] != dimension or vectors["distance"].lower() != "cosine" or metadata["ragent_embedding_model"] != self.model or metadata["ragent_embedding_endpoint_sha256"] != self.embedding_identity:
                raise ServiceError("Qdrant 集合模型或维度不匹配")
        except (KeyError, TypeError, AttributeError) as error:
            raise ServiceError("Qdrant 集合缺少可信的模型或维度信息") from error

    def verify_sources(self, chunks):
        expected = {}
        for chunk in chunks:
            expected.setdefault(chunk["source"], set()).add(chunk["chunk_id"])
        for source, ids in expected.items():
            offset = None
            while True:
                request = {"filter": {"must": [{"key": "source", "match": {"value": source}}]},
                           "limit": 128, "with_payload": False, "with_vector": False}
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
                if next_offset is None:
                    break
                if next_offset == offset:
                    raise ServiceError("Qdrant 来源检查分页异常")
                offset = next_offset

    def upsert(self, chunks, vectors):
        points = [{"id": chunk["chunk_id"], "vector": vector, "payload": chunk}
                  for chunk, vector in zip(chunks, vectors, strict=True)]
        self._call("PUT", "/points?wait=true", {"points": points})

    def search(self, vector, limit):
        result = self._call("POST", "/points/query", {"query": vector, "limit": limit, "with_payload": True})
        if result is None:
            raise ServiceError("Qdrant 未返回检索结果")
        try:
            points = result["result"]["points"]
            if not isinstance(points, list):
                raise ValueError
            matches = []
            for point in points:
                payload, score = point["payload"], point["score"]
                if not isinstance(payload, dict) or any(not isinstance(payload.get(key), str) for key in ("source", "chunk_id", "text")):
                    raise ValueError
                if type(score) not in (int, float) or not math.isfinite(score):
                    raise ValueError
                matches.append({**payload, "score": score})
            return matches
        except (KeyError, TypeError, ValueError) as error:
            raise ServiceError("Qdrant 检索响应格式错误") from error


def configured_services():
    """从环境变量创建 Embedding 与 Qdrant 客户端，不读取或记录密钥文件。"""
    base_url = os.environ.get("RAGENT_EMBEDDING_BASE_URL", "")
    model = os.environ.get("RAGENT_EMBEDDING_MODEL", "text-embedding-v4")
    key = os.environ.get("RAGENT_EMBEDDING_API_KEY", "")
    embedding = Embedding(base_url, model, key)
    store = Qdrant(os.environ.get("RAGENT_QDRANT_URL", "http://127.0.0.1:6333"),
                   os.environ.get("RAGENT_QDRANT_COLLECTION", "linux_manual_v1"), model, base_url)
    return embedding, store
