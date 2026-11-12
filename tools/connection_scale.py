"""验证真实 C 服务的千连接容量和 PING 延迟，不调用云端。"""

import argparse
import asyncio
import json
from pathlib import Path
import resource
import time

from tools.performance_common import HEADER, connect_socket, percentile, socket_exchange


async def measure(output="artifacts/performance/target-idle1000.json", driver="stream"):
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if hard < 4096:
        raise ValueError("测试需要至少4096的描述符硬限制")
    resource.setrlimit(resource.RLIMIT_NOFILE, (max(soft, 4096), hard))
    process = await asyncio.create_subprocess_exec(
        "build/agent-server",
        "--port",
        "0",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    connections = []
    try:
        assert process.stdout is not None
        line = await asyncio.wait_for(process.stdout.readline(), 30)
        port = int(line.rsplit(b":", 1)[1])
        if driver == "native":
            child = await asyncio.create_subprocess_exec(
                "build/socket-probe",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
            )
            try:
                settings = {"port": port, "count": 1000, "mode": "ping"}
                output_bytes, _ = await asyncio.wait_for(
                    child.communicate(json.dumps(settings).encode()), 30
                )
                if child.returncode != 0:
                    raise RuntimeError("C负载客户端失败")
                report = json.loads(output_bytes)
                target = Path(output)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps(report, indent=2) + "\n")
                print(json.dumps(report))
                return
            finally:
                if child.returncode is None:
                    child.terminate()
                    await child.wait()
        # 逐个保存所有权，连接中途失败时也能回收已经建立的连接。
        for _ in range(1000):
            if driver == "socket":
                connections.append(await connect_socket(port))
            else:
                connections.append(await asyncio.open_connection("127.0.0.1", port))
        await asyncio.sleep(2)

        async def ping(pair, identity):
            started = time.monotonic()
            if driver == "socket":
                async with asyncio.timeout(3):
                    kind, payload = await socket_exchange(pair, 200, identity, {})
                if kind != 201 or payload != {}:
                    raise ValueError("PONG响应错误")
                return time.monotonic() - started
            reader, writer = pair
            writer.write(HEADER.pack(0x52414754, 1, 200, 2, identity) + b"{}")
            await writer.drain()
            raw = await asyncio.wait_for(reader.readexactly(20), 3)
            magic, version, kind, length, returned = HEADER.unpack(raw)
            if (magic, version, kind, length, returned) != (0x52414754, 1, 201, 2, identity):
                raise ValueError("PING响应帧不匹配")
            if await reader.readexactly(length) != b"{}":
                raise ValueError("PONG载荷错误")
            return time.monotonic() - started

        idle_durations = []
        for connection in connections[:100]:
            idle_durations.append(await ping(connection, 1))
        durations = []
        for identity in range(2, 5):
            durations.extend(await asyncio.gather(*(ping(c, identity) for c in connections)))
        report = {
            "connections": len(connections),
            "client_io": driver,
            "ping_success": len(durations),
            "idle_ping_samples": len(idle_durations),
            "idle_ping_p95_s": percentile(idle_durations, 0.95),
            "ping_p95_s": percentile(durations, 0.95),
        }
        target = Path(output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report))
    finally:
        if driver == "socket":
            for connection in connections:
                connection.close()
        else:
            for _, writer in connections:
                writer.close()
            await asyncio.gather(*(w.wait_closed() for _, w in connections), return_exceptions=True)
        if process.returncode is None:
            process.terminate()
            await process.wait()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="artifacts/performance/target-idle1000.json")
    parser.add_argument("--client-io", choices=("stream", "socket", "native"), default="stream")
    args = parser.parse_args()
    asyncio.run(measure(args.output, args.client_io))
