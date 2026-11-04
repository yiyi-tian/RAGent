"""每请求独立的 Mock 工具循环，不执行系统命令。"""

import asyncio
import logging
from typing import TYPE_CHECKING

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from .errors import ModelError

if TYPE_CHECKING:
    from .model import Model

LOGGER = logging.getLogger(__name__)


@tool
def search_linux_manual(query: str) -> dict:
    """检索模拟 Linux 手册。"""
    return {
        "text": "模拟手册：epoll 用于 I/O 事件通知。",
        "sources": [
            {"source": "mock-manual", "chunk_id": "mock-1", "text": "模拟证据", "score": 1.0}
        ],
    }


@tool
def diagnose(target: str) -> dict:
    """返回模拟诊断结果，target 只能是 memory。"""
    if target != "memory":
        raise ValueError("target 只支持 memory")
    return {"text": "模拟内存诊断：未读取宿主机。"}


TOOLS = {t.name: t for t in (search_linux_manual, diagnose)}


class RetrievalError(Exception):
    """必需检索失败。"""


class MockModel:
    async def ainvoke(self, messages, allowed):
        tool_results = []
        for message in messages:
            if isinstance(message, ToolMessage):
                tool_results.append(message.content)
        if tool_results:
            return AIMessage(content="模拟回答：" + "；".join(tool_results))

        calls = []
        for index, name in enumerate(allowed):
            if name == "search_linux_manual":
                args = {"query": messages[0].content}
            else:
                args = {"target": "memory"}
            call = {
                "name": name,
                "args": args,
                "id": str(index),
                "type": "tool_call",
            }
            calls.append(call)

        content = ""
        if not calls:
            questions = []
            for message in messages:
                if isinstance(message, HumanMessage):
                    questions.append(message.content)
            content = "模拟回答：" + "；".join(questions)
        return AIMessage(content=content, tool_calls=calls)


async def answer(
    query: str,
    mode: str,
    model: "Model | None" = None,
    timeout: float = 10,
    rounds: int = 4,
    retriever=None,
) -> dict:
    """执行独立请求，返回 text、sources 和 actions；输入非法时抛出 ValueError。"""
    if (
        mode not in ("chat", "rag", "cmd", "agent")
        or not isinstance(query, str)
        or not query.strip()
    ):
        raise ValueError("模式或 query 非法")
    allowed = {"chat": [], "rag": [], "cmd": ["diagnose"], "agent": list(TOOLS)}[mode]
    messages: list[BaseMessage] = [HumanMessage(content=query)]
    sources = []
    actions = []
    selected_model = model or MockModel()

    async def execute(name, args):
        try:
            tool = TOOLS[name]
            if set(args) != set(tool.args) or any(not isinstance(v, str) for v in args.values()):
                raise ValueError("工具参数非法")
            if name == "search_linux_manual" and retriever is not None:
                result = await retriever(args["query"])
            else:
                result = await tool.ainvoke(args)
            sources.extend(result.get("sources", []))
            actions.append({"tool": name, "status": "success", "summary": result["text"]})
            return result["text"]
        except Exception:
            # 工具任务独立失败；保留堆栈便于定位，不向客户端暴露内部异常。
            LOGGER.exception("工具调用失败: %s", name)
            actions.append({"tool": name, "status": "failed", "summary": "工具调用失败"})
            return "工具调用失败"

    try:
        async with asyncio.timeout(timeout):
            if mode == "rag":
                result = await execute("search_linux_manual", {"query": query})
                if actions[-1]["status"] == "failed":
                    raise RetrievalError("检索服务不可用")
                messages.append(HumanMessage(content="检索结果：" + result))
            for _ in range(rounds):
                response = await selected_model.ainvoke(messages, allowed)
                messages.append(response)
                if not response.tool_calls:
                    return {"text": response.content, "sources": sources, "actions": actions}
                if len(response.tool_calls) > 4:
                    break
                ids = [c["id"] for c in response.tool_calls]
                if len(set(ids)) != len(ids):
                    break

                async def invoke(call):
                    if call["name"] not in allowed:
                        actions.append(
                            {"tool": call["name"], "status": "failed", "summary": "工具未授权"}
                        )
                        return "工具未授权"
                    return await execute(call["name"], call["args"])

                results = await asyncio.gather(*(invoke(c) for c in response.tool_calls))
                for call, result in zip(response.tool_calls, results):
                    messages.append(ToolMessage(content=result, tool_call_id=call["id"]))
    except TimeoutError:
        if not any(action["status"] == "success" for action in actions):
            raise ModelError("UPSTREAM_TIMEOUT", "请求总时限已到")
        return {"text": "请求超时；已有操作见摘要。", "sources": sources, "actions": actions}
    return {"text": "达到工具调用限制；已有操作见摘要。", "sources": sources, "actions": actions}
