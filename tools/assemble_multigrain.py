"""复用已经校验的段落和条目向量，组装多粒度候选集合，不调用云端。"""

import argparse
import json
from pathlib import Path

from rag.chunking import manual_chunks
from rag.entries import entry_chunks
from rag.services import Qdrant, configured_services
from tools.compare_rag import load_vector_cache


def main():
    parser = argparse.ArgumentParser(description="复用本机成功缓存，不重新调用 Embedding")
    parser.add_argument("--corpus", default="artifacts/rag-optimization/baseline.json")
    parser.add_argument("--entries-cache", default="artifacts/rag-improvement-v4")
    args = parser.parse_args()
    corpus = json.loads(Path(args.corpus).read_text())
    embedding, configured = configured_services()
    target = Qdrant(configured.base_url, "linux_manual_v4", embedding.model, embedding.base_url)
    entry_directory = Path(args.entries_cache)
    sources = [
        (
            manual_chunks,
            Path("artifacts/rag-optimization/final"),
            "manifest.json",
            "embedding_model",
            "embedding_endpoint_sha256",
        ),
        (entry_chunks, entry_directory, "import-manifest.json", "model", "endpoint"),
    ]
    batches = []
    all_chunks = []
    for chunker, directory, manifest_name, model_key, endpoint_key in sources:
        manifest = json.loads((directory / manifest_name).read_text())
        if (
            manifest[model_key] != embedding.model
            or manifest[endpoint_key] != target.embedding_identity
        ):
            raise ValueError("向量缓存模型或服务身份不匹配")
        chunks = []
        for manual in corpus["manuals"]:
            chunks.extend(chunker(manual))
        for offset in range(0, len(chunks), 10):
            path = directory / f"chunks-{offset}.json"
            if not path.is_file():
                raise ValueError(f"缺少成功缓存：{path}")
            batch = chunks[offset : offset + 10]
            # 文件已存在且输入哈希一致才复用，不允许在此工具中补调云端。
            vectors = load_vector_cache([chunk["text"] for chunk in batch], path)
            batches.append((batch, vectors))
        all_chunks.extend(chunks)
    target.ensure_collection(len(batches[0][1][0]), create=True)
    target.verify_sources(all_chunks)
    for batch, vectors in batches:
        target.upsert(batch, vectors)
    print(json.dumps({"collection": "linux_manual_v4", "chunks": len(all_chunks)}))


if __name__ == "__main__":
    main()
