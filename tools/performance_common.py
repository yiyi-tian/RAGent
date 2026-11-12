"""压测客户端共享的轻量协议头与分位数算法，不加载业务服务。"""

import asyncio
import json
import math
import socket
import struct

HEADER = struct.Struct(">IHHIQ")


def percentile(values, fraction):
    """按 nearest-rank 计算样本分位数。"""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


async def connect_socket(port):
    """调用者持有返回的非阻塞Socket，并负责关闭；连接失败或取消时内部回收。"""
    connection = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    connected = False
    try:
        connection.setblocking(False)
        connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        await asyncio.get_running_loop().sock_connect(connection, ("127.0.0.1", port))
        connected = True
        return connection
    finally:
        if not connected:
            connection.close()


async def socket_exchange(connection, kind, identity, payload):
    """单请求交换并校验帧边界；调用者保证同一Socket没有其他读取者。"""
    loop = asyncio.get_running_loop()
    body = b"{}" if payload == {} else json.dumps(payload, separators=(",", ":")).encode()
    await loop.sock_sendall(
        connection, HEADER.pack(0x52414754, 2, kind, len(body), identity) + body
    )

    async def receive_exact(length):
        data = bytearray()
        while len(data) < length:
            part = await loop.sock_recv(connection, length - len(data))
            if not part:
                raise ConnectionError("响应帧未完整接收，连接已断开")
            data.extend(part)
        return data

    magic, version, returned_kind, length, returned_id = HEADER.unpack(await receive_exact(20))
    if (magic, version, returned_id) != (0x52414754, 2, identity) or length > 1048576:
        raise ValueError("响应帧头非法")
    raw = await receive_exact(length)
    result = {} if raw == b"{}" else json.loads(raw)
    if not isinstance(result, dict):
        raise ValueError("响应载荷必须是JSON对象")
    return returned_kind, result
