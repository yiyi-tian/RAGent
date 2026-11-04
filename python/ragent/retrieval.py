"""把离线模块的同步检索接入在线工具。"""

import asyncio

from rag.search import search_linux_manual
from rag.services import configured_services


def create_retriever():
    embedding, store = configured_services()

    async def retrieve(query: str) -> dict:
        matches = await asyncio.to_thread(search_linux_manual, query, embedding, store)
        sources = []
        for match in matches:
            sources.append({key: match[key] for key in ("source", "chunk_id", "text", "score")})
        text = "未找到相关资料。"
        if sources:
            text = "检索证据：\n"
            text += "\n".join(source["text"] for source in sources)
        return {"text": text, "sources": sources}

    return retrieve
