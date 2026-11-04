"""对比证据块检索策略；所有 Qdrant 请求只读。"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sys

from rag.services import ServiceError, configured_services

COMMANDS = ("passwd", "man", "ls", "grep", "chmod", "ps", "gpasswd", "systemctl")
COMMAND_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_-])(" + "|".join(COMMANDS) + r")(?![A-Za-z0-9_-])", re.IGNORECASE
)
DEFAULT_SNAPSHOT = "artifacts/knowledge-base/linux-manual-v1-osejvh4r/linux_manual_v1.snapshot"


def command_source(query):
    """仅识别完整命令名；多个不同命令或未识别时回退。"""
    names = {match.group(1).lower() for match in COMMAND_PATTERN.finditer(query)}
    return next(iter(names)) + "(1)" if len(names) == 1 else None


def load_cases(path, snapshot_path):
    raw = Path(path).read_bytes()
    dataset = json.loads(raw)
    if not isinstance(dataset, dict) or not isinstance(dataset.get("version"), str):
        raise ValueError("证据集缺少版本")
    snapshot_hash = hashlib.sha256(Path(snapshot_path).read_bytes()).hexdigest()
    if snapshot_hash != dataset.get("snapshot_sha256"):
        raise ValueError("证据标注与快照不匹配")
    cases = dataset.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("证据集缺少问题")
    seen = set()
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError("评测问题格式错误")
        identity, query = case.get("id"), case.get("query")
        sources, evidence = case.get("expected_sources"), case.get("evidence_ids")
        if not isinstance(identity, str) or not identity or identity in seen:
            raise ValueError("问题 ID 非法或重复")
        if not isinstance(query, str) or not query.strip() or len(query) > 4096:
            raise ValueError("问题非法")
        if case.get("split") not in ("development", "validation"):
            raise ValueError("评测集分组非法")
        if (
            not isinstance(sources, list)
            or len(sources) > 1
            or any(not isinstance(s, str) or not s for s in sources)
        ):
            raise ValueError("来源标注非法")
        if not isinstance(evidence, list) or any(not isinstance(i, str) or not i for i in evidence):
            raise ValueError("证据标注非法")
        status = case.get("evidence_status")
        if status == "supported":
            if not sources or not evidence:
                raise ValueError("可回答问题缺少证据或来源")
        elif status == "insufficient":
            if sources or evidence:
                raise ValueError("无资料问题不应标注来源或证据")
        elif status == "fragmented":
            if not sources or evidence:
                raise ValueError("跨分块证据标注非法")
        else:
            raise ValueError("证据状态非法")
        seen.add(identity)
    return dataset, hashlib.sha256(raw).hexdigest()


def check_reference(dataset, store):
    """只读检查集合容量和每个标注的来源，避免误用其他知识库。"""
    count = store._call("POST", "/points/count", {"exact": True})
    if count is None or count.get("result", {}).get("count") != 306:
        raise ServiceError("集合点数与首版不匹配")
    labels = {}
    for case in dataset["cases"]:
        for identity in case["evidence_ids"]:
            source = case["expected_sources"][0]
            if identity in labels and labels[identity] != source:
                raise ValueError("同一证据 ID 标注了不同来源")
            labels[identity] = source
    response = store._call(
        "POST", "/points", {"ids": list(labels), "with_payload": True, "with_vector": False}
    )
    if response is None or not isinstance(response.get("result"), list):
        raise ServiceError("证据块检查响应非法")
    found = {str(point["id"]): point["payload"]["source"] for point in response["result"]}
    if found != labels:
        raise ServiceError("证据标注与集合来源不匹配")


def filtered_search(store, vector, source, limit=5):
    request = {
        "query": vector,
        "limit": limit,
        "with_payload": True,
        "filter": {"must": [{"key": "source", "match": {"value": source}}]},
    }
    response = store._call("POST", "/points/query", request)
    try:
        if response is None:
            raise ValueError
        points = response["result"]["points"]
        if not isinstance(points, list):
            raise ValueError
        matches = []
        for point in points:
            payload, score = point["payload"], point["score"]
            if not isinstance(payload, dict) or any(
                not isinstance(payload.get(k), str) for k in ("source", "chunk_id", "text")
            ):
                raise ValueError
            if type(score) not in (int, float) or not math.isfinite(score):
                raise ValueError
            matches.append({**payload, "score": score})
        return matches
    except (KeyError, TypeError, ValueError) as error:
        raise ServiceError("过滤检索响应格式错误") from error


def compact_results(matches):
    return [
        {
            "source": p["source"],
            "chunk_id": p["chunk_id"],
            "score": p["score"],
            "section": p.get("section", ""),
            "excerpt": p["text"][:160],
        }
        for p in matches
    ]


def rank_for(rows, key, targets):
    return next((index for index, row in enumerate(rows, 1) if row[key] in targets), None)


def summarize(rows, strategy):
    supported = [r for r in rows if r["evidence_status"] == "supported"]
    insufficient = [r for r in rows if r["evidence_status"] == "insufficient"]
    sources = [r for r in rows if r["expected_sources"]]

    def hits(items, key, rank):
        return sum(r[strategy][key] is not None and r[strategy][key] <= rank for r in items)

    return {
        "total": len(rows),
        "supported": len(supported),
        "insufficient": len(insufficient),
        "fragmented": sum(r["evidence_status"] == "fragmented" for r in rows),
        "errors": sum(r[strategy]["error"] is not None for r in rows),
        "evidence_top1": hits(supported, "evidence_rank", 1),
        "evidence_hit_at_5": hits(supported, "evidence_rank", 5),
        "source_top1": hits(sources, "source_rank", 1),
        "source_hit_at_5": hits(sources, "source_rank", 5),
        "insufficient_with_results": sum(bool(r[strategy]["results"]) for r in insufficient),
        "insufficient_errors": sum(r[strategy]["error"] is not None for r in insufficient),
        "filter_applied": (
            sum(r["filter_source"] is not None and r["baseline"]["error"] is None for r in rows)
            if strategy == "filtered"
            else 0
        ),
    }


def evaluate(dataset, embedding, store):
    rows = []
    for case in dataset["cases"]:
        row = {
            key: case[key]
            for key in (
                "id",
                "query",
                "split",
                "expected_sources",
                "evidence_ids",
                "evidence_status",
            )
        }
        row["filter_source"] = command_source(case["query"])
        vector = None
        try:
            vector = embedding.embed([case["query"]])[0]
            store.ensure_collection(len(vector))
            baseline = compact_results(store.search(vector, 5))
            row["baseline"] = {"results": baseline, "error": None}
        except (ServiceError, ValueError, OSError) as error:
            row["baseline"] = {"results": [], "error": type(error).__name__}
        try:
            if vector is None or row["baseline"]["error"] is not None:
                raise ServiceError("基线查询失败")
            if row["filter_source"] is None:
                filtered = row["baseline"]["results"]
            else:
                filtered = compact_results(filtered_search(store, vector, row["filter_source"]))
            row["filtered"] = {"results": filtered, "error": None}
        except (ServiceError, ValueError, OSError) as error:
            row["filtered"] = {"results": [], "error": type(error).__name__}
        for name in ("baseline", "filtered"):
            result = row[name]
            result["source_rank"] = rank_for(result["results"], "source", case["expected_sources"])
            result["evidence_rank"] = rank_for(result["results"], "chunk_id", case["evidence_ids"])
        rows.append(row)
    summaries = {}
    for split in ("development", "validation"):
        subset = [r for r in rows if r["split"] == split]
        summaries[split] = {name: summarize(subset, name) for name in ("baseline", "filtered")}
    return {"summary": summaries, "cases": rows}


def main():
    parser = argparse.ArgumentParser(description="手册证据检索策略对比")
    parser.add_argument("--dataset", default="tests/rag/evidence_v1.json")
    parser.add_argument("--snapshot", default=DEFAULT_SNAPSHOT)
    parser.add_argument("--output", help="报告路径；不覆盖已有文件")
    args = parser.parse_args()
    try:
        dataset, digest = load_cases(args.dataset, args.snapshot)
        if args.output and Path(args.output).exists():
            raise FileExistsError("报告文件已存在")
        embedding, store = configured_services()
        store.ensure_collection(1024)
        check_reference(dataset, store)
        report = evaluate(dataset, embedding, store)
        report.update(
            {
                "dataset_version": dataset["version"],
                "dataset_sha256": digest,
                "snapshot_sha256": dataset["snapshot_sha256"],
                "model": embedding.model,
                "collection": store.url.rsplit("/", 1)[-1],
                "created_at_utc": datetime.now(timezone.utc).isoformat(),
                "limit": 5,
            }
        )
        output = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        if args.output:
            with open(args.output, "x", encoding="utf-8") as file:
                file.write(output)
            print(json.dumps(report["summary"], ensure_ascii=False))
        else:
            print(output, end="")
        for summary in report["summary"].values():
            for strategy in ("baseline", "filtered"):
                if summary[strategy]["errors"]:
                    return 1
        return 0
    except (ServiceError, ValueError, OSError, KeyError, TypeError) as error:
        print(f"评测失败：{type(error).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
