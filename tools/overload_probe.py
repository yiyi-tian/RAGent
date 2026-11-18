"""独立进程运行过载客户端，避免检索计算占用客户端事件循环。"""

import asyncio
import json
import sys
import time

from tools.performance_common import HEADER, connect_socket, percentile, socket_exchange


async def probe(settings):
    port = settings["port"]
    count = settings["count"]
    rows = []
    connections = []
    driver = settings.get("client_io", "stream")

    async def request(identity):
        writer = None
        connection = None
        started = time.monotonic()
        try:
            if driver == "socket":
                if settings.get("preconnect"):
                    connection = connections[identity - 1]
                else:
                    connection = await connect_socket(port)
                connected = time.monotonic()
                sent = time.monotonic()
                kind, payload = await socket_exchange(
                    connection, 2, identity, {"query": settings["query"]}
                )
                if kind != 101:
                    raise ValueError("过载探测收到非法响应类型")
            else:
                if settings.get("preconnect"):
                    reader, writer = connections[identity - 1]
                else:
                    reader, writer = await asyncio.open_connection("127.0.0.1", port)
                connected = time.monotonic()
                body = json.dumps({"query": settings["query"]}).encode()
                writer.write(HEADER.pack(0x52414754, 2, 2, len(body), identity) + body)
                await writer.drain()
                sent = time.monotonic()
                header = HEADER.unpack(await reader.readexactly(20))
                if (
                    header[:3] != (0x52414754, 2, 101)
                    or header[3] > 1048576
                    or header[4] != identity
                ):
                    raise ValueError("过载探测收到非法响应")
                payload = json.loads(await reader.readexactly(header[3]))
            rows.append(
                {
                    "code": payload["code"],
                    "seconds": time.monotonic() - started,
                    "connect_s": connected - started,
                    "reply_s": time.monotonic() - sent,
                }
            )
        except (OSError, ValueError, asyncio.IncompleteReadError) as error:
            rows.append({"code": type(error).__name__, "seconds": time.monotonic() - started})
        finally:
            if connection is not None:
                connection.close()
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass

    tasks = []
    try:
        if settings.get("preconnect"):
            for _ in range(count):
                if driver == "socket":
                    connections.append(await connect_socket(port))
                else:
                    connections.append(await asyncio.open_connection("127.0.0.1", port))
        tasks = [asyncio.create_task(request(i)) for i in range(1, count + 1)]
        await asyncio.sleep(4)
        started = time.monotonic()
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write(HEADER.pack(0x52414754, 2, 200, 2, 999999) + b"{}")
            await writer.drain()
            raw = await asyncio.wait_for(reader.readexactly(20), 3)
            header = HEADER.unpack(raw)
            if header != (0x52414754, 2, 201, 2, 999999):
                raise ValueError("PING响应头错误")
            if await reader.readexactly(2) != b"{}":
                raise ValueError("PONG载荷错误")
            ping_seconds = time.monotonic() - started
        finally:
            writer.close()
            await writer.wait_closed()
        rejected = [r["seconds"] for r in rows if r["code"] == "SERVER_BUSY"]
        return {
            "clients": count,
            "probe": "overload",
            "client_process": "separate",
            "client_io": driver,
            "preconnected": bool(settings.get("preconnect")),
            "ping_ok": True,
            "ping_seconds": ping_seconds,
            "reject_p95_s": percentile(rejected, 0.95),
            "reject_connect_p95_s": percentile(
                [r["connect_s"] for r in rows if r["code"] == "SERVER_BUSY"], 0.95
            ),
            "reject_reply_p95_s": percentile(
                [r["reply_s"] for r in rows if r["code"] == "SERVER_BUSY"], 0.95
            ),
            "rejected": len(rejected),
            "other_errors": [r for r in rows if r["code"] != "SERVER_BUSY"],
            "abandoned_after_probe": count - len(rows),
        }
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if driver == "socket":
            for connection in connections:
                connection.close()
        else:
            for _, writer in connections:
                writer.close()
            await asyncio.gather(*(w.wait_closed() for _, w in connections), return_exceptions=True)


if __name__ == "__main__":
    print(json.dumps(asyncio.run(probe(json.load(sys.stdin)))))
