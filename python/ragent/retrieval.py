"""在线检索使用独立固定线程池，不占用事件循环默认工作线程。"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import contextvars
from functools import partial
import os
import time
import threading

from rag.search import search_linux_manual
from rag.services import configured_services, initialize_http_clients


class ManualRetriever:
    """异步可调用检索器；创建者在服务停止后 await close() 回收线程。"""

    def __init__(self, embedding, store, workers=None, observe=None):
        if workers is None:
            workers = int(os.environ.get("RAGENT_RETRIEVAL_WORKERS", "8"))
        if not 1 <= workers <= 64:
            raise ValueError("检索线程数必须在 1 至 64 之间")
        if getattr(store, "retrieval_strategy", "vector") == "context":
            store.prepare_search()
        self.embedding = embedding
        self.store = store
        self.observe = observe
        self.workers = workers
        self.http_clients = []
        self.executor = ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="ragent-rag",
            initializer=initialize_http_clients,
            initargs=(self.http_clients, threading.Lock()),
        )

    async def __call__(self, query: str) -> dict:
        started = time.monotonic()
        context = contextvars.copy_context()
        search = partial(search_linux_manual, query, self.embedding, self.store)

        def execute():
            running = time.monotonic()
            if self.observe is not None:
                self.observe("retrieval_wait", 0, running - started)
            try:
                return search()
            finally:
                if self.observe is not None:
                    self.observe("retrieval_execute", 0, time.monotonic() - running)

        try:
            matches = await asyncio.get_running_loop().run_in_executor(
                self.executor, context.run, execute
            )
            return format_matches(matches)
        finally:
            if self.observe is not None:
                self.observe("retrieval", 0, time.monotonic() - started)

    async def check_ready(self):
        await asyncio.to_thread(self.store.check_ready)

    async def close(self):
        def shutdown():
            self.executor.shutdown(wait=True, cancel_futures=True)
            with ExitStack() as resources:
                for client in self.http_clients:
                    resources.callback(client.close)

        await asyncio.to_thread(shutdown)


def create_retriever():
    embedding, store = configured_services()
    return ManualRetriever(embedding, store)


def format_matches(matches):
    sources = []
    excerpts = []
    for index, match in enumerate(matches, 1):
        sources.append({key: match[key] for key in ("source", "chunk_id", "text", "score")})
        excerpts.append(f"[{index}] {match['source']}\n{match['text']}")
    text = "未找到相关资料。"
    if excerpts:
        text = "检索证据：\n" + "\n\n".join(excerpts)
    return {"text": text, "sources": sources}
