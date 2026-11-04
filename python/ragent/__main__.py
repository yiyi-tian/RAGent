"""内部 HTTP 入口。"""

import argparse
import logging
import math

from aiohttp import web
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
        try:
            result = await answer(
                data["query"], data["mode"], retriever=retriever, model=model, timeout=timeout
            )
        except (ValueError, KeyError, TypeError):
            return web.json_response(
                {"code": "INVALID_REQUEST", "message": "请求字段非法"}, status=400
            )
        except ModelError as error:
            logging.getLogger(__name__).warning("模型请求失败: %s", error.code)
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
        return web.json_response(result)

    app = web.Application(client_max_size=1024 * 1024)
    app.router.add_post("/agent", handle)
    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9001)
    parser.add_argument("--retrieval", choices=("mock", "qdrant"), default="mock")
    parser.add_argument("--model", choices=("mock", "deepseek"), default="mock")
    parser.add_argument("--request-timeout", type=float, default=120)
    args = parser.parse_args()
    if not math.isfinite(args.request_timeout) or args.request_timeout <= 0:
        parser.error("请求总时限必须是有限正数")
    retriever = create_retriever() if args.retrieval == "qdrant" else None
    model = DeepSeekModel() if args.model == "deepseek" else None
    web.run_app(create_app(retriever, model, args.request_timeout), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
