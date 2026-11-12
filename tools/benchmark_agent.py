"""有调用预算的真实 Agent 端到端基准；只读知识库。"""

import argparse
import asyncio
import fcntl
from collections import Counter
import contextvars
from functools import partial
import json
import math
import os
from pathlib import Path
import socket
import re
import resource
import subprocess
import tempfile
import threading
import time

from aiohttp import web
from ragent.__main__ import create_app
from ragent.errors import ModelError
from ragent.model import DeepSeekModel
from ragent.retrieval import ManualRetriever
from rag.services import configured_services

from tools.performance_common import HEADER, percentile

REQUEST = contextvars.ContextVar("request", default="")
QUERIES = {
    "chat": "用中文简要解释 Linux 文件权限。",
    "rag": "如何列出目录中的隐藏文件？",
    "agent": "请调用 Linux 手册检索工具，说明如何修改用户密码。",
}

RETRIEVAL_QUERIES = (
    "如何列出目录中的隐藏文件？",
    "grep 的退出状态 0、1、2 分别表示什么？",
    "systemctl 怎样只列出失败的单元？",
    "passwd 命令如何查看密码状态？",
)


def measured_retriever(embedding, store, budget, observe):
    """只用于新建压测实例：替换计时方法，返回检索器和索引启动耗时。"""
    started = time.monotonic()
    if store.retrieval_strategy == "context":
        store.prepare_search()
    startup_seconds = time.monotonic() - started

    instrument_services(embedding, store, budget, observe)
    retrieve = ManualRetriever(embedding, store, observe=observe)
    return retrieve, startup_seconds


def instrument_services(embedding, store, budget, observe):
    def timed(stage, function, *args, **kwargs):
        started = time.monotonic()
        cpu_started = time.thread_time()
        try:
            return function(*args, **kwargs)
        finally:
            observe(stage, 0, time.monotonic() - started)
            observe(stage + "_cpu", 0, time.thread_time() - cpu_started)

    original_embed = embedding.embed

    def embed(texts):
        budget.take("embedding")
        return timed("embedding", original_embed, texts)

    embedding.embed = embed
    store.ensure_collection = partial(timed, "qdrant_verify", store.ensure_collection)
    store.search = partial(timed, "qdrant_query", store.search)
    store.search_multigrain = partial(timed, "qdrant_query", store.search_multigrain)
    if store.search_index is not None:
        index = store.search_index
        index.select = partial(timed, "fusion", index.select)
        index.select_multigrain = partial(timed, "fusion", index.select_multigrain)


def stage_summary(events, request_ids):
    """按请求累加阶段工作时间；并行检索的时间可能重叠。"""
    totals = {}
    for event in events:
        if event["request"] not in request_ids or event["stage"] == "tokens":
            continue
        values = totals.setdefault(event["stage"], {})
        identity = event["request"]
        values[identity] = values.get(identity, 0) + event["value"]
        if event["stage"] in ("qdrant_verify", "qdrant_query"):
            qdrant = totals.setdefault("qdrant", {})
            qdrant[identity] = qdrant.get(identity, 0) + event["value"]
        if event["stage"] == "model":
            waiting = totals.setdefault("model_wait", {})
            waiting[identity] = waiting.get(identity, 0) + event["wait_s"]
    summary = {}
    for stage, values in totals.items():
        durations = list(values.values())
        summary[stage] = {
            "requests": len(durations),
            "mean_s": sum(durations) / len(durations),
            "p50_s": percentile(durations, 0.5),
            "p95_s": percentile(durations, 0.95),
        }
    return summary


class Budget:
    def __init__(
        self,
        model=300,
        embedding=100,
        ledger=None,
        extend=False,
        phase_model=None,
        phase_embedding=None,
    ):
        self.limits = {"model": model, "embedding": embedding}
        self.counts = {"model": 0, "embedding": 0}
        self.ledger = Path(ledger) if ledger else None
        self.lock = threading.Lock()
        self.closed = False
        self.ledger_lock = None
        if extend and (self.ledger is None or not self.ledger.exists()):
            raise ValueError("扩额要求已有预算账本")
        if self.ledger is not None:
            self.ledger.parent.mkdir(parents=True, exist_ok=True)
            self.ledger_lock = self.ledger.with_suffix(".lock").open("a")
            try:
                fcntl.flock(self.ledger_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                self.ledger_lock.close()
                raise ValueError("预算账本已被其他压测进程使用") from None
        if self.ledger is not None and self.ledger.exists():
            saved = json.loads(self.ledger.read_text())
            if extend:
                old_limit = saved["limits"]["model"]
                new_limit = self.limits["model"]
                allowed = (old_limit, new_limit) in ((2000, 6000), (6000, 9000))
                if (
                    not allowed
                    or saved["limits"]["embedding"] != old_limit
                    or self.limits["embedding"] != new_limit
                ):
                    self.close()
                    raise ValueError("扩额仅支持2000到6000或6000到9000，模型与Embedding上限相同")
                suffix = ".before-extension.json" if old_limit == 2000 else ".before-9000.json"
                self.ledger.with_suffix(suffix).write_text(json.dumps(saved))
                temporary = self.ledger.with_suffix(".tmp")
                temporary.write_text(json.dumps({"limits": self.limits, "counts": saved["counts"]}))
                temporary.replace(self.ledger)
            elif saved["limits"] != self.limits:
                self.close()
                raise ValueError("已有预算账本上限不同，请沿用原上限")
            self.counts.update(saved["counts"])
        self.phase_start = dict(self.counts)
        self.phase_limits = {"model": phase_model, "embedding": phase_embedding}
        self.stopped = set()
        self.interrupted = set()
        self.aborted = threading.Event()

    def close(self):
        # 等待已开始的持久化完成，再释放跨进程锁，防止关闭后发生迟到写入。
        with self.lock:
            self.closed = True
            if self.ledger_lock is not None:
                self.ledger_lock.close()
                self.ledger_lock = None

    def remaining(self, kind):
        available = self.limits[kind] - self.counts[kind]
        phase_limit = self.phase_limits[kind]
        if phase_limit is not None:
            available = min(available, phase_limit - self.counts[kind] + self.phase_start[kind])
        return available

    def take(self, kind, amount=1):
        with self.lock:
            if self.closed:
                raise ModelError("SERVER_BUSY", "预算计量已关闭")
            if self.aborted.is_set():
                self.interrupted.add(REQUEST.get())
                raise ModelError("SERVER_BUSY", "本轮因故障阈值停止")
            available = self.remaining(kind)
            if available <= 0:
                self.stopped.add(REQUEST.get())
                raise ModelError("SERVER_BUSY", "测量调用预算已用完")
            reserved = min(amount, available)
            self.counts[kind] += reserved
            if self.ledger is not None:
                temporary = self.ledger.with_suffix(".tmp")
                temporary.write_text(json.dumps({"limits": self.limits, "counts": self.counts}))
                temporary.replace(self.ledger)
            return reserved


def memory(pid):
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    except (FileNotFoundError, ProcessLookupError):
        pass
    return 0


def save_report(output, report):
    output.seek(0)
    json.dump(report, output, indent=2)
    output.write("\n")
    output.truncate()
    output.flush()


async def measure(args):
    budget = Budget(
        args.model_budget,
        args.embedding_budget,
        args.budget_ledger,
        args.extend_budget_limits,
        args.phase_model_budget,
        args.phase_embedding_budget,
    )
    groups = []
    events = []
    serial = 0
    shared_model = None
    embedding, store = configured_services()
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "model": os.environ["RAGENT_MODEL"],
        "cpu_count": os.cpu_count(),
        "cpu_affinity": len(os.sched_getaffinity(0)),
        "model_timeout_s": float(os.environ.get("RAGENT_MODEL_TIMEOUT_SECONDS", "60")),
        "max_output_tokens": int(os.environ.get("RAGENT_MAX_OUTPUT_TOKENS", "2048")),
        "percentile_method": "nearest-rank",
        "groups": groups,
        "events": events,
        "calls": budget.counts,
    }

    def observe(stage, wait, duration):
        events.append({"request": REQUEST.get(), "stage": stage, "wait_s": wait, "value": duration})

    retrieve, startup_seconds = measured_retriever(embedding, store, budget, observe)
    report["retrieval"] = {
        "collection": store.url.rsplit("/", 1)[-1],
        "strategy": store.retrieval_strategy,
        "index_startup_s": startup_seconds,
        "workers": retrieve.workers,
    }

    completions = 0
    errors = 0
    streak = 0

    @web.middleware
    async def correlate(request, handler):
        nonlocal completions, errors, streak
        if request.method != "POST" or request.path != "/agent":
            return await handler(request)
        data = await request.json()
        token = REQUEST.set(data["request_id"])
        try:
            response = await handler(request)
            completions += 1
            failed = response.status >= 400
            if response.status == 200 and data["mode"] != "chat":
                payload = json.loads(response.body)
                failed = (
                    not payload.get("sources")
                    or any(action["status"] != "success" for action in payload.get("actions", []))
                    or payload.get("text", "").startswith(("请求超时", "达到工具"))
                )
            errors += int(failed)
            streak = streak + 1 if failed else 0
            if streak >= 3 or (completions >= 20 and errors / completions > 0.05):
                budget.aborted.set()
            return response
        finally:
            REQUEST.reset(token)

    async def group(workers, concurrency, mode, count, phase, clients=None):
        nonlocal serial
        model = shared_model
        if model is None:
            model = DeepSeekModel(concurrency, lambda: budget.take("model"), observe)
        model.peak_active = 0
        model.http_versions.clear()
        app = create_app(retrieve, model, 120)
        app.middlewares.append(correlate)
        runner = web.AppRunner(app, access_log=None, handler_cancellation=True)
        await runner.setup()
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        python_port = listener.getsockname()[1]
        site = web.SockSite(runner, listener)
        await site.start()
        rows = []
        client_slots = asyncio.Semaphore(clients or count)
        peak = {"c_rss_kib": 0, "python_rss_kib": 0}
        try:
            with tempfile.TemporaryDirectory() as temporary:
                config = Path(temporary) / "config.json"
                config.write_text(
                    json.dumps(
                        {
                            "port": 0,
                            "worker_threads": workers,
                            "agent_url": f"http://127.0.0.1:{python_port}/agent",
                            "agent_timeout_ms": 125000,
                        }
                    )
                )
                log_path = path.with_name(f"{path.stem}-{len(groups)}.log")
                with log_path.open("x") as log:
                    process = await asyncio.create_subprocess_exec(
                        str(Path(args.server).resolve()),
                        "--config",
                        str(config),
                        stdout=asyncio.subprocess.PIPE,
                        stderr=log,
                        env=dict(os.environ, RAGENT_METRICS="1"),
                    )
                    try:
                        assert process.stdout is not None
                        line = await asyncio.wait_for(process.stdout.readline(), 30)
                        if not line.startswith(b"Listening on "):
                            raise RuntimeError(f"C 服务启动失败，见 {log_path}")
                        port = int(line.decode().strip().rsplit(":", 1)[1])
                        gate = asyncio.Event()

                        completed = 0
                        failed = 0
                        consecutive = 0
                        halted = False

                        async def client(request_id, query):
                            nonlocal completed, failed, consecutive, halted
                            await gate.wait()
                            scheduled = group_started
                            if args.arrival_rate:
                                scheduled += (request_id - group_first_id) / args.arrival_rate
                                await asyncio.sleep(max(0, scheduled - time.monotonic()))
                            async with client_slots:
                                if halted:
                                    now = time.monotonic()
                                    return {
                                        "id": str(request_id),
                                        "code": "NOT_SENT",
                                        "started_s": now,
                                        "finished_s": now,
                                        "seconds": 0,
                                    }
                                row = await exchange(request_id, query)
                                completed += 1
                                unsuccessful = row["code"] != "OK"
                                failed += int(unsuccessful)
                                consecutive = consecutive + 1 if unsuccessful else 0
                                if (
                                    consecutive >= 3
                                    or (completed >= 20 and failed / completed > 0.05)
                                    or row["code"] == "BUDGET_STOP"
                                ):
                                    halted = True
                                row["dispatch_wait_s"] = row["started_s"] - scheduled
                                if args.arrival_rate:
                                    row["seconds"] = row["finished_s"] - scheduled
                                return row

                        async def exchange(request_id, query):
                            started = time.monotonic()
                            row = {"id": str(request_id), "code": "IO_ERROR", "started_s": started}
                            writer = None
                            try:
                                async with asyncio.timeout(130):
                                    reader, writer = await asyncio.open_connection(
                                        "127.0.0.1", port
                                    )
                                    body = json.dumps({"query": query}).encode()
                                    request_mode = (
                                        mode
                                        if mode != "mixed"
                                        else ("rag" if request_id % 2 == 0 else "agent")
                                    )
                                    row["mode"] = request_mode
                                    kind = {"chat": 1, "rag": 2, "agent": 4}[request_mode]
                                    writer.write(
                                        HEADER.pack(0x52414754, 2, kind, len(body), request_id)
                                        + body
                                    )
                                    await writer.drain()
                                    magic, version, kind, length, returned = HEADER.unpack(
                                        await reader.readexactly(20)
                                    )
                                    if (magic, version, returned) != (
                                        0x52414754,
                                        2,
                                        request_id,
                                    ) or length > 1048576:
                                        row["code"] = "PROTOCOL_ERROR"
                                    else:
                                        payload = json.loads(await reader.readexactly(length))
                                        row["response_bytes"] = length
                                        row["answer_chars"] = len(payload.get("text", ""))
                                        row["code"] = (
                                            payload.get("code", "OK") if kind == 101 else "OK"
                                        )
                                        if kind not in (100, 101):
                                            row["code"] = "PROTOCOL_ERROR"
                                        if kind == 100:
                                            if any(
                                                action["status"] == "failed"
                                                for action in payload.get("actions", [])
                                            ) or payload["text"].startswith(("达到工具", "请求超时")):
                                                row["code"] = "PARTIAL"
                                            if mode != "chat" and not payload.get("sources"):
                                                row["code"] = "NO_EVIDENCE"
                            except (OSError, TimeoutError, ValueError, asyncio.IncompleteReadError):
                                row["code"] = "IO_ERROR"
                            finally:
                                if writer is not None:
                                    writer.close()
                                    try:
                                        await writer.wait_closed()
                                    except OSError:
                                        row["code"] = "IO_ERROR"
                            row["finished_s"] = time.monotonic()
                            row["seconds"] = row["finished_s"] - started
                            if str(request_id) in budget.stopped:
                                row["code"] = "BUDGET_STOP"
                            elif str(request_id) in budget.interrupted:
                                row["code"] = "ABORTED"
                            return row

                        async def sample():
                            while True:
                                peak["c_rss_kib"] = max(peak["c_rss_kib"], memory(process.pid))
                                peak["python_rss_kib"] = max(
                                    peak["python_rss_kib"], memory(os.getpid())
                                )
                                await asyncio.sleep(0.1)

                        sampler = asyncio.create_task(sample())
                        started = time.monotonic()
                        python_cpu = time.process_time()
                        child_cpu = resource.getrusage(resource.RUSAGE_CHILDREN)
                        group_started = time.monotonic()
                        if args.client_io == "native":
                            requests = []
                            for query in RETRIEVAL_QUERIES:
                                if mode in ("mixed", "agent"):
                                    requests.append(
                                        {"kind": 4, "query": "请检索 Linux 手册后回答：" + query}
                                    )
                                if mode in ("mixed", "rag"):
                                    requests.append({"kind": 2, "query": query})
                            native = await asyncio.create_subprocess_exec(
                                "build/socket-probe",
                                stdin=asyncio.subprocess.PIPE,
                                stdout=asyncio.subprocess.PIPE,
                                stderr=log,
                            )
                            try:
                                settings = {
                                    "port": port,
                                    "count": count,
                                    "mode": "business",
                                    "requests": requests,
                                }
                                native_output, _ = await asyncio.wait_for(
                                    native.communicate(json.dumps(settings).encode()), 140
                                )
                                if native.returncode != 0:
                                    raise RuntimeError("C业务负载客户端失败")
                                native_report = json.loads(native_output)
                                rows = native_report["rows"]
                                for row in rows:
                                    row["id"] = str(row["id"])
                                    if row["id"] in budget.stopped:
                                        row["code"] = "BUDGET_STOP"
                                    elif row["id"] in budget.interrupted:
                                        row["code"] = "ABORTED"
                                serial += count
                            finally:
                                if native.returncode is None:
                                    native.terminate()
                                    await native.wait()
                                sampler.cancel()
                                await asyncio.gather(sampler, return_exceptions=True)
                        else:
                            tasks = []
                            group_first_id = serial + 1
                            for index in range(count):
                                serial += 1
                                query = QUERIES.get(mode, QUERIES["rag"])
                                if (
                                    args.retrieval_scan
                                    or args.configuration_scan
                                    or args.io_confirm
                                    or args.target
                                ):
                                    query_index = index // 2 if mode == "mixed" else index
                                    query = RETRIEVAL_QUERIES[query_index % len(RETRIEVAL_QUERIES)]
                                    if mode == "agent" or (mode == "mixed" and serial % 2 != 0):
                                        query = "请检索 Linux 手册后回答：" + query
                                tasks.append(asyncio.create_task(client(serial, query)))
                            started = time.monotonic()
                            python_cpu = time.process_time()
                            child_cpu = resource.getrusage(resource.RUSAGE_CHILDREN)
                            group_started = time.monotonic()
                            gate.set()
                            try:
                                rows = await asyncio.gather(*tasks)
                            finally:
                                sampler.cancel()
                                await asyncio.gather(sampler, return_exceptions=True)
                        elapsed = time.monotonic() - started
                    finally:
                        if process.returncode is None:
                            process.terminate()
                            await asyncio.wait_for(process.wait(), 130)
        finally:
            await runner.cleanup()
            if shared_model is None:
                await model.close()
        usage = resource.getrusage(resource.RUSAGE_CHILDREN)
        pairs = re.findall(r"queue_ms=([0-9.]+) http_ms=([0-9.]+)", log_path.read_text())
        resource_metrics = {
            **peak,
            "c_cpu_percent": 100
            * (usage.ru_utime + usage.ru_stime - child_cpu.ru_utime - child_cpu.ru_stime)
            / elapsed,
            "python_cpu_percent": 100 * (time.process_time() - python_cpu) / elapsed,
            "queue_p95_ms": percentile([float(queue) for queue, _ in pairs], 0.95),
            "http_p95_ms": percentile([float(http) for _, http in pairs], 0.95),
        }
        success = [row["seconds"] for row in rows if row["code"] == "OK"]
        result = {
            "workers": workers,
            "model_concurrency": concurrency,
            "model_active_peak": model.peak_active,
            "model_http_versions": dict(model.http_versions),
            "mode": mode,
            "client_io": args.client_io,
            "retrieval_workers": retrieve.workers,
            "errors": dict(Counter(row["code"] for row in rows if row["code"] != "OK")),
            "phase": phase,
            "client_concurrency": clients or count,
            "arrival_rate": args.arrival_rate,
            "dispatch_wait_p95_s": percentile(
                [row["dispatch_wait_s"] for row in rows if "dispatch_wait_s" in row], 0.95
            ),
            "rows": rows,
            "elapsed_s": elapsed,
            "success": len(success),
            "throughput": len(success) / elapsed,
            "p50_s": percentile(success, 0.5),
            "p95_s": percentile(success, 0.95),
            "p99_s": percentile(success, 0.99),
            **resource_metrics,
        }
        result["by_mode"] = {}
        for request_mode in ("rag", "agent", "chat"):
            selected = [row for row in rows if row.get("mode") == request_mode]
            if selected:
                durations = [row["seconds"] for row in selected if row["code"] == "OK"]
                result["by_mode"][request_mode] = {
                    "requests": len(selected),
                    "success": len(durations),
                    "p95_s": percentile(durations, 0.95),
                }
        identities = {row["id"] for row in rows}
        result["stages"] = stage_summary(events, identities)
        successful_ids = {row["id"] for row in rows if row["code"] == "OK"}
        result["success_stages"] = stage_summary(events, successful_ids)
        result["model_calls"] = sum(
            event["stage"] == "model" and event["request"] in identities for event in events
        )
        result["embedding_calls"] = sum(
            event["stage"] == "embedding" and event["request"] in identities for event in events
        )
        timeline = []
        for row in rows:
            if row["code"] == "NOT_SENT":
                continue
            timeline.extend(((row["started_s"], 1), (row["finished_s"], -1)))
        active = 0
        result["max_inflight"] = 0
        for _, change in sorted(timeline):
            active += change
            result["max_inflight"] = max(result["max_inflight"], active)
        failures = [
            row for row in rows if row["code"] not in ("OK", "BUDGET_STOP", "NOT_SENT", "ABORTED")
        ]
        result["failed"] = len(failures)
        result["not_sent"] = sum(row["code"] == "NOT_SENT" for row in rows)
        result["completion_rate"] = len(success) / len(rows)
        result["budget_stops"] = sum(row["code"] == "BUDGET_STOP" for row in rows)
        result["aborted"] = sum(row["code"] == "ABORTED" for row in rows)
        denominator = len(success) + len(failures)
        result["failure_rate"] = len(failures) / denominator if denominator else None
        result["failure_p95_s"] = percentile([row["seconds"] for row in failures], 0.95)
        groups.append(result)
        save_report(output, report)
        print(json.dumps({k: v for k, v in result.items() if k != "rows"}), flush=True)
        return result

    with path.open("x") as output:
        try:
            if args.retrieval_scan or args.configuration_scan or args.io_confirm or args.target:
                shared_model = DeepSeekModel(8, lambda: budget.take("model"), observe)
                if args.target:
                    shared_model.slots = asyncio.Semaphore(args.concurrency)
                    await group(
                        args.workers,
                        args.concurrency,
                        args.mode,
                        args.count,
                        "target",
                        args.clients,
                    )
                    return
                if args.io_confirm:
                    shared_model.slots = asyncio.Semaphore(32)
                    result = await group(
                        64, 32, "rag", args.confirm_count, "io-confirm", args.confirm_count
                    )
                    if result["failed"] or result["budget_stops"]:
                        raise RuntimeError("检索线程确认失败")
                    return
                for mode in ("rag", "agent"):
                    result = await group(8, 8, mode, 1, "preflight", clients=1)
                    if result["success"] != 1:
                        raise RuntimeError("真实检索预检失败")
                if args.configuration_scan:
                    for workers, concurrency in ((8, 8), (16, 16), (32, 32), (64, 32)):
                        for mode in ("rag", "agent"):
                            count = max(8, concurrency)
                            shared_model.slots = asyncio.Semaphore(concurrency)
                            result = await group(
                                workers, concurrency, mode, count, "configuration-scan", count
                            )
                            if result["budget_stops"]:
                                raise RuntimeError("测量预算耗尽，停止扫描")
                            if result["failure_rate"] >= 0.1:
                                raise RuntimeError("失败率达到停止阈值")
                    return
                for clients in (1, 4, 8, 16):
                    for mode in ("rag", "agent"):
                        count = max(8, clients)
                        result = await group(8, 8, mode, count, "retrieval-scan", clients)
                        if result["budget_stops"]:
                            raise RuntimeError("测量预算耗尽，停止扫描")
                        if result["failure_rate"] >= 0.1:
                            raise RuntimeError("失败率达到停止阈值")
                return
            if args.tools_compare:
                for mode in ("rag", "agent"):
                    result = await group(4, 4, mode, 1, "single")
                    if result["success"] != 1:
                        raise RuntimeError("检索链路预检失败")
                for phase, count in (("tools", 10), ("repeat", 8)):
                    for workers, concurrency in ((4, 4), (8, 8)):
                        modes = ("rag", "agent") if phase == "tools" else QUERIES
                        for mode in modes:
                            if (
                                budget.counts["model"] >= budget.limits["model"]
                                or budget.counts["embedding"] >= budget.limits["embedding"]
                            ):
                                return
                            result = await group(workers, concurrency, mode, count, phase)
                            if result["failed"] / count >= 0.1:
                                raise RuntimeError("失败率达到停止阈值")
                return
            if args.confirm_chat:
                for workers, concurrency in ((4, 4), (8, 8)):
                    await group(workers, concurrency, "chat", 16, "confirm")
                return
            for mode in ("chat",) if args.chat_only else QUERIES:
                result = await group(4, 4, mode, 1, "single")
                if result["success"] != 1:
                    raise RuntimeError("单请求预检失败，停止负载扫描")
            scans = []
            for workers, concurrency in ((4, 2), (4, 4), (8, 4), (8, 8), (16, 8)):
                result = await group(workers, concurrency, "chat", 16, "scan")
                scans.append(result)
                if result["success"] < 15:
                    break
            candidates = [(4, 4)]
            for row in sorted(scans, key=lambda row: row["throughput"], reverse=True):
                pair = (row["workers"], row["model_concurrency"])
                if pair not in candidates and row["success"] == 16:
                    candidates.append(pair)
                if len(candidates) == 3:
                    break
            for phase, count in (("tools", 10), ("repeat", 6)):
                for workers, concurrency in candidates:
                    modes = ("chat",) if args.chat_only else QUERIES
                    if phase == "tools" and args.chat_only:
                        continue
                    for mode in ("rag", "agent") if phase == "tools" else modes:
                        if (
                            budget.counts["model"] >= budget.limits["model"]
                            or budget.counts["embedding"] >= budget.limits["embedding"]
                        ):
                            return
                        await group(workers, concurrency, mode, count, phase)
        finally:
            try:
                if shared_model is not None:
                    await shared_model.close()
            finally:
                try:
                    await retrieve.close()
                finally:
                    budget.close()
                    save_report(output, report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tools-compare", action="store_true", help="比较 4/4 与 8/8 的真实工具链并复测"
    )
    parser.add_argument(
        "--retrieval-scan", action="store_true", help="固定8/8，扫描当前检索的客户端并发"
    )
    parser.add_argument(
        "--configuration-scan", action="store_true", help="扫描当前检索的工作线程和模型并发"
    )
    parser.add_argument("--io-confirm", action="store_true", help="小样本确认独立检索线程池")
    parser.add_argument("--confirm-count", type=int, default=16, help="检索线程确认的请求数")
    parser.add_argument("--confirm-chat", action="store_true", help="仅复测 4/4 与 8/8 CHAT 配置")
    parser.add_argument("--chat-only", action="store_true", help="只测 CHAT，不访问检索服务")
    parser.add_argument("--server", default="build/agent-server")
    parser.add_argument("--output", required=True)
    parser.add_argument("--target", action="store_true")
    parser.add_argument("--extend-budget-limits", action="store_true")
    parser.add_argument("--capacity", action="store_true", help="千在途专项，累计预算上限9000/9000")
    parser.add_argument("--agent-workers", type=int, choices=(1, 2, 4))
    parser.add_argument("--workers", type=int, default=64)
    parser.add_argument("--concurrency", type=int, default=32)
    parser.add_argument("--clients", type=int, default=32)
    parser.add_argument("--count", type=int, default=64)
    parser.add_argument("--mode", choices=("rag", "agent", "mixed"), default="mixed")
    parser.add_argument("--client-io", choices=("stream", "native"), default="stream")
    parser.add_argument("--arrival-rate", type=float, default=0)
    parser.add_argument("--budget-ledger", default="artifacts/performance/api-budget.json")
    parser.add_argument("--model-budget", type=int, default=300)
    parser.add_argument("--embedding-budget", type=int, default=100)
    parser.add_argument("--phase-model-budget", type=int)
    parser.add_argument("--phase-embedding-budget", type=int)
    args = parser.parse_args()
    if args.client_io == "native" and (
        not args.target or args.mode == "chat" or args.arrival_rate or args.clients != args.count
    ):
        parser.error("C业务负载要求--target、RAG/AGENT模式、固定突发且clients等于count")
    if args.extend_budget_limits and (not args.capacity or not args.budget_ledger):
        parser.error("扩额必须使用容量专项及持久账本")
    limit = 9000 if args.capacity else 300
    if args.capacity and not args.target:
        parser.error("千在途专项必须使用--target显式指定负载")
    if not 1 <= args.model_budget <= limit or not 1 <= args.embedding_budget <= limit:
        parser.error(f"预算必须为正数，且不超过 {limit} 次模型与 {limit} 次 Embedding")
    if any(
        value is not None and not 1 <= value <= limit
        for value in (args.phase_model_budget, args.phase_embedding_budget)
    ):
        parser.error("阶段预算必须为正数，且不超过本次调用上限")
    if not 1 <= args.confirm_count <= 64:
        parser.error("确认请求数必须为1至64")
    if not 1 <= args.workers <= 256 or not 1 <= args.concurrency <= 256:
        parser.error("工作线程和模型并发必须为1至256")
    if not 1 <= args.clients <= 2048 or not 1 <= args.count <= 10000:
        parser.error("客户端数量或请求数超出范围")
    if not math.isfinite(args.arrival_rate) or args.arrival_rate < 0:
        parser.error("到达速率必须为有限非负数")
    if args.agent_workers is not None:
        if not args.target or args.mode == "chat" or args.arrival_rate:
            parser.error("工作进程对照要求--target、RAG/AGENT/mixed及固定在途或突发负载")
        from tools.benchmark_workers import measure_workers

        asyncio.run(measure_workers(args))
    else:
        asyncio.run(measure(args))


if __name__ == "__main__":
    main()
