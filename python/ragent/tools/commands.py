"""通过独立C执行器运行固定只读诊断，不接受Shell或任意命令行。"""

import asyncio
import json
import os
from pathlib import Path
import signal

from rag.runtime import remaining_timeout

COMMAND_ERRORS = {
    "COMMAND_NOT_ALLOWED": "操作不在只读白名单内",
    "COMMAND_TIMEOUT": "命令执行超时",
    "COMMAND_OUTPUT_LIMIT": "命令输出超过限制",
    "COMMAND_FAILED": "命令执行失败",
    "SERVER_BUSY": "工具容量已满",
    "UPSTREAM_UNAVAILABLE": "工具执行环境不可用",
}


class CommandError(Exception):
    def __init__(self, code: str, message: str, effect: str = "none"):
        super().__init__(message)
        self.code = code
        self.effect = effect


class ReadOnlyExecutor:
    def __init__(self, path: str, concurrency: int = 4):
        if not 1 <= concurrency <= 4:
            raise ValueError("只读命令并发须为1至4")
        executable = Path(path).resolve()
        self.path = str(executable)
        self.check_ready()
        self.slots = asyncio.Semaphore(concurrency)
        self.processes: set[asyncio.subprocess.Process] = set()
        self.tasks: set[asyncio.Task] = set()
        self.closed = False

    def check_ready(self):
        if not Path(self.path).is_file() or not os.access(self.path, os.X_OK):
            raise ValueError(f"C执行器不可用：{self.path}，请先构建或指定 --executor")

    async def _stop(self, process: asyncio.subprocess.Process):
        if process.returncode is None:
            try:
                # C执行器接收信号后负责杀死命令进程组并waitpid回收。
                process.terminate()
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(process.communicate(), 3)
        except TimeoutError:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.communicate()

    async def ainvoke(self, target: str) -> dict:
        return await self.invoke_tool("diagnose", {"target": target})

    async def invoke_tool(self, name: str, args: dict) -> dict:
        payload = dict(args)
        if name == "inspect_process":
            payload["target"] = "process_detail"
        elif name == "list_listeners":
            payload["target"] = "listeners"
        elif name != "diagnose":
            raise CommandError("COMMAND_NOT_ALLOWED", "未知只读工具")
        payload["timeout_ms"] = max(1, int(remaining_timeout(3) * 1000))
        result = await self.run_request(payload, 3)
        result["summary"] = f"已完成只读诊断：{name}"
        return result

    async def run_request(
        self,
        payload: dict,
        timeout: float,
        program: str | None = None,
        arguments: tuple[str, ...] = (),
        control: bool = False,
        mutating: bool = False,
    ) -> dict:
        if self.closed:
            raise CommandError("COMMAND_FAILED", "C执行器已关闭")
        task = asyncio.current_task()
        assert task is not None
        self.tasks.add(task)
        slots = asyncio.Semaphore(1) if control else self.slots
        effect = "unknown" if mutating else "none"
        try:
            async with slots:
                seconds = remaining_timeout(timeout)
                if program is None and "timeout_ms" in payload:
                    payload["timeout_ms"] = max(1, int(seconds * 1000))
                request = json.dumps(payload, ensure_ascii=False).encode()
                launching = asyncio.create_task(
                    asyncio.create_subprocess_exec(
                        program or self.path,
                        *arguments,
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL,
                        start_new_session=True,
                        env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
                    )
                )
                try:
                    # 启动过程也可能收到取消；先拿到进程句柄，才能可靠终止和回收。
                    process = await asyncio.shield(launching)
                except asyncio.CancelledError:
                    try:
                        process = await launching
                    except OSError:
                        raise asyncio.CancelledError from None
                    await self._stop(process)
                    raise
                except OSError as error:
                    raise CommandError("COMMAND_FAILED", "无法启动C执行器") from error
                self.processes.add(process)
                try:
                    try:
                        raw, _ = await asyncio.wait_for(process.communicate(request), seconds + 0.5)
                    except TimeoutError as error:
                        raise CommandError(
                            "COMMAND_TIMEOUT", "C执行器未在时限内返回", effect
                        ) from error
                    if process.returncode != 0 or len(raw) > 128 * 1024:
                        raise CommandError(
                            "COMMAND_FAILED",
                            f"C执行器异常退出或响应过大（退出码{process.returncode}）",
                            effect,
                        )
                    try:
                        # Linux进程名可能含非UTF-8字节，对展示文本使用替换字符。
                        result = json.loads(raw.decode("utf-8", errors="replace"))
                    except ValueError as error:
                        raise CommandError(
                            "COMMAND_FAILED", "C执行器响应不是合法JSON", effect
                        ) from error
                    if (
                        not isinstance(result, dict)
                        or type(result.get("ok")) is not bool
                        or not isinstance(result.get("output"), str)
                        or type(result.get("exit_code")) is not int
                        or type(result.get("signal")) is not int
                        or not isinstance(result.get("code"), str)
                    ):
                        raise CommandError("COMMAND_FAILED", "C执行器响应字段非法", effect)
                    if not result["ok"]:
                        code = result["code"]
                        if code not in COMMAND_ERRORS:
                            raise CommandError("COMMAND_FAILED", "C执行器返回未知错误码", effect)
                        outcome = result.get("effect", "none")
                        if outcome not in ("none", "committed", "unknown"):
                            outcome = "unknown"
                        message = result.get("message") or COMMAND_ERRORS[code]
                        message += f"（退出码{result['exit_code']}，信号{result['signal']}）"
                        raise CommandError(code, message, outcome)
                    if result["code"] or result["exit_code"] != 0 or result["signal"] != 0:
                        raise CommandError("COMMAND_FAILED", "C执行器成功状态与退出信息矛盾")
                    outcome = result.get("effect", "none")
                    if outcome not in ("none", "committed", "unknown") or (
                        mutating and outcome != "committed"
                    ):
                        raise CommandError("COMMAND_FAILED", "C执行器实际影响字段非法", effect)
                    return {"text": result["output"], "effect": outcome}
                finally:
                    try:
                        if process.returncode is None:
                            await self._stop(process)
                    finally:
                        self.processes.discard(process)
        finally:
            self.tasks.discard(task)

    async def close(self):
        self.closed = True
        tasks = tuple(self.tasks)
        for task in tasks:
            task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                raise result
