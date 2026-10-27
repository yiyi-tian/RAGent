"""固定响应网络基准；用相同参数比较版本，不代表模型端到端性能。"""
import argparse
import concurrent.futures
import json
import os
import socket
import struct
import subprocess
import tempfile
import time

HEADER = struct.Struct("!IHHIQ")
BODY = b'{"query":"hello"}'


def exact(sock, count):
    data = bytearray()
    while len(data) < count:
        chunk = sock.recv(count - len(data))
        if not chunk:
            raise RuntimeError("连接提前关闭")
        data.extend(chunk)
    return data


def worker(port, requests):
    latencies = []
    errors = 0
    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        sock.settimeout(10)
        for request_id in range(1, requests + 1):
            payload = HEADER.pack(0x52414754, 1, 1, len(BODY), request_id) + BODY
            start = time.monotonic()
            sock.sendall(payload)
            magic, version, kind, length, returned_id = HEADER.unpack(exact(sock, HEADER.size))
            body = json.loads(exact(sock, length))
            if magic != 0x52414754 or version != 1 or returned_id != request_id or kind != 100:
                errors += 1
            latencies.append((time.monotonic() - start) * 1000)
    return latencies, errors


def cpu_ticks(pid):
    with open(f"/proc/{pid}/stat", encoding="utf-8") as stat:
        fields = stat.read().rsplit(")", 1)[1].split()
    return int(fields[11]) + int(fields[12])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("server")
    parser.add_argument("--clients", type=int, default=16)
    parser.add_argument("--requests", type=int, default=100)
    args = parser.parse_args()
    if args.clients < 1 or args.requests < 1:
        parser.error("clients和requests必须大于0")
    with tempfile.TemporaryFile() as log:
        server = subprocess.Popen([args.server, "--port", "0"], stdout=subprocess.PIPE, stderr=log, text=True)
        try:
            line = server.stdout.readline()
            if not line.startswith("Listening on "):
                raise RuntimeError("服务器启动失败")
            port = int(line.strip().rsplit(":", 1)[1])
            ticks_before = cpu_ticks(server.pid)
            start = time.monotonic()
            with concurrent.futures.ThreadPoolExecutor(max_workers=args.clients) as executor:
                results = list(executor.map(lambda _: worker(port, args.requests), range(args.clients)))
            elapsed = time.monotonic() - start
            ticks = cpu_ticks(server.pid) - ticks_before
            latencies = sorted(value for values, _ in results for value in values)
            percentile = lambda p: latencies[min(len(latencies) - 1, int((len(latencies) - 1) * p))]
            print(f"clients={args.clients} requests={len(latencies)} errors={sum(e for _, e in results)} "
                  f"throughput={len(latencies) / elapsed:.1f}/s p50={percentile(.5):.2f}ms "
                  f"p95={percentile(.95):.2f}ms p99={percentile(.99):.2f}ms "
                  f"server_cpu={100 * ticks / os.sysconf('SC_CLK_TCK') / elapsed:.1f}%")
        finally:
            server.terminate()
            server.wait(timeout=10)
            server.stdout.close()


if __name__ == "__main__":
    main()
