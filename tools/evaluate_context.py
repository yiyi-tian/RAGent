"""用冻结回归题和独立验证题验收上下文补全，不自动切换集合。"""

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import time

from rag import context
from rag.chunking import distinct_matches, manual_chunks
from rag.entries import entry_chunks
from rag.services import Qdrant, configured_services
from tools.compare_rag import (
    Budget,
    cached_vectors,
    check_baseline,
    evidence_rank,
    read_points,
    validate_evidence,
    write_json,
)
from tools.diagnose_retrieval import cached_queries

ANSWER_CASES = (
    "grep-03",
    "ls-02",
    "ps-02",
    "systemctl-03",
    "independent-grep",
    "independent-passwd",
    "outside-01",
    "independent-outside-3",
)


def summarize(cases, results):
    summary = {}
    splits = dict.fromkeys(case["split"] for case in cases)
    for split in splits:
        ranks = []
        for case in cases:
            if case["split"] == split and case["evidence_status"] == "supported":
                ranks.append(results[case["id"]]["rank"])
        summary[split] = {
            "questions": len(ranks),
            "hit5": sum(rank is not None for rank in ranks),
            "top1": sum(rank == 1 for rank in ranks),
            "mrr": sum(1 / rank for rank in ranks if rank is not None) / len(ranks),
        }
    summary["fragmented_covered"] = results["systemctl-03"]["rank"] is not None
    return summary


async def compare_answers(cases, results, directory, budget, identities=ANSWER_CASES):
    from ragent.agent import answer
    from ragent.model import DeepSeekModel
    from ragent.retrieval import format_matches

    import ragent.agent
    import ragent.model
    import ragent.retrieval

    pipeline = {}
    for module in (ragent.agent, ragent.model, ragent.retrieval):
        filename = module.__file__
        if filename is None:
            raise ValueError(f"模块缺少源码路径，无法核验回答缓存：{module.__name__}")
        pipeline[module.__name__] = hashlib.sha256(Path(filename).read_bytes()).hexdigest()
    pipeline_path = directory / "answer-pipeline.json"
    if pipeline_path.exists():
        if json.loads(pipeline_path.read_text()) != pipeline:
            raise ValueError("回答缓存来自不同的提示或来源格式，请使用独立评测目录")
    elif any(directory.glob("answer-*.json")):
        raise ValueError("已有回答缺少流程标识，不能作为当前实现的缓存复用")
    write_json(pipeline_path, pipeline)
    model = DeepSeekModel(before_call=lambda: budget.reserve(model=True))
    for identity in identities:
        case = next(case for case in cases if case["id"] == identity)
        for variant in ("baseline", "context"):
            path = directory / f"answer-{identity}-{variant}.json"
            if path.exists():
                continue
            matches = results[variant][identity]["matches"]

            async def retrieve(query):
                return format_matches(matches)

            result = await answer(case["query"], "rag", model, timeout=120, retriever=retrieve)
            write_json(path, {"id": identity, "variant": variant, "result": result})
            print(f"回答完成：{identity} {variant}", flush=True)


def run(args):
    directory = Path(args.output)
    directory.mkdir(parents=True, exist_ok=True)
    budget = Budget(Path(args.budget) if args.budget else directory / "budget.json")
    corpus_path = Path(args.corpus)
    corpus = json.loads(corpus_path.read_text())
    regression_path = Path("tests/rag/evidence_v2.json")
    independent_path = Path("tests/rag/evidence_independent_v3.json")
    regression = json.loads(regression_path.read_text())["cases"]
    independent = json.loads(independent_path.read_text())["cases"]
    previous_independent = []
    previous_caches = []
    if args.multigrain:
        args.entries = True
    if args.entries:
        previous_independent = independent
        previous_caches.append((independent, Path("artifacts/rag-improvement-v3")))
        independent_path = Path("tests/rag/evidence_independent_v4.json")
        independent = json.loads(independent_path.read_text())["cases"]
    if args.multigrain:
        previous_caches.append((independent, Path("artifacts/rag-improvement-v4")))
        previous_independent = previous_independent + independent
        independent_path = Path("tests/rag/evidence_independent_v5.json")
        independent = json.loads(independent_path.read_text())["cases"]
    cases = regression + previous_independent + independent
    validate_evidence(cases, corpus["manuals"])
    embedding, configured = configured_services()
    baseline = Qdrant(configured.base_url, "linux_manual_v1", embedding.model, embedding.base_url)
    candidate_collection = "linux_manual_v2_sections"
    if args.entries:
        candidate_collection = "linux_manual_v3_entries"
    if args.multigrain:
        candidate_collection = "linux_manual_v4"
    candidate = Qdrant(
        baseline.base_url,
        candidate_collection,
        embedding.model,
        embedding.base_url,
        strategy="context",
    )
    identity = {
        "strategy": "entries-context1800-top5" if args.entries else "vector20-context1800-top5",
        "context_sha256": hashlib.sha256(Path(context.__file__).read_bytes()).hexdigest(),
        "corpus_sha256": hashlib.sha256(corpus_path.read_bytes()).hexdigest(),
        "regression_sha256": hashlib.sha256(regression_path.read_bytes()).hexdigest(),
        "independent_sha256": hashlib.sha256(independent_path.read_bytes()).hexdigest(),
        "embedding_model": embedding.model,
        "embedding_endpoint_sha256": baseline.embedding_identity,
        "baseline": baseline.url,
        "candidate": candidate.url,
    }
    if args.entries:
        identity["entries_sha256"] = hashlib.sha256(Path("rag/entries.py").read_bytes()).hexdigest()
        identity["previous_independent_sha256"] = hashlib.sha256(
            Path("tests/rag/evidence_independent_v3.json").read_bytes()
        ).hexdigest()
    if args.multigrain:
        identity["strategy"] = "multigrain-rrf10-fine0.5-context1800-top5"
        identity["previous_v4_sha256"] = hashlib.sha256(
            Path("tests/rag/evidence_independent_v4.json").read_bytes()
        ).hexdigest()
    manifest = directory / "manifest.json"
    if manifest.exists() and json.loads(manifest.read_text()) != identity:
        raise ValueError("评测配置已变化，请使用独立输出目录")
    write_json(manifest, identity)
    check_baseline(baseline, corpus["points"])
    expected = {}
    for manual in corpus["manuals"]:
        chunks = entry_chunks(manual) if args.entries else manual_chunks(manual)
        if args.multigrain:
            chunks.extend(manual_chunks(manual))
        for chunk in chunks:
            expected[chunk["chunk_id"]] = chunk
    actual = {point["payload"]["chunk_id"]: point["payload"] for point in read_points(candidate)}
    if expected != actual:
        raise ValueError("段落集合与冻结语料不符")
    vectors = cached_queries(regression, Path(args.cache))
    cache_manifest = json.loads((Path(args.cache) / "manifest.json").read_text())
    if (
        cache_manifest["embedding_model"] != embedding.model
        or cache_manifest["embedding_endpoint_sha256"] != baseline.embedding_identity
        or cache_manifest["corpus"] != identity["corpus_sha256"]
    ):
        raise ValueError("回归向量缓存身份不匹配")
    for previous_cases, previous_directory in previous_caches:
        previous_manifest = json.loads((previous_directory / "manifest.json").read_text())
        if (
            previous_manifest["embedding_model"] != embedding.model
            or previous_manifest["embedding_endpoint_sha256"] != baseline.embedding_identity
            or previous_manifest["corpus_sha256"] != identity["corpus_sha256"]
        ):
            raise ValueError("上一轮独立题缓存身份不匹配")
        for offset in range(0, len(previous_cases), 10):
            texts = [case["query"] for case in previous_cases[offset : offset + 10]]
            vectors.extend(
                cached_vectors(
                    embedding, texts, previous_directory / f"independent-{offset}.json", budget
                )
            )
    for offset in range(0, len(independent), 10):
        texts = [case["query"] for case in independent[offset : offset + 10]]
        vectors.extend(
            cached_vectors(embedding, texts, directory / f"independent-{offset}.json", budget)
        )
    baseline.ensure_collection(len(vectors[0]))
    candidate.ensure_collection(len(vectors[0]))
    started = time.perf_counter()
    candidate.prepare_search()
    index = candidate.search_index
    if index is None:
        raise ValueError("上下文评测需要已加载的检索索引")
    index_seconds = time.perf_counter() - started
    unit_variant = "entries" if args.entries and not args.multigrain else "paragraph"
    results = {"baseline": {}, unit_variant: {}, "context": {}}
    if args.multigrain:
        results["previous_context"] = {}
    local_seconds = []
    for case, vector in zip(cases, vectors, strict=True):
        dense = candidate.search(vector, 20, "paragraph-v2" if args.multigrain else None)
        fine = candidate.search(vector, 20, "entry-v3") if args.multigrain else []
        started = time.perf_counter()
        if args.multigrain:
            expanded = index.select_multigrain(dense, fine)
        else:
            expanded = index.select(dense)
        local_seconds.append(time.perf_counter() - started)
        matches_by_variant = {
            "baseline": baseline.search(vector, 5),
            unit_variant: distinct_matches(dense[:10], 5),
            "context": expanded,
        }
        if args.multigrain:
            matches_by_variant["previous_context"] = index.select(dense)
        for variant, matches in matches_by_variant.items():
            results[variant][case["id"]] = {
                "rank": evidence_rank(case, matches),
                "matches": matches,
            }
    summaries = {variant: summarize(cases, data) for variant, data in results.items()}
    before, after = summaries["baseline"], summaries["context"]
    splits = [split for split in before if split != "fragmented_covered"]
    gates = {
        "development": after["development"]["hit5"] >= 20,
        "validation": after["validation"]["hit5"] >= 7,
        "holdout": after["holdout"]["hit5"] >= before["holdout"]["hit5"],
        "independent": after["independent"]["hit5"] >= before["independent"]["hit5"],
        "top1": sum(after[s]["top1"] for s in splits) >= sum(before[s]["top1"] for s in splits),
        "mrr": sum(after[s]["mrr"] * after[s]["questions"] for s in splits)
        >= sum(before[s]["mrr"] * before[s]["questions"] for s in splits),
        "fragmented": not before["fragmented_covered"] or after["fragmented_covered"],
    }
    if args.entries:
        previous = json.loads(Path("artifacts/rag-improvement-v3/retrieval.json").read_text())
        previous = previous["summary"]["context"]
        gates["holdout"] = after["holdout"]["hit5"] >= previous["holdout"]["hit5"]
        gates["independent"] = after["independent"]["hit5"] >= previous["independent"]["hit5"]
        gates["independent_v4"] = (
            after["independent_v4"]["hit5"] >= before["independent_v4"]["hit5"]
        )
        gates["known_misses"] = any(
            results["baseline"][name]["rank"] is None
            and results["context"][name]["rank"] is not None
            for name in ("grep-03", "ls-02", "ps-02")
        )
    if args.multigrain:
        gates["independent_v4"] = after["independent_v4"]["hit5"] >= 8
        gates["independent_v5"] = after["independent_v5"]["hit5"] >= max(
            before["independent_v5"]["hit5"],
            summaries["previous_context"]["independent_v5"]["hit5"],
        )
    report = {
        "summary": summaries,
        "gates": gates,
        "results": results,
        "timing": {
            "index_seconds": index_seconds,
            "context_mean_ms": 1000 * sum(local_seconds) / len(local_seconds),
        },
    }
    write_json(directory / "retrieval.json", report)
    print(
        json.dumps({"summary": summaries, "gates": gates, "timing": report["timing"]}, indent=2),
        flush=True,
    )
    if args.answers:
        identities = ANSWER_CASES
        if args.entries:
            identities = (
                "grep-03",
                "holdout-grep",
                "holdout-systemctl",
                "systemctl-03",
                "blind-v4-grep",
                "blind-v4-systemctl",
                "outside-01",
                "blind-v4-outside-1",
            )
        if args.multigrain:
            identities = (
                "grep-03",
                "holdout-grep",
                "holdout-systemctl",
                "systemctl-03",
                "blind-v5-ls",
                "blind-v5-systemctl",
                "outside-01",
                "blind-v5-outside-2",
            )
        asyncio.run(compare_answers(cases, results, directory, budget, identities))
    print(json.dumps(budget.counts))


def main():
    parser = argparse.ArgumentParser(description="上下文补全的独立验收")
    parser.add_argument("--corpus", default="artifacts/rag-optimization/baseline.json")
    parser.add_argument("--cache", default="artifacts/rag-optimization/final")
    parser.add_argument("--output", required=True)
    parser.add_argument("--entries", action="store_true", help="验收独立条目集合及 v4 独立题")
    parser.add_argument("--multigrain", action="store_true", help="验收多粒度方案及 v5 独立题")
    parser.add_argument("--budget", help="同轮候选共用的累计预算文件")
    parser.add_argument("--answers", action="store_true")
    run(parser.parse_args())


if __name__ == "__main__":
    main()
