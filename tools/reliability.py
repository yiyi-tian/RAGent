"""隔离故障恢复与持续运行验收；模型及Embedding受控，不产生云端用量。"""

import argparse
import asyncio
from collections import Counter, deque
from contextlib import asynccontextmanager
import hashlib
import json
import logging
import re
import os
from pathlib import Path
import socket
import statistics
import sys
import time
import uuid

from aiohttp import web
from rag.services import Qdrant, ServiceError, configured_services, request_json
from ragent.__main__ import create_app
from ragent.retrieval import ManualRetriever
from tools.controlled_scale import ControlledModel
from tools.diagnose_retrieval import cached_queries
from tools.performance_common import HEADER, percentile


def save(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


async def command(*arguments):
    process = await asyncio.create_subprocess_exec(
        *arguments, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        output, error = await asyncio.wait_for(process.communicate(), 40)
        if process.returncode != 0:
            raise RuntimeError(f"{arguments[0]}失败: {error.decode(errors='replace')[:2000]}")
        return output.decode().strip()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def wait_http(url, process=None):
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        if process is not None and process.returncode is not None:
            raise RuntimeError("服务启动过程中退出")
        try:
            return await asyncio.to_thread(request_json, "GET", url, timeout=1)
        except ServiceError:
            await asyncio.sleep(0.2)
    raise TimeoutError(f"等待本地服务超时: {url}")


@asynccontextmanager
async def replica(directory):
    embedding, original = configured_services()
    name = "ragent-reliability-" + uuid.uuid4().hex[:10]
    volume = name + "-data"
    container_created = False
    volume_created = False
    try:
        image = await command(
            "docker", "inspect", "--format", "{{.Image}}", "ragent-qdrant-official"
        )
        await command("docker", "volume", "create", volume)
        volume_created = True
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            published_port = reservation.getsockname()[1]
        await command(
            "docker",
            "create",
            "--name",
            name,
            "--label",
            "ragent.reliability=true",
            "-p",
            f"127.0.0.1:{published_port}:6333",
            "-v",
            f"{volume}:/qdrant/storage",
            image,
        )
        container_created = True
        await command("docker", "start", name)
        address = await command("docker", "port", name, "6333/tcp")
        url = "http://" + address.splitlines()[0]
        await wait_http(url + "/collections")
        info = await asyncio.to_thread(original._call, "GET")
        config = info["result"]["config"]
        collection = original.url.rsplit("/", 1)[1]
        store = Qdrant(
            url,
            collection,
            embedding.model,
            embedding.base_url,
            strategy=original.retrieval_strategy,
        )
        await asyncio.to_thread(
            store._call,
            "PUT",
            data={"vectors": config["params"]["vectors"], "metadata": config["metadata"]},
        )
        offset = None
        points = 0
        ids = []
        while True:
            body = {"limit": 128, "with_payload": True, "with_vector": True}
            if offset is not None:
                body["offset"] = offset
            page = (await asyncio.to_thread(original._call, "POST", "/points/scroll", body))[
                "result"
            ]
            copied = []
            for point in page["points"]:
                copied.append({key: point[key] for key in ("id", "payload", "vector")})
                ids.append(str(point["id"]))
            if copied:
                await asyncio.to_thread(store._call, "PUT", "/points?wait=true", {"points": copied})
            points += len(copied)
            offset = page["next_page_offset"]
            if offset is None:
                break
        actual = await asyncio.to_thread(store._call, "POST", "/points/count", {"exact": True})
        source = await asyncio.to_thread(original._call, "POST", "/points/count", {"exact": True})
        if actual["result"]["count"] != points or source["result"]["count"] != points:
            raise ValueError("临时知识库复制数量不一致")
        save(
            directory / "replica.json",
            {
                "container": name,
                "volume": volume,
                "points": points,
                "ids_sha256": hashlib.sha256(json.dumps(sorted(ids)).encode()).hexdigest(),
                "source_collection": collection,
                "image": image,
            },
        )
        yield embedding, store, name, url
    finally:
        if container_created:
            await command("docker", "rm", "-f", name)
        if volume_created:
            await command("docker", "volume", "rm", volume)


class FrozenEmbedding:
    def __init__(self):
        cases = json.loads(Path("tests/rag/evidence_v2.json").read_text())["cases"]
        vectors = cached_queries(cases, Path("artifacts/rag-optimization/final"))
        selected = []
        sources = set()
        for case, vector in zip(cases, vectors):
            expected = case["expected_sources"]
            if expected and expected[0] not in sources:
                sources.add(expected[0])
                selected.append((case, vector))
            if len(selected) == 4:
                break
        if len(selected) != 4:
            raise ValueError("需要四个不同来源的冻结查询")
        self.queries = [case["query"] for case, _ in selected]
        self.case_ids = [case["id"] for case, _ in selected]
        self.vectors = {case["query"]: vector for case, vector in selected}

    def embed(self, texts):
        if len(texts) != 1 or texts[0] not in self.vectors:
            raise ValueError("受控测试只允许冻结查询")
        time.sleep(0.6)
        return [self.vectors[texts[0]]]


@asynccontextmanager
async def c_server(directory, endpoint, workers=64):
    config = json.loads(Path("config/agent.json").read_text())
    config.update(port=0, worker_threads=workers, agent_url=endpoint)
    path = directory / "server.json"
    save(path, config)
    process = None
    with (directory / "server.log").open("w") as log:
        try:
            process = await asyncio.create_subprocess_exec(
                "build/agent-server",
                "--config",
                str(path),
                stdout=asyncio.subprocess.PIPE,
                stderr=log,
                env=dict(os.environ, RAGENT_METRICS="1"),
            )
            line = await asyncio.wait_for(process.stdout.readline(), 30)
            if not line.startswith(b"Listening on "):
                raise RuntimeError("C服务未就绪")
            yield int(line.rsplit(b":", 1)[1]), process.pid
        finally:
            if process is not None and process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 15)
                except TimeoutError:
                    process.kill()
                    await process.wait()
                    raise


@asynccontextmanager
async def harness(directory, retriever, model, workers=64):
    runner = web.AppRunner(
        create_app(retriever, model, 120), access_log=None, handler_cancellation=True
    )
    await runner.setup()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    python_port = listener.getsockname()[1]
    try:
        await web.SockSite(runner, listener).start()
        async with c_server(directory, f"http://127.0.0.1:{python_port}/agent", workers) as (
            port,
            pid,
        ):
            yield port, python_port, pid
    finally:
        await runner.cleanup()


async def exchange(port, identity, query, mode):
    started = time.monotonic()
    writer = None
    try:
        async with asyncio.timeout(130):
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            body = json.dumps({"query": query}).encode()
            writer.write(HEADER.pack(0x52414754, 1, mode, len(body), identity) + body)
            await writer.drain()
            header = HEADER.unpack(await reader.readexactly(20))
            if header[:2] != (0x52414754, 1) or header[4] != identity or header[3] > 1048576:
                raise ValueError("响应帧头非法")
            response = json.loads(await reader.readexactly(header[3]))
            if header[2] == 101:
                code = response["code"]
            elif header[2] == 100:
                code = "OK"
                if mode != 1 and not response.get("sources"):
                    code = "NO_EVIDENCE"
                if any(action["status"] == "failed" for action in response.get("actions", [])):
                    code = "PARTIAL"
                if response.get("text", "").startswith(("请求超时", "达到工具")):
                    code = "PARTIAL"
            else:
                raise ValueError("响应类型非法")
            return {
                "id": identity,
                "code": code,
                "seconds": time.monotonic() - started,
                "mode": mode,
            }
    finally:
        if writer is not None:
            writer.close()
            await writer.wait_closed()


def resources(pid):
    status = Path(f"/proc/{pid}/status").read_text().splitlines()
    result = {}
    for line in status:
        if line.startswith("VmRSS:"):
            result["rss_kib"] = int(line.split()[1])
        elif line.startswith("Threads:"):
            result["threads"] = int(line.split()[1])
    result["fds"] = len(list(Path(f"/proc/{pid}/fd").iterdir()))
    return result


async def faults(directory, embedding, store, container):
    frozen = FrozenEmbedding()
    embedding.embed = frozen.embed
    retrieve = ManualRetriever(embedding, store)
    model = ControlledModel(32, 0.01)
    results = []
    try:
        async with harness(directory, retrieve, model) as (port, python_port, _):
            query = frozen.queries[0]
            before = await exchange(port, 1, query, 2)
            assert before["code"] == "OK", before
            await command("docker", "stop", "--time", "1", container)
            live = await asyncio.to_thread(
                request_json, "GET", f"http://127.0.0.1:{python_port}/health"
            )
            assert live == {"alive": True}
            try:
                await asyncio.to_thread(
                    request_json, "GET", f"http://127.0.0.1:{python_port}/ready"
                )
            except ServiceError:
                pass
            else:
                raise AssertionError("Qdrant停止后ready未失败")
            failed = await exchange(port, 2, query, 2)
            assert failed["code"] == "UPSTREAM_UNAVAILABLE", failed
            await command("docker", "start", container)
            await wait_http(store.base_url + "/collections")
            await wait_http(f"http://127.0.0.1:{python_port}/ready")
            recovered = await exchange(port, 3, query, 2)
            assert recovered["code"] == "OK", recovered
            results.append(
                {
                    "case": "qdrant-stop-start",
                    "passed": True,
                    "before": before,
                    "failed": failed,
                    "recovered": recovered,
                }
            )
    finally:
        await retrieve.close()
    return results


async def model_faults(directory):
    from langchain_openai import ChatOpenAI
    from unittest.mock import patch
    from ragent.model import DeepSeekModel

    state = {"status": 200, "calls": 0}

    async def handler(request):
        await request.json()
        state["calls"] += 1
        status = state["status"]
        if status == "timeout":
            await asyncio.sleep(0.5)
        if status == "disconnect":
            if request.transport is not None:
                request.transport.close()
            return web.Response()
        if status == "malformed":
            return web.Response(text="invalid", content_type="application/json")
        if isinstance(status, int) and status != 200:
            return web.json_response({"error": {"message": "受控故障"}}, status=status)
        return web.json_response(
            {
                "id": "fixture",
                "object": "chat.completion",
                "created": 1,
                "model": "controlled",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "恢复成功"},
                    }
                ],
            }
        )

    async def start_api(port=0):
        app = web.Application()
        app.router.add_post("/chat/completions", handler)
        runner = web.AppRunner(app, access_log=None, handler_cancellation=True)
        await runner.setup()
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", port))
        selected = listener.getsockname()[1]
        await web.SockSite(runner, listener).start()
        return runner, selected

    api, api_port = await start_api()
    with patch.dict(
        os.environ,
        {
            "RAGENT_API_KEY": "controlled",
            "RAGENT_MODEL": "controlled",
            "RAGENT_BASE_URL": "https://unused.invalid",
        },
    ):
        model = DeepSeekModel(2)
    await model.close()
    model.client = ChatOpenAI(
        model="controlled",
        api_key="controlled",
        base_url=f"http://127.0.0.1:{api_port}/",
        timeout=0.15,
        max_retries=0,
    )
    model.timeout = 0.2
    results = []
    try:
        async with harness(directory, None, model) as (port, _, _):
            identity = 0
            for status, expected in (
                (401, "UPSTREAM_UNAVAILABLE"),
                (402, "UPSTREAM_UNAVAILABLE"),
                (429, "SERVER_BUSY"),
                (500, "UPSTREAM_UNAVAILABLE"),
                ("malformed", "UPSTREAM_UNAVAILABLE"),
                ("timeout", "UPSTREAM_TIMEOUT"),
                ("disconnect", "UPSTREAM_UNAVAILABLE"),
            ):
                state["status"] = status
                identity += 1
                failed = await asyncio.wait_for(exchange(port, identity, "test", 1), 5)
                assert failed["code"] == expected, (status, failed)
                state["status"] = 200
                identity += 1
                recovered = await asyncio.wait_for(exchange(port, identity, "test", 1), 5)
                assert recovered["code"] == "OK", (status, recovered)
                results.append(
                    {"case": str(status), "failed": failed, "recovered": recovered, "passed": True}
                )
            await api.cleanup()
            api = None
            failed = await asyncio.wait_for(exchange(port, 99, "test", 1), 5)
            assert failed["code"] == "UPSTREAM_UNAVAILABLE", failed
            api, _ = await start_api(api_port)
            recovered = await asyncio.wait_for(exchange(port, 100, "test", 1), 5)
            assert recovered["code"] == "OK", recovered
            results.append(
                {"case": "api-stop-start", "failed": failed, "recovered": recovered, "passed": True}
            )
    finally:
        await model.close()
        if api is not None:
            await api.cleanup()
    return results


async def process_faults(directory):
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        python_port = reservation.getsockname()[1]
    process = None
    with (directory / "python.log").open("w") as log:

        async def start():
            child = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "ragent", "--port", str(python_port), stdout=log, stderr=log
            )
            ready = False
            try:
                await wait_http(f"http://127.0.0.1:{python_port}/ready", child)
                ready = True
                return child
            finally:
                if not ready and child.returncode is None:
                    child.terminate()
                    await child.wait()

        try:
            process = await start()
            async with c_server(directory, f"http://127.0.0.1:{python_port}/agent") as (port, _):
                before = await exchange(port, 1, "test", 1)
                assert before["code"] == "OK", before
                process.terminate()
                await asyncio.wait_for(process.wait(), 15)
                failed = await exchange(port, 2, "test", 1)
                assert failed["code"] == "UPSTREAM_UNAVAILABLE", failed
                process = await start()
                recovered = await exchange(port, 3, "test", 1)
                assert recovered["code"] == "OK", recovered
                return {
                    "case": "python-stop-start",
                    "before": before,
                    "failed": failed,
                    "recovered": recovered,
                    "passed": True,
                }
        finally:
            if process is not None and process.returncode is None:
                process.terminate()
                await asyncio.wait_for(process.wait(), 15)


async def native_smoke(directory, embedding, store):
    frozen = FrozenEmbedding()
    embedding.embed = frozen.embed
    retrieve = ManualRetriever(embedding, store, workers=16)
    model = ControlledModel(64, 0.01)
    try:
        async with harness(directory, retrieve, model, 128) as (port, _, _):
            requests = []
            for query in frozen.queries:
                requests.extend([{"kind": 4, "query": query}, {"kind": 2, "query": query}])
            process = await asyncio.create_subprocess_exec(
                "build/socket-probe", stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE
            )
            try:
                settings = {"port": port, "count": 64, "mode": "business", "requests": requests}
                output, _ = await asyncio.wait_for(
                    process.communicate(json.dumps(settings).encode()), 30
                )
                if process.returncode != 0:
                    raise RuntimeError("C业务客户端冒烟失败")
                report = json.loads(output)
                report["counts"] = dict(Counter(row["code"] for row in report["rows"]))
                report["real_api_calls"] = {"model": 0, "embedding": 0}
                report["smoke_only"] = True
                report["passed"] = report["counts"] == {"OK": 64}
                save(directory / "report.json", report)
                if not report["passed"]:
                    raise AssertionError(report["counts"])
                return report
            finally:
                if process.returncode is None:
                    process.terminate()
                    await process.wait()
    finally:
        await retrieve.close()


async def soak(args, directory, embedding, store):
    frozen = FrozenEmbedding()
    embedding.embed = frozen.embed
    retrieve = ManualRetriever(embedding, store)
    model = ControlledModel(args.concurrency, 2)
    slots = asyncio.Semaphore(32)
    counts = Counter()
    latencies = {2: deque(maxlen=10000), 4: deque(maxlen=10000)}
    samples = []
    tasks = set()
    duration = args.duration
    send_seconds = duration - 300 if duration >= 3600 else max(1, duration - 10)
    report = {
        "duration_s": duration,
        "send_s": send_seconds,
        "real_api_calls": {"model": 0, "embedding": 0},
        "case_ids": frozen.case_ids,
        "full_acceptance": duration >= 3600,
        "passed": False,
        "counts": counts,
    }
    try:
        async with harness(directory, retrieve, model, args.workers) as (port, _, server_pid):
            started = time.monotonic()
            stop_sampling = asyncio.Event()
            row_file = (directory / "requests.jsonl").open("w")

            async def sample():
                last_progress = -1
                pool = None
                with (directory / "server.log").open() as server_log:
                    while not stop_sampling.is_set():
                        for line in server_log:
                            match = re.search(r"queued=(\d+) active=(\d+) results=(\d+)", line)
                            if match:
                                pool = {
                                    "queued": int(match[1]),
                                    "active": int(match[2]),
                                    "results": int(match[3]),
                                }
                        elapsed = time.monotonic() - started
                        record = {
                            "seconds": elapsed,
                            "c": resources(server_pid),
                            "python": resources(os.getpid()),
                            "inflight": len(tasks),
                            "pool_last_observed": pool,
                        }
                        samples.append(record)
                        minute = int(elapsed // 60)
                        if minute != last_progress:
                            print(
                                json.dumps(
                                    {
                                        "minute": minute,
                                        "completed": sum(counts.values()),
                                        "counts": dict(counts),
                                        "resources": record,
                                    }
                                ),
                                flush=True,
                            )
                            last_progress = minute
                        await asyncio.sleep(1)

            async def request(identity, scheduled):
                async with slots:
                    dispatched = time.monotonic()
                    try:
                        row = await exchange(
                            port,
                            identity,
                            frozen.queries[((identity - 1) // 2) % 4],
                            2 if identity % 2 == 0 else 4,
                        )
                    except (
                        OSError,
                        ValueError,
                        TimeoutError,
                        asyncio.IncompleteReadError,
                    ) as error:
                        row = {
                            "id": identity,
                            "code": type(error).__name__,
                            "seconds": time.monotonic() - dispatched,
                        }
                    except Exception as error:
                        logging.getLogger(__name__).exception("持续运行请求异常 id=%s", identity)
                        row = {
                            "id": identity,
                            "code": "INTERNAL_ERROR",
                            "error_type": type(error).__name__,
                            "seconds": time.monotonic() - dispatched,
                        }
                    row["dispatch_wait_s"] = dispatched - scheduled
                    row["seconds"] += row["dispatch_wait_s"]
                    counts[row["code"]] += 1
                    if row["code"] == "OK":
                        latencies[row["mode"]].append(row["seconds"])
                    row_file.write(json.dumps(row) + "\n")

            sampler = asyncio.create_task(sample())
            try:
                total = int(send_seconds * 2.5)
                for identity in range(1, total + 1):
                    if sampler.done():
                        await sampler
                    target = started + (identity - 1) / 2.5
                    await asyncio.sleep(max(0, target - time.monotonic()))
                    if len(tasks) >= 64:
                        counts["CLIENT_BACKLOG"] += 1
                        row_file.write(
                            json.dumps({"id": identity, "code": "CLIENT_BACKLOG"}) + "\n"
                        )
                        continue
                    task = asyncio.create_task(request(identity, target))
                    tasks.add(task)
                    task.add_done_callback(tasks.discard)
                await asyncio.gather(*list(tasks))
                await asyncio.sleep(max(0, started + duration - time.monotonic()))
                if sampler.done():
                    await sampler
            finally:
                stop_sampling.set()
                sampler.cancel()
                await asyncio.gather(sampler, return_exceptions=True)
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*list(tasks), return_exceptions=True)
                row_file.close()
                save(directory / "resources.json", samples)
            report["sent"] = total
            report["unfinished"] = len(tasks)
            report["p95_s"] = {
                str(mode): percentile(list(values), 0.95) for mode, values in latencies.items()
            }
            report["final"] = samples[-1]
            checks = {
                "all_success": counts["OK"] == total and sum(counts.values()) == total,
                "drained": len(tasks) == 0,
                "rag_latency": report["p95_s"]["2"] <= 15,
                "agent_latency": report["p95_s"]["4"] <= 25,
            }
            baseline = [sample for sample in samples if 600 <= sample["seconds"] < 900]
            end = [sample for sample in samples if 3000 <= sample["seconds"] < 3300]
            if duration >= 3600:
                for name, limit in (("c", 8192), ("python", 32768)):
                    growth = statistics.median(s[name]["rss_kib"] for s in end) - statistics.median(
                        s[name]["rss_kib"] for s in baseline
                    )
                    checks[name + "_rss"] = growth <= limit
                    report[name + "_rss_growth_kib"] = growth
                    # 空闲末态须与预热后的低水位比较，不以负载峰值掩盖泄漏。
                    idle = min(baseline, key=lambda s: s[name]["fds"])[name]
                    checks[name + "_fds"] = samples[-1][name]["fds"] <= idle["fds"] + 4
                    checks[name + "_threads"] = samples[-1][name]["threads"] <= idle["threads"] + 2
            report["checks"] = checks
            report["passed"] = report["full_acceptance"] and all(checks.values())
    finally:
        await retrieve.close()
        save(directory / "report.json", report)
    shutdown = (directory / "server.log").read_text()
    gauge = re.findall(r"\[POOL\] shutdown queued=(\d+) active=(\d+) results=(\d+)", shutdown)
    if not gauge:
        report["checks"]["pool_empty"] = False
    else:
        report["pool_at_shutdown"] = list(map(int, gauge[-1]))
        report["checks"]["pool_empty"] = report["pool_at_shutdown"] == [0, 0, 0]
    report["passed"] = report["full_acceptance"] and all(report["checks"].values())
    save(directory / "report.json", report)
    return report


async def main_async(args):
    directory = Path(args.output)
    directory.mkdir(parents=True, exist_ok=False)
    async with replica(directory) as (embedding, store, container, _):
        if args.native_smoke:
            report = await native_smoke(directory, embedding, store)
            print(json.dumps({k: v for k, v in report.items() if k != "rows"}), flush=True)
        elif args.faults_only:
            for name in ("qdrant", "model", "python"):
                (directory / name).mkdir()
            results = await faults(directory / "qdrant", embedding, store, container)
            results.extend(await model_faults(directory / "model"))
            results.append(await process_faults(directory / "python"))
            report = {
                "passed": all(r["passed"] for r in results),
                "results": results,
                "real_api_calls": {"model": 0, "embedding": 0},
            }
            save(directory / "report.json", report)
            print(json.dumps(results), flush=True)
        else:
            report = await soak(args, directory, embedding, store)
            print(json.dumps(report), flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--duration", type=int, default=3600)
    parser.add_argument("--faults-only", action="store_true")
    parser.add_argument("--native-smoke", action="store_true")
    parser.add_argument("--workers", type=int, default=64)
    parser.add_argument("--concurrency", type=int, default=32)
    args = parser.parse_args()
    if not 1 <= args.workers <= 256 or not 1 <= args.concurrency <= 256:
        parser.error("工作线程和模型并发必须为1至256")
    if not 15 <= args.duration <= 3600:
        parser.error("持续时间必须为15至3600秒；少于3600秒只用于冒烟")
    report = asyncio.run(main_async(args))
    success = report["passed"]
    if report.get("full_acceptance") is False:
        success = all(report["checks"].values())
    if not success:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
