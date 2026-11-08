"""Linux 手册检索接口，供业务入口和开发工具共同使用。"""

from .chunking import VERSION, distinct_matches

MAX_QUERY_LENGTH = 4096


def search_linux_manual(query, embedding, store, limit=5):
    """检索块和来源；异常表示服务不可用或响应无效。"""
    if not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY_LENGTH:
        raise ValueError("查询必须为 1 至 4096 字符的非空字符串")
    if not 1 <= limit <= 50:
        raise ValueError("检索条数必须在 1 至 50 之间")
    vector = embedding.embed([query])[0]
    store.ensure_collection(len(vector))
    if getattr(store, "retrieval_strategy", "vector") == "context":
        store.prepare_search()
        if store.search_index.multigrain:
            coarse, fine = store.search_multigrain(vector, max(20, limit))
            return store.search_index.select_multigrain(coarse, fine, limit)
        matches = store.search(vector, max(20, limit))
        return store.search_index.select(matches, limit)
    matches = store.search(vector, max(10, limit))
    if matches and matches[0].get("chunking_version") in (VERSION, "entry-v3"):
        return distinct_matches(matches, limit)
    return matches[:limit]
