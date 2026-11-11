"""来源级检索评测。"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

from rag.search import search_linux_manual
from rag.services import ServiceError, configured_services


def load_dataset(path):
    raw = Path(path).read_bytes()
    dataset = json.loads(raw)
    if not isinstance(dataset, dict) or not isinstance(dataset.get("version"), str):
        raise ValueError("评测集必须包含版本")
    cases = dataset.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("评测集必须包含问题")
    ids = set()
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError("评测问题格式错误")
        identity, query, expected = case.get("id"), case.get("query"), case.get("expected_sources")
        if not isinstance(identity, str) or not identity.strip() or identity in ids:
            raise ValueError("评测问题 ID 必须唯一且非空")
        if not isinstance(query, str) or not query.strip() or len(query) > 4096:
            raise ValueError("评测问题必须为非空字符串且不超过 4096 字符")
        if (
            not isinstance(expected, list)
            or len(expected) > 1
            or any(not isinstance(x, str) or not x for x in expected)
        ):
            raise ValueError("每个问题应标注一个来源，或为空表示无对应资料")
        ids.add(identity)
    return dataset, hashlib.sha256(raw).hexdigest()


def evaluate(dataset, search):
    """每条请求独立执行；失败问题计入正例分母并单独列出。"""
    results = []
    positive_count = 0
    top_one = 0
    hit_five = 0
    reciprocal_sum = 0.0
    failures = 0
    negatives = 0
    for case in dataset["cases"]:
        expected = case["expected_sources"]
        positive_count += bool(expected)
        negatives += not expected
        row = {"id": case["id"], "query": case["query"], "expected_sources": expected}
        try:
            matches = search(case["query"], 5)
            row["results"] = [
                {
                    "source": m["source"],
                    "chunk_id": m["chunk_id"],
                    "score": m["score"],
                    "section": m.get("section", ""),
                    "excerpt": m["text"][:160],
                }
                for m in matches
            ]
            row["error"] = None
        except (ServiceError, ValueError, OSError) as error:
            # 不透传外部异常文本，避免将服务内容或凭据写入评测报告。
            row["results"] = []
            row["error"] = type(error).__name__
            failures += 1
        ranks = [
            index
            for index, item in enumerate(row["results"], start=1)
            if item["source"] in expected
        ]
        rank = ranks[0] if ranks else None
        row["first_source_rank"] = rank
        if expected and rank is not None:
            top_one += rank == 1
            hit_five += 1
            reciprocal_sum += 1.0 / rank
        results.append(row)
    summary = {
        "total": len(results),
        "positive": positive_count,
        "out_of_scope": negatives,
        "failed": failures,
        "source_top1": top_one,
        "source_hit_at_5": hit_five,
        "source_top1_rate": top_one / positive_count if positive_count else None,
        "source_hit_at_5_rate": hit_five / positive_count if positive_count else None,
        "source_mrr_at_5": reciprocal_sum / positive_count if positive_count else None,
    }
    return {"summary": summary, "cases": results}


def main():
    parser = argparse.ArgumentParser(description="Linux 手册来源级检索评测")
    parser.add_argument("--dataset", default="tests/rag/evaluation_v1.json")
    parser.add_argument("--output", help="将报告写入指定文件；不覆盖已有文件")
    args = parser.parse_args()
    try:
        dataset, digest = load_dataset(args.dataset)
        if args.output and Path(args.output).exists():
            raise FileExistsError("报告文件已存在")
        embedding, store = configured_services()
        report = evaluate(
            dataset, lambda query, limit: search_linux_manual(query, embedding, store, limit)
        )
        report.update(
            {
                "dataset_version": dataset["version"],
                "dataset_sha256": digest,
                "model": embedding.model,
                "collection": store.url.rsplit("/", 1)[-1],
                "created_at_utc": datetime.now(timezone.utc).isoformat(),
                "limit": 5,
            }
        )
        text = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        if args.output:
            with open(args.output, "x", encoding="utf-8") as output:
                output.write(text)
            print(json.dumps(report["summary"], ensure_ascii=False))
        else:
            print(text, end="")
        return 1 if report["summary"]["failed"] else 0
    except (ServiceError, ValueError, OSError, json.JSONDecodeError) as error:
        print(f"评测失败：{type(error).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
