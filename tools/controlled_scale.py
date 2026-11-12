"""真实 C/Python/Qdrant 路径的规模演练；Embedding 和模型延迟受控，不调用云端。"""

import argparse
import asyncio
from collections import Counter
import json
import math
import os
from pathlib import Path
import resource
import socket
import sys
import threading
import time

from aiohttp import web
from langchain_core.messages import AIMessage, ToolMessage
from ragent.__main__ import create_app
from ragent.retrieval import ManualRetriever
from rag.services import configured_services
from tools.benchmark_agent import HEADER, memory, percentile
from tools.compare_rag import write_json
from tools.diagnose_retrieval import cached_queries


class ControlledEmbedding:
    def __init__(self, query, vector, delay):
        self.query = query
        self.vector = vector
        self.delay = delay
        self.calls = 0
        self.lock = threading.Lock()

    def embed(self, texts):
        if texts != [self.query]:
            raise ValueError("受控试验只允许冻结的查询")
        with self.lock:
            self.calls += 1
        time.sleep(self.delay)
        return [self.vector]


class ControlledModel:
    def __init__(self, concurrency, delay):
        self.slots = asyncio.Semaphore(concurrency)
        self.delay = delay
        self.gate = asyncio.Event()
        self.gate.set()
        self.calls = 0
        self.active = 0
        self.maximum = 0

    async def ainvoke(self, messages, allowed):
        await self.gate.wait()
        async with self.slots:
            self.calls += 1
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            try:
                await asyncio.sleep(self.delay)
            finally:
                self.active -= 1
        if "search_linux_manual" in allowed and not any(
            isinstance(m, ToolMessage) for m in messages
        ):
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "search_linux_manual",
                        "args": {"query": messages[0].content},
                        "id": "lookup",
                    }
                ],
            )
        return AIMessage(content="受控后端响应，用于并发调度验证，不代表真实模型回答。")


async def receive(reader, identity):
    magic, version, kind, length, returned = HEADER.unpack(await reader.readexactly(20))
    if (magic, version, returned) != (0x52414754, 1, identity) or length > 1048576:
        raise ValueError("响应帧非法")
    data = json.loads(await reader.readexactly(length))
    if kind == 101:
        return data["code"]
    if kind != 100 or not data.get("sources"):
        return "INVALID_RESPONSE"
    return "OK"


async def main_async(args):
    directory = Path(args.output)
    directory.mkdir(parents=True, exist_ok=False)
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    required = 8192
    if soft < required:
        if hard < required:
            raise ValueError("规模演练需要至少8192的文件描述符硬限制")
        resource.setrlimit(resource.RLIMIT_NOFILE, (required, hard))
    cases = json.loads(Path("tests/rag/evidence_v2.json").read_text())["cases"]
    vectors = cached_queries(cases, Path("artifacts/rag-optimization/final"))
    case = cases[0]
    embedding, store = configured_services()
    fake_embedding = ControlledEmbedding(case["query"], vectors[0], args.embedding_delay)
    # 覆盖云端调用；Qdrant 的模型、地址身份仍使用当前配置校验。
    embedding.embed = fake_embedding.embed
    retrieve = ManualRetriever(embedding, store)
    model = ControlledModel(args.model_concurrency, args.model_delay)
    runner = web.AppRunner(
        create_app(retrieve, model, 120), access_log=None, handler_cancellation=True
    )
    await runner.setup()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    python_port = listener.getsockname()[1]
    await web.SockSite(runner, listener).start()
    report = {
        "backend": "controlled",
        "real_api_calls": {"model": 0, "embedding": 0},
        "model_delay_s": args.model_delay,
        "embedding_delay_s": args.embedding_delay,
        "workers": args.workers,
        "model_concurrency": args.model_concurrency,
        "retrieval_workers": retrieve.workers,
        "groups": [],
    }

    async def run_group(count, overload=False):
        config = json.loads(Path("config/agent.json").read_text())
        config.update(
            port=0,
            max_connections=2048,
            worker_threads=args.workers,
            task_capacity=1024,
            result_capacity=256,
            agent_url=f"http://127.0.0.1:{python_port}/agent",
            agent_timeout_ms=125000,
        )
        path = directory / f"config-{count}.json"
        write_json(path, config)
        rows = []
        writers = []
        gate = asyncio.Event()
        peak = {"c_rss_kib": 0, "python_rss_kib": 0}
        with (directory / f"server-{count}.log").open("x") as log:
            server = await asyncio.create_subprocess_exec(
                str(Path("build/agent-server").resolve()),
                "--config",
                str(path),
                stdout=asyncio.subprocess.PIPE,
                stderr=log,
                env=dict(os.environ, RAGENT_METRICS="1"),
            )
            tasks = []
            sampler = None
            try:
                assert server.stdout is not None
                line = await asyncio.wait_for(server.stdout.readline(), 10)
                if not line.startswith(b"Listening on "):
                    raise RuntimeError("C 服务启动失败")
                port = int(line.decode().strip().rsplit(":", 1)[1])
                if overload:
                    model.gate.clear()
                    command = (
                        ["build/socket-probe"]
                        if args.client_io == "native"
                        else [sys.executable, "-m", "tools.overload_probe"]
                    )
                    child = await asyncio.create_subprocess_exec(
                        *command,
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                    )
                    try:
                        settings = {
                            "port": port,
                            "count": count,
                            "query": case["query"],
                            "preconnect": args.preconnect,
                            "client_io": args.client_io,
                            "mode": "overload",
                        }
                        output, _ = await asyncio.wait_for(
                            child.communicate(json.dumps(settings).encode()), 30
                        )
                        if child.returncode != 0:
                            raise RuntimeError("独立过载客户端失败")
                        result = json.loads(output)
                        report["groups"].append(result)
                        write_json(directory / "report.json", report)
                        print(json.dumps(result), flush=True)
                        return
                    finally:
                        if child.returncode is None:
                            child.terminate()
                            await child.wait()

                async def sample():
                    while True:
                        peak["c_rss_kib"] = max(peak["c_rss_kib"], memory(server.pid))
                        peak["python_rss_kib"] = max(peak["python_rss_kib"], memory(os.getpid()))
                        await asyncio.sleep(0.1)

                async def client(identity):
                    await gate.wait()
                    start = time.monotonic()
                    row = {"id": identity, "code": "IO_ERROR"}
                    writer = None
                    try:
                        async with asyncio.timeout(130):
                            reader, writer = await asyncio.open_connection("127.0.0.1", port)
                            writers.append(writer)
                            body = json.dumps({"query": case["query"]}).encode()
                            mode = 2 if identity % 2 == 0 else 4
                            writer.write(
                                HEADER.pack(0x52414754, 1, mode, len(body), identity) + body
                            )
                            await writer.drain()
                            row["code"] = await receive(reader, identity)
                    except (OSError, ValueError, TimeoutError, asyncio.IncompleteReadError):
                        row["code"] = "IO_ERROR"
                    finally:
                        if writer is not None:
                            writer.close()
                            try:
                                await writer.wait_closed()
                            except OSError:
                                row["code"] = "IO_ERROR"
                    row["seconds"] = time.monotonic() - start
                    rows.append(row)
                    if len(rows) % 100 == 0:
                        print(f"clients={count} completed={len(rows)}", flush=True)

                sampler = asyncio.create_task(sample())
                started = time.monotonic()
                if args.client_io == "native":
                    child = await asyncio.create_subprocess_exec(
                        "build/socket-probe",
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                    )
                    try:
                        settings = {
                            "port": port,
                            "count": count,
                            "mode": "business",
                            "requests": [
                                {"kind": 4, "query": case["query"]},
                                {"kind": 2, "query": case["query"]},
                            ],
                        }
                        output, _ = await asyncio.wait_for(
                            child.communicate(json.dumps(settings).encode()), 135
                        )
                        if child.returncode != 0:
                            raise RuntimeError("独立C业务客户端失败")
                        rows = json.loads(output)["rows"]
                    finally:
                        if child.returncode is None:
                            child.terminate()
                            await child.wait()
                else:
                    tasks = [asyncio.create_task(client(i)) for i in range(1, count + 1)]
                    gate.set()
                    await asyncio.gather(*tasks)
                elapsed = time.monotonic() - started
                success = [row["seconds"] for row in rows if row["code"] == "OK"]
                result = {
                    "clients": count,
                    "client_io": args.client_io,
                    "success": len(success),
                    "errors": dict(Counter(row["code"] for row in rows if row["code"] != "OK")),
                    "elapsed_s": elapsed,
                    "throughput": len(success) / elapsed,
                    "p50_s": percentile(success, 0.5),
                    "p95_s": percentile(success, 0.95),
                    "rows": rows,
                    **peak,
                }
                report["groups"].append(result)
                write_json(directory / "report.json", report)
                print(json.dumps({k: v for k, v in result.items() if k != "rows"}), flush=True)
            finally:
                model.gate.set()
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                if sampler is not None:
                    sampler.cancel()
                    await asyncio.gather(sampler, return_exceptions=True)
                if server.returncode is None:
                    server.terminate()
                    await asyncio.wait_for(server.wait(), 20)

    try:
        for count in args.counts:
            await run_group(count, overload=args.overload or count == 1100)
    finally:
        await runner.cleanup()
        await retrieve.close()
        report["simulated_model_calls"] = model.calls
        report["simulated_embedding_calls"] = fake_embedding.calls
        report["max_model_active"] = model.maximum
        write_json(directory / "report.json", report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--overload", action="store_true", help="显式执行队列满探测")
    parser.add_argument("--preconnect", action="store_true", help="过载探测先建立连接再提交请求")
    parser.add_argument("--counts", type=int, nargs="+", default=[100, 300, 1000, 1100])
    parser.add_argument("--workers", type=int, default=64)
    parser.add_argument("--model-concurrency", type=int, default=32)
    parser.add_argument("--model-delay", type=float, default=2.0)
    parser.add_argument("--embedding-delay", type=float, default=0.6)
    parser.add_argument("--client-io", choices=("stream", "socket", "native"), default="stream")
    args = parser.parse_args()
    if args.client_io == "native" and args.preconnect:
        parser.error("C客户端使用冷连接，连接复用请选择stream或socket")
    if (
        args.client_io == "socket"
        and not args.overload
        and any(count != 1100 for count in args.counts)
    ):
        parser.error("socket仅用于1100连接过载对照，业务负载请选择stream或native")
    if any(not 1 <= count <= 2046 for count in args.counts):
        parser.error("请求数必须为1至2046；1100或--overload用于过载探测")
    if not 1 <= args.workers <= 256 or not 1 <= args.model_concurrency <= 256:
        parser.error("工作线程和模型并发必须为1至256")
    for delay in (args.model_delay, args.embedding_delay):
        if not math.isfinite(delay) or delay < 0:
            parser.error("模拟延迟必须为有限非负数")
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
