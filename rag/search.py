"""Linux 手册检索接口，供业务入口和开发工具共同使用。"""


def search_linux_manual(query, embedding, store, limit=5):
    """检索块和来源；异常表示服务不可用或响应无效。"""
    if not isinstance(query, str) or not query.strip() or len(query) > 4096:
        raise ValueError("查询必须为 1 至 4096 字符的非空字符串")
    if not 1 <= limit <= 50:
        raise ValueError("检索条数必须在 1 至 50 之间")
    vector = embedding.embed([query])[0]
    store.ensure_collection(len(vector))
    return store.search(vector, limit)
