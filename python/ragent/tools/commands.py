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
}


class CommandError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


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
            await asyncio.wait_for(process.communicate(), 1)
        except TimeoutError:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.communicate()

    async def ainvoke(self, target: str) -> dict:
        if self.closed:
            raise CommandError("COMMAND_FAILED", "C执行器已关闭")
        task = asyncio.current_task()
        assert task is not None
        self.tasks.add(task)
        try:
            async with self.slots:
                timeout_ms = max(1, int(remaining_timeout(3) * 1000))
                request = json.dumps({"target": target, "timeout_ms": timeout_ms}).encode()
                try:
                    process = await asyncio.create_subprocess_exec(
                        self.path,
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL,
                        start_new_session=True,
                        env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
                    )
                except OSError as error:
                    raise CommandError("COMMAND_FAILED", "无法启动C执行器") from error
                self.processes.add(process)
                try:
                    try:
                        raw, _ = await asyncio.wait_for(
                            process.communicate(request), timeout_ms / 1000 + 0.5
                        )
                    except TimeoutError as error:
                        raise CommandError("COMMAND_TIMEOUT", "C执行器未在时限内返回") from error
                    if process.returncode != 0 or len(raw) > 128 * 1024:
                        raise CommandError("COMMAND_FAILED", "C执行器异常退出或响应过大")
                    try:
                        # Linux进程名可能含非UTF-8字节，对展示文本使用替换字符。
                        result = json.loads(raw.decode("utf-8", errors="replace"))
                    except ValueError as error:
                        raise CommandError("COMMAND_FAILED", "C执行器响应不是合法JSON") from error
                    if (
                        not isinstance(result, dict)
                        or type(result.get("ok")) is not bool
                        or not isinstance(result.get("output"), str)
                        or type(result.get("exit_code")) is not int
                        or type(result.get("signal")) is not int
                        or not isinstance(result.get("code"), str)
                    ):
                        raise CommandError("COMMAND_FAILED", "C执行器响应字段非法")
                    if not result["ok"]:
                        code = result["code"]
                        if code not in COMMAND_ERRORS:
                            raise CommandError("COMMAND_FAILED", "C执行器返回未知错误码")
                        raise CommandError(code, COMMAND_ERRORS[code])
                    if result["code"] or result["exit_code"] != 0 or result["signal"] != 0:
                        raise CommandError("COMMAND_FAILED", "C执行器成功状态与退出信息矛盾")
                    return {"text": result["output"], "summary": f"已完成只读诊断：{target}"}
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
