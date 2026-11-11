"""复用已有查询向量，诊断正确证据落在召回还是排序环节。"""

import argparse
import hashlib
import json
from pathlib import Path

from rag.chunking import distinct_matches
from rag.services import Qdrant, configured_services
from tools.compare_rag import check_baseline, evidence_rank, validate_evidence, write_json


def diagnose(cases, vectors, store, deduplicate=False):
    results = []
    for case, vector in zip(cases, vectors, strict=True):
        if not case["evidence"]:
            continue
        matches = store.search(vector, 20)
        ranks = {}
        for limit in (5, 10, 20):
            selected = matches[:limit]
            if deduplicate:
                selected = distinct_matches(selected, limit)
            ranks[str(limit)] = evidence_rank(case, selected)
        if ranks["5"] is not None:
            category = "top5"
        elif ranks["20"] is not None:
            category = "ranking"
        else:
            category = "recall_or_coverage"
        results.append(
            {"id": case["id"], "split": case["split"], "category": category, "ranks": ranks}
        )
    summary = {}
    for category in ("top5", "ranking", "recall_or_coverage"):
        summary[category] = sum(item["category"] == category for item in results)
    return {"summary": summary, "cases": results}


def cached_queries(cases, directory):
    vectors = []
    for offset in range(0, len(cases), 10):
        texts = [case["query"] for case in cases[offset : offset + 10]]
        digest = hashlib.sha256(json.dumps(texts, ensure_ascii=False).encode()).hexdigest()
        cached = json.loads((directory / f"queries-{offset}.json").read_text())
        if cached["sha256"] != digest or len(cached["vectors"]) != len(texts):
            raise ValueError("查询缓存与问题不符")
        vectors.extend(cached["vectors"])
    return vectors


def main():
    parser = argparse.ArgumentParser(description="只读诊断，不调用 Embedding 或问答模型")
    parser.add_argument("--corpus", required=True)
    parser.add_argument("--dataset", default="tests/rag/evidence_v2.json")
    parser.add_argument("--cache", required=True, help="已完成比较评测的输出目录")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = Path(args.output)
    if output.exists():
        raise ValueError("输出文件已存在")
    corpus = json.loads(Path(args.corpus).read_text())
    dataset = json.loads(Path(args.dataset).read_text())
    cases = dataset["cases"]
    validate_evidence(cases, corpus["manuals"])
    directory = Path(args.cache)
    manifest = json.loads((directory / "manifest.json").read_text())
    embedding, store = configured_services()
    if (
        manifest["embedding_model"] != embedding.model
        or manifest["embedding_endpoint_sha256"] != store.embedding_identity
        or manifest["dataset"] != hashlib.sha256(Path(args.dataset).read_bytes()).hexdigest()
        or manifest["corpus"] != hashlib.sha256(Path(args.corpus).read_bytes()).hexdigest()
    ):
        raise ValueError("缓存模型、服务或数据集身份不匹配")
    vectors = cached_queries(cases, directory)
    check_baseline(store, corpus["points"])
    store.ensure_collection(len(vectors[0]))
    baseline = diagnose(cases, vectors, store)
    prefix = store.base_url + "/collections/"
    if not manifest["candidate"].startswith(prefix):
        raise ValueError("候选集合与当前 Qdrant 地址不一致")
    candidate_store = Qdrant(
        store.base_url, manifest["candidate"][len(prefix) :], embedding.model, embedding.base_url
    )
    candidate_store.ensure_collection(len(vectors[0]))
    candidate = diagnose(cases, vectors, candidate_store, deduplicate=True)
    report = {"baseline": baseline, "candidate": candidate}
    write_json(output, report)
    print(json.dumps({version: data["summary"] for version, data in report.items()}, indent=2))


if __name__ == "__main__":
    main()
