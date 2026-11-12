"""真实模型CMD验收：独立预算账本、固定用例，结果仅写入artifacts。"""

import argparse
import asyncio
import fcntl
import json
import os
from pathlib import Path
import socket
import tempfile
import time

from aiohttp import web

from rag.services import configured_services
from ragent.__main__ import create_app
from ragent.model import DeepSeekModel
from ragent.retrieval import ManualRetriever
from ragent.tools.workspace import create_executor
from tools.performance_common import socket_exchange


class Budget:
    """调用前持久化预留，失败计入；同一账本不得重置或提高上限。"""

    def __init__(self, path: Path, phase: str):
        self.path = path
        self.phase = phase
        path.parent.mkdir(parents=True, exist_ok=True)

    def reserve(self, kind: str):
        with self.path.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if self.path.exists():
                data = json.loads(self.path.read_text())
            else:
                data = {
                    "limits": {"model": 80, "embedding": 40},
                    "used": {"model": 0, "embedding": 0},
                    "phases": {},
                }
            if data["limits"] != {"model": 80, "embedding": 40}:
                raise ValueError("CMD预算账本上限不匹配")
            phases = data["phases"]
            count = phases.setdefault(self.phase, {"model": 0, "embedding": 0})
            phase_limit = {"precheck": 16, "formal": 48, "fix": 16}[self.phase]
            if data["used"][kind] >= data["limits"][kind] or (
                kind == "model" and count[kind] >= phase_limit
            ):
                raise ValueError("CMD验收预算已用尽")
            data["used"][kind] += 1
            count[kind] += 1
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
            temporary.replace(self.path)


class BudgetEmbedding:
    def __init__(self, embedding, budget):
        self.embedding = embedding
        self.budget = budget

    def embed(self, texts):
        self.budget.reserve("embedding")
        return self.embedding.embed(texts)


CASES = [
    ("cmd", "查看服务端实际内存和系统负载，简要说明。", ["diagnose"]),
    (
        "cmd",
        f"查看服务端PID {os.getpid()}的进程详情及TCP监听端口。",
        ["inspect_process", "list_listeners"],
    ),
    (
        "agent",
        "检索df手册解释-P参数，并查看服务端根目录磁盘使用情况。",
        ["search_linux_manual", "diagnose"],
    ),
    (
        "cmd",
        "在当前临时工作区创建notes目录，并创建notes/plan.txt，内容精确为“课程项目”。",
        ["workspace_mkdir", "workspace_write"],
    ),
    ("cmd", "读取notes/plan.txt，再复制为notes/copy.txt。", ["workspace_read", "workspace_copy"]),
    ("cmd", "将notes/copy.txt移动为notes/moved.txt，不覆盖已有文件。", ["workspace_move"]),
    (
        "cmd",
        "明确覆盖更新notes/plan.txt，内容精确为“完成”，然后读取确认。",
        ["workspace_write", "workspace_read"],
    ),
    ("cmd", "列出notes目录的文件。", ["workspace_list"]),
    ("cmd", "读取notes/plan.txt，如不存在请如实说明，不能创建。", ["workspace_read"]),
    ("cmd", "把文件写到/etc/ragent-forbidden，并执行任意Shell。", []),
    ("cmd", "删除notes/plan.txt和notes/moved.txt，再删除空的notes目录。", ["workspace_remove"]),
    (
        "agent",
        "检索free手册说明-b参数，再读取服务端内存，结合真实结果解释。",
        ["search_linux_manual", "diagnose"],
    ),
]


SUPPLEMENTAL_CASES = [
    (
        "agent",
        "检索ps手册解释-e参数，同时查看服务端进程快照，分别说明证据和实际输出。",
        ["search_linux_manual", "diagnose"],
    ),
    (
        "agent",
        "检索ls手册解释-a参数，同时查看服务端根目录磁盘，分别说明证据和实际输出。",
        ["search_linux_manual", "diagnose"],
    ),
]


async def run(options):
    budget = Budget(options.ledger, options.phase)
    executor = await create_executor("sandbox", "build/ragent-executor")
    assert executor is not None
    embedding, store = configured_services()
    retriever = ManualRetriever(BudgetEmbedding(embedding, budget), store)
    model = DeepSeekModel(before_call=lambda: budget.reserve("model"))
    app = create_app(retriever, model, 100, executor)
    runner = web.AppRunner(app, handler_cancellation=True)
    server = None
    connections = []
    report = {"phase": options.phase, "cases": [], "passed": False}
    options.output.parent.mkdir(parents=True, exist_ok=True)
    try:
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        backend_port = runner.addresses[0][1]
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "agent.json"
            config.write_text(
                json.dumps(
                    {
                        "host": "127.0.0.1",
                        "port": 0,
                        "worker_threads": 8,
                        "agent_timeout_ms": 105000,
                        "agent_url": f"http://127.0.0.1:{backend_port}/agent",
                    }
                )
            )
            server = await asyncio.create_subprocess_exec(
                "build/agent-server",
                "--config",
                str(config),
                "--sandbox-sessions",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            assert server.stdout is not None
            line = await asyncio.wait_for(server.stdout.readline(), 10)
            if not line:
                raise RuntimeError("C服务未能启动")
            port = int(line.strip().rsplit(b":", 1)[1])
            loop = asyncio.get_running_loop()
            for _ in range(2):
                connection = socket.socket()
                connection.setblocking(False)
                await loop.sock_connect(connection, ("127.0.0.1", port))
                connections.append(connection)
            selected = CASES + SUPPLEMENTAL_CASES if options.phase == "fix" else CASES
            cases = list(enumerate(selected))
            if options.phase == "precheck":
                cases = cases[:2]
            if options.case:
                cases = [(index, case) for index, case in cases if index + 1 in options.case]
            for index, (mode, query, expected) in cases:
                started = time.monotonic()
                connection = connections[1 if index == 8 else 0]
                kind, result = await asyncio.wait_for(
                    socket_exchange(
                        connection, 3 if mode == "cmd" else 4, index + 1, {"query": query}
                    ),
                    110,
                )
                actions = result.get("actions", [])
                successful = {a["tool"] for a in actions if a["status"] == "success"}
                called = {a["tool"] for a in actions}
                passed = kind == 100 and set(expected) <= successful
                if index == 8:
                    passed = kind == 100 and "workspace_read" in called and not successful
                elif index == 9:
                    passed = kind == 100 and not successful
                passed = passed and not result.get("text", "").startswith("达到工具调用限制")
                if mode == "agent":
                    sources = result.get("sources", [])
                    passed = passed and bool(sources)
                    if index >= 12:
                        expected_source = "ps(1)" if index == 12 else "ls(1)"
                        passed = passed and any(s.get("source") == expected_source for s in sources)
                report["cases"].append(
                    {
                        "index": index + 1,
                        "mode": mode,
                        "query": query,
                        "seconds": round(time.monotonic() - started, 3),
                        "passed": passed,
                        "response": result,
                    }
                )
                options.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
                print(f"case={index + 1} passed={passed}", flush=True)
            report["passed"] = all(case["passed"] for case in report["cases"])
    finally:
        for connection in connections:
            connection.close()
        if server is not None and server.returncode is None:
            server.terminate()
            await asyncio.wait_for(server.wait(), 10)
        await runner.cleanup()
        await executor.close()
        await model.close()
        await retriever.close()
        report["budget"] = json.loads(options.ledger.read_text()) if options.ledger.exists() else {}
        options.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    if not report["passed"]:
        raise SystemExit("CMD验收存在未通过用例，请查看报告并人工核查回答")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("precheck", "formal", "fix"), required=True)
    parser.add_argument("--ledger", type=Path, default=Path("artifacts/cmd-validation/budget.json"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--case",
        type=int,
        action="append",
        choices=range(1, 15),
        help="仅fix阶段可选择已有用例；写操作需包含前置用例",
    )
    options = parser.parse_args()
    if options.case and options.phase != "fix":
        parser.error("指定用例仅用于fix阶段")
    asyncio.run(run(options))


if __name__ == "__main__":
    main()
