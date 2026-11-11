"""在冻结原文上构建候选集合，并用同一查询向量比较证据覆盖。"""

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import re

from rag import chunking
from rag.chunking import distinct_matches, manual_chunks
from rag.corpus import MAN_ROOT, package_info, render_manual, split_sections
from rag.services import Qdrant, configured_services

ANSWER_CASES = (
    "grep-03",
    "ls-02",
    "ps-02",
    "systemctl-03",
    "new-passwd",
    "new-systemctl",
    "outside-01",
    "new-outside-3",
)


def write_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


class Budget:
    """调用前落盘计数，失败请求也占预算；同一目录不得并发运行。"""

    def __init__(self, path):
        self.path = Path(path)
        self.counts = {"embedding_requests": 0, "embedding_texts": 0, "model_calls": 0}
        if self.path.exists():
            self.counts = json.loads(self.path.read_text())

    def reserve(self, texts=0, model=False):
        updated = self.counts.copy()
        updated["embedding_requests"] += int(texts > 0)
        updated["embedding_texts"] += texts
        updated["model_calls"] += int(model)
        limits = {"embedding_requests": 150, "embedding_texts": 1200, "model_calls": 20}
        for key, limit in limits.items():
            if updated[key] > limit:
                raise RuntimeError(f"本轮 {key} 预算已用尽")
        write_json(self.path, updated)
        self.counts = updated


def load_vector_cache(texts, path):
    digest = hashlib.sha256(json.dumps(texts, ensure_ascii=False).encode()).hexdigest()
    cached = json.loads(path.read_text())
    if cached["sha256"] != digest or len(cached["vectors"]) != len(texts):
        raise ValueError("向量缓存与输入不符，请使用独立输出目录")
    return cached["vectors"]


def cached_vectors(embedding, texts, path, budget):
    if path.exists():
        return load_vector_cache(texts, path)
    budget.reserve(texts=len(texts))
    vectors = embedding.embed(texts)
    digest = hashlib.sha256(json.dumps(texts, ensure_ascii=False).encode()).hexdigest()
    write_json(path, {"sha256": digest, "vectors": vectors})
    return vectors


def validate_evidence(cases, manuals):
    by_source = {manual["source"]: manual["text"] for manual in manuals}
    seen = set()
    for case in cases:
        if case["id"] in seen or not case["query"].strip():
            raise ValueError("问题 ID 重复或问题为空")
        seen.add(case["id"])
        if case["evidence_status"] != "insufficient" and not case["evidence"]:
            raise ValueError("正例缺少原文证据")
        for evidence in case["evidence"]:
            text = by_source[evidence["source"]]
            if hashlib.sha256(text.encode()).hexdigest() != evidence["document_sha256"]:
                raise ValueError("标注原文哈希不匹配")
            if text[evidence["start"] : evidence["end"]] != evidence["quote"]:
                raise ValueError("标注位置与原文不匹配")


def evidence_rank(case, matches):
    covered = set()
    for rank, match in enumerate(matches, 1):
        body = " ".join(match.get("context_text", match.get("body", match["text"])).split())
        for index, evidence in enumerate(case["evidence"]):
            if evidence["source"] != match["source"]:
                continue
            if " ".join(evidence["quote"].split()) in body:
                covered.add(index)
        if case["evidence"] and len(covered) == len(case["evidence"]):
            return rank
    return None


def metrics(cases, results):
    summary = {}
    for split in ("development", "validation", "holdout"):
        supported = []
        for case in cases:
            if case["split"] == split and case["evidence_status"] == "supported":
                supported.append(case)
        ranks = [results[case["id"]]["rank"] for case in supported]
        summary[split] = {
            "questions": len(ranks),
            "hit5": sum(rank is not None for rank in ranks),
            "top1": sum(rank == 1 for rank in ranks),
            "mrr": sum(1 / rank for rank in ranks if rank is not None) / len(ranks),
        }
    fragmented = next(case for case in cases if case["id"] == "systemctl-03")
    summary["fragmented_covered"] = results[fragmented["id"]]["rank"] is not None
    return summary


def read_points(store):
    points = []
    offset = None
    while True:
        request = {"limit": 128, "with_payload": True, "with_vector": False}
        if offset is not None:
            request["offset"] = offset
        response = store._call("POST", "/points/scroll", request)["result"]
        points.extend(response["points"])
        offset = response["next_page_offset"]
        if offset is None:
            return points


def freeze_corpus(store, path):
    if path.exists():
        raise ValueError("冻结文件已存在，拒绝覆盖")
    points = read_points(store)
    if not points:
        raise ValueError("基线集合为空")
    sources = {}
    for point in points:
        payload = point["payload"]
        sources.setdefault(payload["source"], []).append(payload)
    manuals = []
    for source, payloads in sources.items():
        metadata = payloads[0]
        if not re.fullmatch(r"[A-Za-z0-9_.+-]+\(1\)", source):
            raise ValueError("基线手册名称非法")
        if metadata["language"] not in ("zh_CN", "en"):
            raise ValueError("基线手册语言不受支持")
        name = source.removesuffix("(1)")
        # 按基线语言读取，避免本机后来安装的中文手册改变对比语料。
        directory = MAN_ROOT / ("zh_CN" if metadata["language"] == "zh_CN" else "") / "man1"
        path_candidates = [directory / (name + suffix) for suffix in (".1.gz", ".1")]
        manual_path = next((item for item in path_candidates if item.is_file()), None)
        if manual_path is None:
            raise ValueError(f"找不到基线手册：{source}")
        package, version, license_file = package_info(manual_path)
        if (package, version, license_file) != (
            metadata["package"],
            metadata["package_version"],
            metadata["license_file"],
        ):
            raise ValueError(f"基线发行包已变化：{source}")
        text = render_manual(manual_path)
        actual = sorted(
            hashlib.sha256(body.encode()).hexdigest() for _, body in split_sections(text)
        )
        expected = sorted(payload["content_sha256"] for payload in payloads)
        if actual != expected:
            raise ValueError(f"原文与 v1 内容不符：{source}")
        manual = {
            key: metadata[key]
            for key in ("source", "language", "package", "package_version", "license_file")
        }
        manual["text"] = text
        manuals.append(manual)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, {"manuals": manuals, "points": points})


def check_baseline(store, points):
    expected = {str(point["id"]): point["payload"] for point in points}
    actual = {str(point["id"]): point["payload"] for point in read_points(store)}
    if expected != actual:
        raise ValueError("在线基线与冻结载荷不符")


async def compare_answers(cases, results, directory, budget):
    from langchain_core.messages import HumanMessage
    from ragent.model import DeepSeekModel

    model = DeepSeekModel(before_call=lambda: budget.reserve(model=True))
    for identity in ANSWER_CASES:
        case = next(case for case in cases if case["id"] == identity)
        for version in ("baseline", "candidate"):
            path = directory / f"answer-{identity}-{version}.json"
            if path.exists():
                continue
            matches = results[version][identity]["matches"]
            prompt = (
                "仅根据下列检索证据回答问题，不要使用证据外的知识补充命令。"
                "如果无法回答，明确说知识库证据不足。引用来源。\n"
                f"问题：{case['query']}\n证据：" + json.dumps(matches, ensure_ascii=False)
            )
            answer = await model.ainvoke([HumanMessage(content=prompt)], [])
            write_json(path, {"id": identity, "version": version, "answer": answer.content})
            print(f"回答完成：{identity} {version}", flush=True)


def run(args):
    directory = Path(args.output)
    directory.mkdir(parents=True, exist_ok=True)
    budget = Budget(Path(args.budget) if args.budget else directory / "budget.json")
    baseline = json.loads(Path(args.corpus).read_text())
    dataset = json.loads(Path(args.dataset).read_text())
    cases = dataset["cases"]
    validate_evidence(cases, baseline["manuals"])
    embedding, store = configured_services()
    candidate = Qdrant(
        store.base_url, args.candidate, embedding.model, embedding.base_url, embedding.timeout
    )
    if candidate.url == store.url:
        raise ValueError("候选集合不能与基线相同")
    identity = {
        "chunker": hashlib.sha256(Path(chunking.__file__).read_bytes()).hexdigest(),
        "corpus": hashlib.sha256(Path(args.corpus).read_bytes()).hexdigest(),
        "dataset": hashlib.sha256(Path(args.dataset).read_bytes()).hexdigest(),
        "baseline": store.url,
        "candidate": candidate.url,
        "embedding_model": embedding.model,
        "embedding_endpoint_sha256": store.embedding_identity,
    }
    manifest = directory / "manifest.json"
    if manifest.exists() and json.loads(manifest.read_text()) != identity:
        raise ValueError("输出目录已有不同的评测配置")
    write_json(manifest, identity)
    check_baseline(store, baseline["points"])
    info = store._call("GET")
    dimension = info["result"]["config"]["params"]["vectors"]["size"]
    store.ensure_collection(dimension)
    chunks = []
    for manual in baseline["manuals"]:
        chunks.extend(manual_chunks(manual))
    if candidate._call("GET", allow_missing=True) is not None:
        candidate.verify_sources(chunks)
    for offset in range(0, len(chunks), 10):
        batch = chunks[offset : offset + 10]
        vectors = cached_vectors(
            embedding,
            [chunk["text"] for chunk in batch],
            directory / f"chunks-{offset}.json",
            budget,
        )
        candidate.ensure_collection(len(vectors[0]), create=True)
        candidate.upsert(batch, vectors)
        print(f"候选入库：{min(offset + 10, len(chunks))}/{len(chunks)}", flush=True)
    results = {"baseline": {}, "candidate": {}}
    for offset in range(0, len(cases), 10):
        batch = cases[offset : offset + 10]
        vectors = cached_vectors(
            embedding,
            [case["query"] for case in batch],
            directory / f"queries-{offset}.json",
            budget,
        )
        store.ensure_collection(len(vectors[0]))
        for case, vector in zip(batch, vectors, strict=True):
            matches_by_version = {
                "baseline": store.search(vector, 5),
                "candidate": distinct_matches(candidate.search(vector, 10), 5),
            }
            for version, matches in matches_by_version.items():
                results[version][case["id"]] = {
                    "rank": evidence_rank(case, matches),
                    "matches": matches,
                }
    summaries = {version: metrics(cases, items) for version, items in results.items()}
    before, after = summaries["baseline"], summaries["candidate"]
    gates = {
        "development": after["development"]["hit5"] >= 20,
        "validation": after["validation"]["hit5"] >= 7,
        "holdout": after["holdout"]["hit5"] >= before["holdout"]["hit5"],
        "top1": sum(after[s]["top1"] for s in ("development", "validation", "holdout"))
        >= sum(before[s]["top1"] for s in ("development", "validation", "holdout")),
        "mrr": sum(
            after[s]["mrr"] * after[s]["questions"]
            for s in ("development", "validation", "holdout")
        )
        >= sum(
            before[s]["mrr"] * before[s]["questions"]
            for s in ("development", "validation", "holdout")
        ),
        "fragmented": not before["fragmented_covered"] or after["fragmented_covered"],
        "known_misses": any(
            results["baseline"][identity]["rank"] is None
            and results["candidate"][identity]["rank"] is not None
            for identity in ("grep-03", "ls-02", "ps-02")
        ),
    }
    report = {"summary": summaries, "gates": gates, "results": results}
    write_json(directory / "retrieval.json", report)
    print(json.dumps({"summary": summaries, "gates": gates}, ensure_ascii=False, indent=2))
    if args.answers:
        asyncio.run(compare_answers(cases, results, directory, budget))
    print(json.dumps(budget.counts))


def main():
    parser = argparse.ArgumentParser(description="冻结语料的 RAG 候选评测，不自动切换生产集合")
    parser.add_argument("--corpus", required=True, help="冻结的 manuals 与基线 points JSON")
    parser.add_argument("--dataset", default="tests/rag/evidence_v2.json")
    parser.add_argument("--candidate", default="linux_manual_v2_sections")
    parser.add_argument("--freeze", action="store_true", help="只读核对 v1 并写入 --corpus")
    parser.add_argument("--output", help="独立输出目录，保留预算及成功调用缓存")
    parser.add_argument("--budget", help="同一轮多个候选共用的累计预算文件")
    parser.add_argument("--answers", action="store_true", help="额外进行固定八题的双版本回答比较")
    args = parser.parse_args()
    if args.freeze:
        _, store = configured_services()
        freeze_corpus(store, Path(args.corpus))
        return
    if not args.output:
        parser.error("评测需要 --output")
    run(args)


if __name__ == "__main__":
    main()
