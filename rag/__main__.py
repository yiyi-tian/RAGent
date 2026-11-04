"""命令行入口：导入手册并执行独立检索。"""

import argparse
import json
import sys

from .corpus import DEFAULT_MANUALS, prepare_manual
from .search import search_linux_manual
from .services import ServiceError, configured_services


def import_manuals(names, embedding, store):
    chunks = []
    for name in names:
        chunks.extend(prepare_manual(name))
    if not chunks:
        raise ValueError("没有可导入的手册")
    for offset in range(0, len(chunks), 10):
        batch = chunks[offset : offset + 10]
        vectors = embedding.embed([chunk["text"] for chunk in batch])
        store.ensure_collection(len(vectors[0]), create=True)
        if offset == 0:
            store.verify_sources(chunks)
        store.upsert(batch, vectors)
    sources = {}
    for chunk in chunks:
        sources.setdefault(
            chunk["source"],
            {
                key: chunk[key]
                for key in ("source", "language", "package", "package_version", "license_file")
            },
        )
    return {"manuals": len(sources), "chunks": len(chunks), "sources": list(sources.values())}


def main():
    parser = argparse.ArgumentParser(description="Linux 手册入库与检索")
    subparsers = parser.add_subparsers(dest="command", required=True)
    importer = subparsers.add_parser("import", help="导入发行版手册")
    importer.add_argument("manuals", nargs="*", default=list(DEFAULT_MANUALS))
    search = subparsers.add_parser("search", help="语义检索手册")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=5)
    args = parser.parse_args()
    try:
        embedding, store = configured_services()
        if args.command == "import":
            result = import_manuals(args.manuals, embedding, store)
        else:
            result = search_linux_manual(args.query, embedding, store, args.limit)
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    except (ServiceError, ValueError, OSError) as error:
        print(f"RAG 失败：{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
