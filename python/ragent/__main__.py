"""内部 HTTP 入口。"""

import argparse
import asyncio
import logging
import json
from functools import partial
import math
import os

from aiohttp import web
import uvloop
from rag.services import ServiceError
from .agent import RetrievalError, answer
from .errors import ModelError
from .model import DeepSeekModel
from .retrieval import create_retriever


def create_app(retriever=None, model=None, timeout=10):
    async def handle(request):
        try:
            data = await request.json()
        except ValueError:
            return web.json_response(
                {"code": "INVALID_REQUEST", "message": "请求 JSON 非法"}, status=400
            )
        if not isinstance(data, dict) or not isinstance(data.get("request_id"), str):
            return web.json_response(
                {"code": "INVALID_REQUEST", "message": "请求字段非法"}, status=400
            )
        remaining_ms = data.get("timeout_ms")
        if remaining_ms is not None and (
            type(remaining_ms) is not int or not 1 <= remaining_ms <= 600000
        ):
            return web.json_response(
                {"code": "INVALID_REQUEST", "message": "timeout_ms 必须为 1 至 600000 的整数"},
                status=400,
            )
        request_timeout = timeout if remaining_ms is None else min(timeout, remaining_ms / 1000)
        try:
            result = await answer(
                data["query"],
                data["mode"],
                retriever=retriever,
                model=model,
                timeout=request_timeout,
            )
        except (ValueError, KeyError, TypeError):
            return web.json_response(
                {"code": "INVALID_REQUEST", "message": "请求字段非法"}, status=400
            )
        except ModelError as error:
            cause = error.__cause__
            logging.getLogger(__name__).warning(
                "模型请求失败: code=%s cause=%s status=%s",
                error.code,
                type(cause).__name__,
                getattr(cause, "status_code", None),
            )
            return web.json_response({"code": error.code, "message": str(error)}, status=502)
        except RetrievalError:
            return web.json_response(
                {"code": "UPSTREAM_UNAVAILABLE", "message": "检索服务不可用"}, status=502
            )
        except Exception:
            logging.getLogger(__name__).exception("Agent 请求处理失败")
            return web.json_response(
                {"code": "INTERNAL_ERROR", "message": "Agent 处理失败"}, status=500
            )
        return web.json_response(result, dumps=partial(json.dumps, ensure_ascii=False))

    async def health(request):
        return web.json_response({"alive": True})

    async def ready(request):
        status = {
            "ready": True,
            "model": "mock" if model is None else "configured",
            "cloud_checked": False,
            "retrieval": "mock" if retriever is None else "qdrant",
        }
        if retriever is not None:
            check = getattr(retriever, "check_ready", None)
            if check is None:
                status["ready"] = False
                status["retrieval"] = "not_checked"
            else:
                try:
                    await asyncio.wait_for(check(), 2.5)
                except (ServiceError, TimeoutError) as error:
                    logging.getLogger(__name__).warning("检索就绪检查失败: %s", error)
                    status["ready"] = False
                    status["retrieval"] = "unavailable"
        return web.json_response(status, status=200 if status["ready"] else 503)

    app = web.Application(client_max_size=1024 * 1024)
    app.router.add_get("/health", health)
    app.router.add_get("/ready", ready)
    app.router.add_post("/agent", handle)
    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9001)
    parser.add_argument("--retrieval", choices=("mock", "qdrant"), default="mock")
    parser.add_argument("--model", choices=("mock", "deepseek"), default="mock")
    parser.add_argument("--request-timeout", type=float, default=120)
    parser.add_argument("--workers", type=int, default=os.environ.get("RAGENT_AGENT_WORKERS", "1"))
    args = parser.parse_args()
    if not math.isfinite(args.request_timeout) or args.request_timeout <= 0:
        parser.error("请求总时限必须是有限正数")
    if not 1 <= args.workers <= 4:
        parser.error("工作进程数必须为1至4")
    if args.workers > 1:
        from .workers import supervise

        options = {
            "workers": args.workers,
            "model": args.model,
            "retrieval": args.retrieval,
            "timeout": args.request_timeout,
            "model_concurrency": int(os.environ.get("RAGENT_MODEL_CONCURRENCY", "32")),
            "retrieval_workers": int(os.environ.get("RAGENT_RETRIEVAL_WORKERS", "8")),
        }
        supervise(args.host, args.port, options)
        return
    retriever = create_retriever() if args.retrieval == "qdrant" else None
    try:
        model = DeepSeekModel() if args.model == "deepseek" else None
    except Exception:
        if retriever is not None:
            uvloop.run(retriever.close())
        raise
    app = create_app(retriever, model, args.request_timeout)

    async def close_resources(app):
        try:
            if model is not None:
                await model.close()
        finally:
            if retriever is not None:
                await retriever.close()

    app.on_cleanup.append(close_resources)
    web.run_app(
        app,
        host=args.host,
        port=args.port,
        handler_cancellation=True,
        loop=uvloop.new_event_loop(),
    )


if __name__ == "__main__":
    main()
