# CMD 命令工具开发方向

目标是让 CMD 能执行真实操作并解释结果，同时让 AGENT 复用这些工具。
命令提案使用 `run_command(command, cwd)`；执行与授权边界见
[命令执行说明](command-policy.md)，线上消息见[TCP协议](protocol.md)。

## 职责与目录

| 部分 | 职责 | 建议目录 |
|---|---|---|
| Python 工具 | LangChain工具声明、提案参数校验与结果续行 | `python/ragent/tools/` |
| C 执行器 | 权限落实、实际执行、超时与输出限制、子进程回收 | `src/executor/` |
| C 公共接口 | 输入输出、所有权与失败方式声明 | `include/executor/` |
| 测试 | 执行器及 Agent 闭环验证 | `tests/executor/`、`tests/agent/` |

现有 C 网络服务继续负责 TCP 接入与 HTTP 转发，不在 epoll 线程执行命令。
根目录 `tools/` 用于开发、运维和评测脚本。

## 扩展原则

优先补充可证明只读的命令及参数，同时在C边界增加有意义的正反例。
写入和向外行动继续走统一容器与逐条确认，不为某个工具另开宿主机执行入口。
模型工具只表达意图；解析、授权、挂载、网络和资源限制由客户端执行层统一落实。
