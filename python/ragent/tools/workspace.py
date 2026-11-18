"""连接上下文和rootless沙箱适配；Docker管理及文件权限由C组件落实。"""

import asyncio
from dataclasses import dataclass
import json
import os
from pathlib import Path
import uuid

from .commands import CommandError, ReadOnlyExecutor

WORKSPACE_TOOLS = (
    "workspace_list",
    "workspace_read",
    "workspace_write",
    "workspace_mkdir",
    "workspace_copy",
    "workspace_move",
    "workspace_remove",
)
MUTATIONS = frozenset(WORKSPACE_TOOLS[2:])


@dataclass(frozen=True)
class SessionContext:
    server_id: str
    connection_id: str
    request_id: str = ""


class SandboxExecutor(ReadOnlyExecutor):
    def __init__(self, path: str, manager: str, socket: str, state: str, concurrency: int = 4):
        super().__init__(path, concurrency)
        executable = Path(manager).resolve()
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise ValueError("C沙箱管理组件不可用，请先构建")
        self.manager = str(executable)
        self.socket = str(Path(socket).resolve())
        self.state = str(Path(state).resolve())
        Path(self.state).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.control_slots = asyncio.Semaphore(1)

    async def manage(self, payload: dict, timeout: float = 8) -> dict:
        async with self.control_slots:
            return await self.run_request(
                payload,
                timeout,
                self.manager,
                ("--socket", self.socket, "--state", self.state),
                control=True,
            )

    async def probe(self):
        await self.manage({"operation": "probe"})

    async def sync(self, snapshot: dict):
        await self.manage({"operation": "sync", "snapshot": snapshot})

    async def invoke_file(self, tool: str, args: dict, session: SessionContext) -> dict:
        if tool not in WORKSPACE_TOOLS:
            raise CommandError("COMMAND_NOT_ALLOWED", "未知文件工具")
        payload = {
            "operation": "invoke",
            "session": {"server_id": session.server_id, "connection_id": session.connection_id},
            "tool": tool,
            "args": args,
            "operation_id": uuid.uuid4().hex,
            "request_id": session.request_id,
        }
        result = await self.run_request(
            payload,
            8,
            self.manager,
            ("--socket", self.socket, "--state", self.state),
            mutating=tool in MUTATIONS,
        )
        result["summary"] = f"文件操作完成：{tool}；实际影响：{result['effect']}"
        return result


async def create_executor(mode: str, path: str, concurrency: int = 4):
    if mode in ("mock", "client"):
        return None
    if mode == "local":
        return ReadOnlyExecutor(path, concurrency)
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
    executor = SandboxExecutor(
        path,
        os.environ.get(
            "RAGENT_SANDBOX_EXECUTOR", str(Path(path).resolve().with_name("ragent-sandbox"))
        ),
        os.environ.get("RAGENT_SANDBOX_SOCKET", str(runtime / "ragent-sandbox-docker.sock")),
        os.environ.get("RAGENT_SANDBOX_STATE", str(runtime / "ragent-sandbox/state.json")),
        concurrency,
    )
    await executor.probe()
    return executor
