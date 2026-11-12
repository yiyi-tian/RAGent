"""同一HTTP监听端口的固定工作进程；每个进程独立持有在线资源。"""

import asyncio
from contextlib import AsyncExitStack
import multiprocessing
import os
import signal
import socket
import time

from aiohttp import web
import uvloop


def split_limit(total, count, index):
    if not 1 <= count <= 4 or total < count:
        raise ValueError("工作进程须为1至4个，总并发不得少于进程数")
    quotient, remainder = divmod(total, count)
    return quotient + int(index < remainder)


def worker_entry(listener, stop, start, ready, listening, options, index, configure):
    os.environ["RAGENT_MODEL_CONCURRENCY"] = str(
        split_limit(options["model_concurrency"], options["workers"], index)
    )
    os.environ["RAGENT_RETRIEVAL_WORKERS"] = str(
        split_limit(options["retrieval_workers"], options["workers"], index)
    )
    uvloop.run(serve(listener, stop, start, ready, listening, options, configure, index))


async def serve(listener, stop, start, ready, listening, options, configure, index):
    from .__main__ import create_app
    from .model import DeepSeekModel
    from .retrieval import create_retriever
    from .tools.commands import ReadOnlyExecutor

    retriever = None
    model = None
    runner = None
    cleanup = None
    executor = None
    address = listener.getsockname()
    listener.close()
    listener = socket.socket()
    try:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        listener.bind(address)
        if options["retrieval"] == "qdrant":
            retriever = create_retriever()
        if options["model"] == "deepseek":
            model = DeepSeekModel()
        if options.get("commands", "mock") == "local":
            executor = ReadOnlyExecutor(
                options["executor_path"], split_limit(4, options["workers"], index)
            )
        app = create_app(retriever, model, options["timeout"], executor)
        if configure is not None:
            cleanup = configure(app, retriever, model)
        runner = web.AppRunner(app, access_log=None, handler_cancellation=True, shutdown_timeout=5)
        await runner.setup()
        ready.value = True
        # 所有进程完成资源初始化后才开始接收，避免部分就绪被当成整组就绪。
        while not start.value and not stop.value:
            await asyncio.sleep(0.05)
        if not stop.value:
            await web.SockSite(runner, listener).start()
            listening.value = True
            while not stop.value:
                await asyncio.sleep(0.1)
    finally:
        # 逆序回收所有已创建资源；某一项关闭失败也继续回收其余资源。
        async with AsyncExitStack() as resources:
            resources.callback(listener.close)
            if cleanup is not None:
                resources.callback(cleanup)
            for resource in (retriever, model, executor):
                if resource is not None:
                    resources.push_async_callback(resource.close)
            if runner is not None:
                resources.push_async_callback(runner.cleanup)


class WorkerGroup:
    def __init__(self, host, port, options, hooks=None):
        self.options = options
        if not 1 <= options["model_concurrency"] <= 256:
            raise ValueError("模型总并发必须为1至256")
        if not 1 <= options["retrieval_workers"] <= 64:
            raise ValueError("检索总线程必须为1至64")
        split_limit(options["model_concurrency"], options["workers"], 0)
        split_limit(options["retrieval_workers"], options["workers"], 0)
        self.context = multiprocessing.get_context("spawn")
        # 信号处理及进程强杀不能依赖跨进程Condition的唤醒确认。
        self.stop_event = self.context.RawValue("b", False)
        self.start_event = self.context.RawValue("b", False)
        self.processes = []
        self.listener = socket.socket()
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.listener.bind((host, port))
            # 先独占检查端口，再允许本组子进程绑定，避免加入已有监听服务。
            self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError:
            self.listener.close()
            raise
        self.port = self.listener.getsockname()[1]
        self.hooks = hooks or [None] * options["workers"]

    def start(self):
        ready = []
        listening = []
        try:
            for index in range(self.options["workers"]):
                event = self.context.RawValue("b", False)
                active = self.context.RawValue("b", False)
                process = self.context.Process(
                    target=worker_entry,
                    args=(
                        self.listener,
                        self.stop_event,
                        self.start_event,
                        event,
                        active,
                        self.options,
                        index,
                        self.hooks[index],
                    ),
                    name=f"ragent-worker-{index + 1}",
                )
                process.start()
                self.processes.append(process)
                ready.append(event)
                listening.append(active)
            deadline = time.monotonic() + 90
            while not all(event.value for event in ready):
                if self.stop_event.value:
                    raise InterruptedError("工作进程启动被取消")
                self.check()
                if time.monotonic() >= deadline:
                    raise TimeoutError("工作进程启动超时")
                time.sleep(0.05)
            self.check()
            self.listener.close()
            self.start_event.value = True
            deadline = time.monotonic() + 5
            while not all(flag.value for flag in listening):
                self.check()
                if time.monotonic() >= deadline:
                    raise TimeoutError("工作进程开始监听超时")
                time.sleep(0.01)
            return self.port
        except BaseException:
            self.stop()
            raise

    def check(self):
        for process in self.processes:
            if process.exitcode is not None:
                raise RuntimeError(f"工作进程 {process.pid} 提前退出，状态 {process.exitcode}")

    def stop(self):
        self.stop_event.value = True
        self.start_event.value = True
        deadline = time.monotonic() + 30
        forced = False
        try:
            for process in self.processes:
                process.join(max(0, deadline - time.monotonic()))
            for process in self.processes:
                if process.is_alive():
                    forced = True
                    process.terminate()
            for process in self.processes:
                process.join(3)
                if process.is_alive():
                    process.kill()
                    process.join()
        finally:
            self.listener.close()
        if forced:
            raise RuntimeError("工作进程未在清理时限内停止")
        failures = [p.exitcode for p in self.processes if p.exitcode != 0]
        if failures:
            raise RuntimeError(f"工作进程异常退出: {failures}")


def supervise(host, port, options):
    group = WorkerGroup(host, port, options)

    def stop(signum, frame):
        group.stop_event.value = True
        group.start_event.value = True

    previous = {}
    for signum in (signal.SIGINT, signal.SIGTERM):
        previous[signum] = signal.signal(signum, stop)
    try:
        group.start()
        print(f"Running on http://{host}:{group.port} workers={options['workers']}", flush=True)
        while not group.stop_event.value:
            group.check()
            time.sleep(0.2)
    finally:
        try:
            group.stop()
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)
