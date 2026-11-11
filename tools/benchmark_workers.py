"""固定工作进程的端到端对照，预算由父进程唯一持有。"""

import asyncio
from collections import Counter, deque
from dataclasses import dataclass
from ctypes import c_byte
import json
import multiprocessing
from multiprocessing.connection import Connection
from multiprocessing.queues import Queue
import os
import resource
import re
from pathlib import Path
import threading
import time
import sys
from typing import cast

from aiohttp import web
from ragent.errors import ModelError
from ragent.workers import WorkerGroup
from tools.benchmark_agent import Budget, REQUEST, RETRIEVAL_QUERIES, instrument_services
from tools.benchmark_agent import memory, stage_summary
from tools.performance_common import HEADER, percentile


def process_cpu(pid):
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")


class RemoteBudget:
    def __init__(self, connection, aborted, messages, credits=None):
        self.connection = connection
        self.aborted = aborted
        self.messages = messages
        self.lock = threading.RLock()
        self.closed = False
        self.credits = dict(credits) if credits is not None else {"model": 0, "embedding": 0}
        self.issued = {"model": 0, "embedding": 0}
        self.fast_calls = 0

    def take(self, kind):
        with self.lock:
            if self.closed:
                raise ModelError("SERVER_BUSY", "预算通道已关闭")
            if self.aborted.value:
                self.messages.put(("interrupted", REQUEST.get()))
                raise ModelError("SERVER_BUSY", "本轮因故障阈值停止")
            if self.credits[kind] == 0:
                # 调用前持久化预留；未使用额度也保留在账本中，不回退累计计数。
                self.connection.send((kind, REQUEST.get(), 8))
                result = self.connection.recv()
                if isinstance(result, tuple):
                    raise ModelError(*result)
                self.credits[kind] = result
            self.credits[kind] -= 1
            self.issued[kind] += 1

    def close(self):
        with self.lock:
            self.closed = True
            self.connection.close()

    async def before_model(self):
        # 已预留额度可直接消费；可能等待锁或IPC时才进入线程，避免逐次线程切换。
        if self.lock.acquire(blocking=False):
            try:
                if self.credits["model"] > 0:
                    self.take("model")
                    self.fast_calls += 1
                    return
            finally:
                self.lock.release()
        await asyncio.to_thread(self.take, "model")


@dataclass
class WorkerHooks:
    connection: Connection
    messages: Queue
    aborted: c_byte
    credits: dict[str, int]

    def __call__(self, app, retriever, model):
        budget = RemoteBudget(self.connection, self.aborted, self.messages, self.credits)
        cpu_started = time.process_time()
        lags = deque(maxlen=10000)
        begun = False
        completed = 0
        heartbeat = None

        async def monitor():
            expected = time.monotonic() + 0.05
            while True:
                await asyncio.sleep(max(0, expected - time.monotonic()))
                now = time.monotonic()
                if begun:
                    lags.append(max(0, now - expected))
                expected = now + 0.05

        async def start_monitor(app):
            nonlocal heartbeat
            heartbeat = asyncio.create_task(monitor())

        async def stop_monitor(app):
            if heartbeat is not None:
                heartbeat.cancel()
                try:
                    await heartbeat
                except asyncio.CancelledError:
                    pass

        app.on_startup.append(start_monitor)
        app.on_cleanup.append(stop_monitor)

        def observe(stage, wait, value):
            self.messages.put(
                (
                    "stage",
                    {
                        "request": REQUEST.get(),
                        "stage": stage,
                        "wait_s": wait,
                        "value": value,
                        "finished_s": time.monotonic(),
                        "pid": os.getpid(),
                    },
                )
            )

        instrument_services(retriever.embedding, retriever.store, budget, observe)
        retriever.observe = observe
        model.before_call = budget.before_model
        model.observe = observe

        @web.middleware
        async def correlate(request, handler):
            nonlocal begun, completed
            if request.path != "/agent" or request.method != "POST":
                return await handler(request)
            data = await request.json()
            begun = True
            token = REQUEST.set(data["request_id"])
            try:
                response = await handler(request)
                failed = response.status >= 400
                if not failed and data["mode"] != "chat":
                    body = json.loads(response.body)
                    failed = (
                        not body.get("sources")
                        or any(a["status"] != "success" for a in body.get("actions", []))
                        or body.get("text", "").startswith(("请求超时", "达到工具"))
                    )
                self.messages.put(("completion", failed))
                completed += 1
                return response
            finally:
                REQUEST.reset(token)

        app.middlewares.append(correlate)

        def cleanup():
            self.messages.put(
                (
                    "worker",
                    {
                        "pid": os.getpid(),
                        "cpu_s": time.process_time() - cpu_started,
                        "model_peak": model.peak_active,
                        "http_versions": model.http_versions,
                        "requests": completed,
                        "issued_calls": budget.issued,
                        "budget_fast_calls": budget.fast_calls,
                        "event_loop_lag_p95_s": percentile(list(lags), 0.95),
                        "event_loop_lag_max_s": max(lags, default=0),
                    },
                )
            )
            budget.close()
            self.messages.close()
            self.messages.join_thread()

        return cleanup


class Coordinator:
    def __init__(self, budget, count, expected_model=0, expected_embedding=0):
        self.budget = budget
        # 负载开始前只预留预算，不发出任何模型/Embedding请求，也不预热连接。
        initial = {}
        for kind, expected in (("model", expected_model), ("embedding", expected_embedding)):
            initial[kind] = 0
            if expected > 0 and budget.remaining(kind) > 0:
                initial[kind] = budget.take(kind, expected)
        self.aborted = multiprocessing.get_context("spawn").RawValue("b", False)
        self.messages = multiprocessing.get_context("spawn").Queue(maxsize=16384)
        self.hooks = []
        self.connections = []
        self.threads = []
        self.events = []
        self.workers = []
        self.errors = []
        for index in range(count):
            parent, child = multiprocessing.get_context("spawn").Pipe()
            self.connections.append(parent)
            credits = {}
            for kind, total in initial.items():
                quotient, remainder = divmod(total, count)
                credits[kind] = quotient + int(index < remainder)
            self.hooks.append(WorkerHooks(child, self.messages, self.aborted, credits))
            thread = threading.Thread(target=self.reserve, args=(parent,), daemon=True)
            thread.start()
            self.threads.append(thread)
        self.collector = threading.Thread(target=self.collect, daemon=True)
        self.collector.start()

    def reserve(self, connection):
        try:
            while True:
                kind, identity, amount = connection.recv()
                token = REQUEST.set(identity)
                try:
                    try:
                        result = self.budget.take(kind, amount)
                    except ModelError as error:
                        result = (error.code, str(error))
                    connection.send(result)
                finally:
                    REQUEST.reset(token)
        except EOFError:
            pass
        except Exception as error:
            self.errors.append(f"{type(error).__name__}: {error}")
            self.budget.aborted.set()
            self.aborted.value = True
        finally:
            connection.close()

    def collect(self):
        completed = 0
        failed = 0
        streak = 0
        while True:
            message = self.messages.get()
            if message is None:
                return
            kind, value = message
            if kind == "stage":
                self.events.append(value)
            elif kind == "worker":
                self.workers.append(value)
            elif kind == "interrupted":
                with self.budget.lock:
                    self.budget.interrupted.add(value)
            elif kind == "completion":
                completed += 1
                failed += int(value)
                streak = streak + 1 if value else 0
                if streak >= 3 or (completed >= 20 and failed / completed > 0.05):
                    self.budget.aborted.set()
                    self.aborted.value = True

    def close(self):
        for hook in self.hooks:
            hook.connection.close()
        for thread in self.threads:
            thread.join(5)
            if thread.is_alive():
                raise RuntimeError("预算协调线程未退出")
        self.messages.put(None)
        self.collector.join(5)
        if self.collector.is_alive():
            self.messages.cancel_join_thread()
            self.messages.close()
            raise RuntimeError("指标汇总线程未退出")
        self.messages.close()
        self.messages.join_thread()
        if self.errors:
            raise RuntimeError(f"预算协调失败: {self.errors}")


async def clients(port, args):
    if args.client_io == "native":
        requests = []
        for query in RETRIEVAL_QUERIES:
            if args.mode in ("mixed", "agent"):
                requests.append({"kind": 4, "query": "请检索 Linux 手册后回答：" + query})
            if args.mode in ("mixed", "rag"):
                requests.append({"kind": 2, "query": query})
        child = await asyncio.create_subprocess_exec(
            "build/socket-probe", stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE
        )
        try:
            settings = {"port": port, "count": args.count, "mode": "business", "requests": requests}
            output, _ = await asyncio.wait_for(
                child.communicate(json.dumps(settings).encode()), 135
            )
            if child.returncode != 0:
                raise RuntimeError("独立C业务客户端失败")
            return json.loads(output)["rows"]
        finally:
            if child.returncode is None:
                child.terminate()
                await child.wait()
    slots = asyncio.Semaphore(args.clients)

    async def request(identity):
        async with slots:
            started = time.monotonic()
            mode = (
                "rag"
                if args.mode == "rag" or (args.mode == "mixed" and identity % 2 == 0)
                else "agent"
            )
            query = RETRIEVAL_QUERIES[((identity - 1) // 2) % len(RETRIEVAL_QUERIES)]
            if mode == "agent":
                query = "请检索 Linux 手册后回答：" + query
            writer = None
            row = {"id": str(identity), "mode": mode, "code": "IO_ERROR", "started_s": started}
            try:
                async with asyncio.timeout(130):
                    reader, writer = await asyncio.open_connection("127.0.0.1", port)
                    body = json.dumps({"query": query}).encode()
                    writer.write(
                        HEADER.pack(0x52414754, 1, 2 if mode == "rag" else 4, len(body), identity)
                        + body
                    )
                    await writer.drain()
                    row["sent_s"] = time.monotonic()
                    header = HEADER.unpack(await reader.readexactly(20))
                    if (
                        header[:2] != (0x52414754, 1)
                        or header[4] != identity
                        or header[3] > 1048576
                    ):
                        raise ValueError("响应帧头非法")
                    payload = json.loads(await reader.readexactly(header[3]))
                    if header[2] == 101:
                        code = payload.get("code")
                        if code not in (
                            "INVALID_REQUEST",
                            "SERVER_BUSY",
                            "UPSTREAM_TIMEOUT",
                            "UPSTREAM_UNAVAILABLE",
                            "INTERNAL_ERROR",
                        ):
                            raise ValueError("错误响应代码非法")
                        row["code"] = code
                    elif (
                        header[2] == 100
                        and isinstance(payload.get("text"), str)
                        and payload["text"].strip()
                    ):
                        row["code"] = "OK"
                        if not payload.get("sources"):
                            row["code"] = "NO_EVIDENCE"
                        if any(
                            a["status"] != "success" for a in payload.get("actions", [])
                        ) or payload["text"].startswith(("请求超时", "达到工具")):
                            row["code"] = "PARTIAL"
            except (OSError, ValueError, TimeoutError, asyncio.IncompleteReadError):
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
            return row

    return await asyncio.gather(*(request(i) for i in range(1, args.count + 1)))


async def measure_workers(args):
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if hard < 4096:
        raise ValueError("千请求对照需要至少4096的文件描述符硬限制")
    resource.setrlimit(resource.RLIMIT_NOFILE, (max(soft, 4096), hard))
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x") as output:
        budget = Budget(
            args.model_budget,
            args.embedding_budget,
            args.budget_ledger,
            args.extend_budget_limits,
            args.phase_model_budget,
            args.phase_embedding_budget,
        )
        estimated_model = args.count
        if args.mode == "agent":
            estimated_model = 2 * args.count
        elif args.mode == "mixed":
            estimated_model = args.count + (args.count + 1) // 2
        estimated_embedding = estimated_model
        # 给进程间请求分配的不均衡留少量计量余量，避免尾部追加额度阻塞已有请求。
        reserved_model = (estimated_model * 11 + 9) // 10
        coordinator = Coordinator(budget, args.agent_workers, reserved_model, estimated_embedding)
        options = {
            "workers": args.agent_workers,
            "model": "deepseek",
            "retrieval": "qdrant",
            "timeout": 120,
            "model_concurrency": args.concurrency,
            "retrieval_workers": int(os.environ.get("RAGENT_RETRIEVAL_WORKERS", "8")),
        }
        group = None
        process = None
        sampler = None
        report = {"groups": [], "completed": False, "calls": budget.counts}
        peaks = {"python_rss_kib": 0, "c_rss_kib": 0}
        log_path = path.with_suffix(".server.log")
        try:
            group = WorkerGroup("127.0.0.1", 0, options, coordinator.hooks)
            python_port = await asyncio.to_thread(group.start)
            config = json.loads(Path("config/agent.json").read_text())
            config.update(
                port=0,
                worker_threads=args.workers,
                agent_url=f"http://127.0.0.1:{python_port}/agent",
            )
            config_path = path.with_suffix(".server.json")
            config_path.write_text(json.dumps(config))
            with log_path.open("x") as log:
                process = await asyncio.create_subprocess_exec(
                    args.server,
                    "--config",
                    str(config_path),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=log,
                    env=dict(os.environ, RAGENT_METRICS="1"),
                )
                stdout = cast(asyncio.StreamReader, process.stdout)
                line = await asyncio.wait_for(stdout.readline(), 30)
                if not line.startswith(b"Listening on "):
                    raise RuntimeError("C服务启动失败")
                port = int(line.rsplit(b":", 1)[1])

                async def sample():
                    while True:
                        group.check()
                        peaks["c_rss_kib"] = max(peaks["c_rss_kib"], memory(process.pid))
                        total = sum(memory(child.pid) for child in group.processes)
                        peaks["python_rss_kib"] = max(peaks["python_rss_kib"], total)
                        await asyncio.sleep(0.1)

                sampler = asyncio.create_task(sample())
                started = time.monotonic()
                c_cpu_started = process_cpu(process.pid)
                measurement = asyncio.create_task(clients(port, args))
                try:
                    done, _ = await asyncio.wait(
                        (measurement, sampler), return_when=asyncio.FIRST_COMPLETED
                    )
                    if sampler in done:
                        budget.aborted.set()
                        coordinator.aborted.value = True
                        await sampler
                    rows = await measurement
                finally:
                    if not measurement.done():
                        measurement.cancel()
                        await asyncio.gather(measurement, return_exceptions=True)
                elapsed = time.monotonic() - started
                if sampler.done():
                    await sampler
                for row in rows:
                    identity = str(row["id"])
                    if identity in budget.stopped:
                        row["code"] = "BUDGET_STOP"
                    elif identity in budget.interrupted:
                        row["code"] = "ABORTED"
                successful = [r["seconds"] for r in rows if r["code"] == "OK"]
                result = {
                    "workers": args.workers,
                    "agent_workers": args.agent_workers,
                    "model_concurrency": args.concurrency,
                    "retrieval_workers": options["retrieval_workers"],
                    "client_io": args.client_io,
                    "requests": args.count,
                    "success": len(successful),
                    "completion_rate": len(successful) / args.count,
                    "throughput": len(successful) / elapsed,
                    "elapsed_s": elapsed,
                    "c_cpu_percent": 100 * (process_cpu(process.pid) - c_cpu_started) / elapsed,
                    "p95_s": percentile(successful, 0.95),
                    "rows": rows,
                    "errors": dict(Counter(r["code"] for r in rows if r["code"] != "OK")),
                    **peaks,
                }
                result["by_mode"] = {}
                for mode in ("rag", "agent"):
                    selected = [r for r in rows if r["mode"] == mode]
                    result["by_mode"][mode] = {
                        "requests": len(selected),
                        "success": sum(r["code"] == "OK" for r in selected),
                        "p95_s": percentile(
                            [r["seconds"] for r in selected if r["code"] == "OK"], 0.95
                        ),
                    }
                inflight = []
                for row in rows:
                    if row.get("sent_s", 0) > 0:
                        inflight.extend(((row["sent_s"], 1), (row["finished_s"], -1)))
                active = 0
                result["max_sent_inflight"] = 0
                for _, change in sorted(inflight):
                    active += change
                    result["max_sent_inflight"] = max(result["max_sent_inflight"], active)
                report["groups"].append(result)
        finally:
            try:
                if sampler is not None:
                    sampler.cancel()
                    await asyncio.gather(sampler, return_exceptions=True)
                if process is not None and process.returncode is None:
                    process.terminate()
                    await asyncio.wait_for(process.wait(), 20)
            finally:
                try:
                    if group is not None:
                        await asyncio.to_thread(group.stop)
                finally:
                    try:
                        await asyncio.to_thread(coordinator.close)
                    finally:
                        budget.close()
                        server_events = []
                        if log_path.exists():
                            for line in log_path.read_text().splitlines():
                                match = re.search(
                                    r"\[TASK\] conn=(\d+) req=(\d+) event=(\w+) ns=(\d+)", line
                                )
                                if match:
                                    server_events.append(
                                        (int(match[4]), match[3], (match[1], match[2]))
                                    )
                        pending = set()
                        accepted_peak = 0
                        for _, event, identity in sorted(server_events):
                            if event == "accepted":
                                pending.add(identity)
                            else:
                                pending.discard(identity)
                            accepted_peak = max(accepted_peak, len(pending))
                        report["server_business_inflight_peak"] = accepted_peak
                        report["events"] = coordinator.events
                        report["worker_statistics"] = coordinator.workers
                        report["issued_calls"] = {
                            kind: sum(
                                worker["issued_calls"][kind] for worker in coordinator.workers
                            )
                            for kind in ("model", "embedding")
                        }
                        for measured in report["groups"]:
                            for row in measured["rows"]:
                                if str(row["id"]) in budget.interrupted:
                                    row["code"] = "ABORTED"
                            measured["errors"] = dict(
                                Counter(r["code"] for r in measured["rows"] if r["code"] != "OK")
                            )
                        if report["groups"]:
                            ids = {str(r["id"]) for r in report["groups"][0]["rows"]}
                            report["groups"][0]["stages"] = stage_summary(coordinator.events, ids)
                        report["completed"] = sys.exc_info()[0] is None and bool(report["groups"])
                        json.dump(report, output, indent=2)
        print(
            json.dumps({k: v for k, v in result.items() if k not in ("rows", "stages")}), flush=True
        )
