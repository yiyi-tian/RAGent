"""命令行入口：单次问答、交互问答和 TCP 服务。"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from dotenv import load_dotenv

from ragent.config import Settings
from ragent.errors import AgentError, protocol_error_for
from ragent.history import ConversationHistory
from ragent.model import create_chat_model
from ragent.qa import QAEngine
from ragent.server import PythonAgentServer


def _build_engine(settings: Settings) -> QAEngine:
    return QAEngine(
        create_chat_model(settings),
        max_input_chars=settings.max_input_chars,
    )


def _safe_error_text(exception: Exception) -> str:
    return protocol_error_for(exception).message


def _ask(engine: QAEngine, question: str) -> str:
    return engine.answer(question)


def _run_chat(engine: QAEngine, settings: Settings) -> None:
    history = ConversationHistory(settings.history_turns)
    print("RAGent 交互问答。输入 /help 查看命令，输入 /quit 退出。")
    while True:
        try:
            question = input("\n你: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return

        if not question:
            continue
        if question in {"/quit", "/exit"}:
            return
        if question == "/clear":
            history.clear()
            print("历史记录已清空。")
            continue
        if question == "/help":
            print("/clear 清空历史，/quit 或 /exit 退出。")
            continue

        try:
            answer = engine.answer(question, history.messages())
        except Exception as exc:
            print(f"错误: {_safe_error_text(exc)}", file=sys.stderr)
            continue
        history.append(question, answer)
        print(f"RAGent: {answer}")


def _read_stdin_query() -> str:
    if sys.stdin.isatty():
        raise AgentError("INVALID_REQUEST", "请提供问题，或改用 `ragent chat`。")
    query = sys.stdin.read()
    if not query.strip():
        raise AgentError("INVALID_REQUEST", "从标准输入读取到的问题为空。")
    return query


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ragent",
        description="RAGent Python LangChain 问答 Agent",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    ask_parser = subparsers.add_parser("ask", help="回答一个问题")
    ask_parser.add_argument("question", nargs="?", help="问题文本；省略时从标准输入读取")

    subparsers.add_parser("chat", help="进入本地交互问答")
    subparsers.add_parser("serve", help="启动兼容现有 TCP 协议的 Agent 服务")
    return parser


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = _build_parser()
    arguments = parser.parse_args(argv)

    try:
        settings = Settings.from_env()
        engine = _build_engine(settings)
        if arguments.command == "ask":
            question = arguments.question if arguments.question else _read_stdin_query()
            print(_ask(engine, question))
            return 0
        if arguments.command == "chat":
            _run_chat(engine, settings)
            return 0

        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )
        server = PythonAgentServer(settings, engine.answer)
        asyncio.run(server.serve_forever())
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(f"错误: {_safe_error_text(exc)}", file=sys.stderr)
        return 1
