"""真实模型CMD验收：独立预算账本、固定用例，结果仅写入artifacts。"""

import argparse
import asyncio
import fcntl
import json
import os
from pathlib import Path
import tempfile
import time

from aiohttp import web

from rag.services import configured_services
from ragent.__main__ import create_app
from ragent.model import DeepSeekModel
from ragent.retrieval import ManualRetriever


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
    ("cmd", "仅使用命令工具执行pwd，输出当前目录的完整路径。不要调用其他命令。"),
    ("agent", "检索ls手册解释-a选项，并调用命令工具执行pwd报告当前目录。"),
    ("cmd", "仅调用命令工具执行touch approval-sentinel，不要执行其他命令。"),
]


async def run(options):
    budget = Budget(options.ledger, options.phase)
    embedding, store = configured_services()
    retriever = ManualRetriever(BudgetEmbedding(embedding, budget), store)
    model = DeepSeekModel(before_call=lambda: budget.reserve("model"))
    app = create_app(retriever, model, 120, client_commands=True)
    runner = web.AppRunner(app, handler_cancellation=True)
    server = None
    report = {"phase": options.phase, "cases": [], "passed": False}
    project = Path(__file__).resolve().parent.parent
    options.output.parent.mkdir(parents=True, exist_ok=True)
    try:
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        backend_port = runner.addresses[0][1]
        with tempfile.TemporaryDirectory(prefix="ragent-real-policy-") as directory:
            root = Path(directory)
            config = root / "agent.json"
            config.write_text(
                json.dumps(
                    {
                        "port": 0,
                        "worker_threads": 4,
                        "agent_timeout_ms": 125000,
                        "agent_url": f"http://127.0.0.1:{backend_port}/tasks",
                    }
                )
            )
            server = await asyncio.create_subprocess_exec(
                str(project / "build/agent-server"),
                "--config",
                str(config),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            assert server.stdout is not None
            line = await asyncio.wait_for(server.stdout.readline(), 10)
            if not line:
                raise RuntimeError("C服务未启动")
            port = int(line.strip().rsplit(b":", 1)[1])
            for index, (mode, query) in enumerate(CASES, 1):
                if options.case and index not in options.case:
                    continue
                started = time.monotonic()
                client = await asyncio.create_subprocess_exec(
                    str(project / "build/agent"),
                    "--port",
                    str(port),
                    "--mode",
                    mode,
                    "--query",
                    query,
                    cwd=directory,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                try:
                    stdout, stderr = await asyncio.wait_for(client.communicate(), 140)
                except BaseException:
                    if client.returncode is None:
                        client.kill()
                        await client.wait()
                    raise
                output = stdout.decode(errors="replace")
                diagnostic = stderr.decode(errors="replace")
                if index == 3:
                    passed = client.returncode == 3 and not (root / "approval-sentinel").exists()
                else:
                    passed = client.returncode == 0 and directory in output
                    if index == 2:
                        passed = passed and "ls(1)" in output
                report["cases"].append(
                    {
                        "case": index,
                        "mode": mode,
                        "query": query,
                        "exit_code": client.returncode,
                        "seconds": time.monotonic() - started,
                        "passed": passed,
                        "output": output,
                        "diagnostic": diagnostic,
                    }
                )
                print(f"case={index} passed={passed}", flush=True)
            report["passed"] = bool(report["cases"]) and all(
                case["passed"] for case in report["cases"]
            )
    finally:
        if server is not None and server.returncode is None:
            server.terminate()
            await asyncio.wait_for(server.wait(), 10)
        await runner.cleanup()
        await model.close()
        await retriever.close()
        report["budget"] = json.loads(options.ledger.read_text())
        options.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    if not report["passed"]:
        raise SystemExit(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("precheck", "formal", "fix"), required=True)
    parser.add_argument("--ledger", type=Path, default=Path("artifacts/cmd-validation/budget.json"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--case", type=int, action="append", choices=range(1, 4))
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
