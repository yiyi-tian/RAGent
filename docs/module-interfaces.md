# RAGent 模块接口

本文描述当前网络、Agent、模型与检索接口。真实命令执行器与沙箱的接口尚待确定。
TCP 帧格式以[协议规范](protocol.md)为准。

## C 网络与资源所有权

`Frame` 包含类型、request_id、body 和 body_length；所有者通过 `frame_destroy`
释放 body。编码函数返回独立帧缓冲，由调用方释放。`FrameDecoder` 持有累计接收缓冲。

网络线程拥有 Socket、连接状态和发送缓冲，处理 accept、帧解码、PING、部分写入。
工作任务持有完整请求数据和稳定 connection_id，不借用网络缓冲，也不写客户端 Socket。
完成结果通过有界队列和 eventfd 回到网络线程；断连结果丢弃，队列满返回 SERVER_BUSY。

HTTP 配置在线程启动前复制，调用期间不变。C 工作线程使用 libcurl 调用 Agent，
限制响应为 1 MiB，并检查 HTTP 状态、JSON 字段及错误码。返回的 Frame 仍由调用方释放。
未配置 agent_url 时使用固定响应处理器。

## 内部 HTTP 接口

Python 默认监听 `127.0.0.1:9001`，提供 `POST /agent`：

```json
{"request_id":"1","mode":"rag","query":"如何列出隐藏文件？"}
```

request_id 使用字符串避免 64 位整数精度问题；mode 为 chat、rag、cmd 或 agent。
成功返回 `text` 字符串、`sources` 数组和 `actions` 数组。来源形状见 TCP 协议；
actions 每项包含 `tool`、`status`、`summary`，status 为 success 或 failed。
失败返回 `code`、`message`，C 负责映射为 TCP ERROR 并保留原 request_id。

非法请求使用 INVALID_REQUEST；模型限流使用 SERVER_BUSY；模型超时使用
UPSTREAM_TIMEOUT；模型连接、认证、非法响应及必需检索失败使用 UPSTREAM_UNAVAILABLE。
未预期异常使用 INTERNAL_ERROR。外部服务错误正文不直接返回客户端。

## Agent 与工具

每请求创建独立消息上下文，保存原始问题、模型实际返回的 AIMessage、工具调用 ID
及对应 ToolMessage。工具执行前校验工具名、参数和模式权限，独立调用并行执行。
工具失败保留失败摘要与成功结果，不伪造调用历史。

| 模式 | 工具策略 |
|---|---|
| CHAT | 无工具 |
| RAG | 编排层强制检索后提供证据，不插入伪造的模型工具调用 |
| CMD | 仅模拟 diagnose，target 为 memory |
| AGENT | search_linux_manual 与模拟 diagnose |

每请求最多 4 次模型调用，每轮最多 4 个工具。服务总时限默认 120 秒，可通过
`--request-timeout` 设置；无成功结果时总超时返回错误，有成功工具结果时返回未完成摘要。
达到调用限制返回限制说明及已有摘要。请求结束不保留跨请求会话。

## 模型与检索

`--model mock|deepseek` 选择模型，默认 mock。DeepSeek 适配器使用 LangChain
ChatOpenAI，按模式绑定工具，采用非流式、非思考模式，不自动重试，并发上限为 4。
环境变量见 `.env.example`；启动时由 shell 导出。配置缺失或非法时启动失败。
空回答、非法工具参数与输出截断形成明确错误。

`--retrieval mock|qdrant` 选择检索，默认 mock。在线适配器调用
`rag.search.search_linux_manual(query, embedding, store, limit=5)`，在工作线程执行同步
Embedding 与 Qdrant HTTP 请求，避免阻塞事件循环。来源仅投影为协议字段，不泄露内部路径。
空结果提供未找到资料的说明。取消异步请求不会强行终止已开始的同步线程请求。

离线入库不使用 LangChain，不参与在线请求处理，负责抽取、分块、来源元数据和向量写入。
集合一致性、重复导入和快照操作见[知识库说明](rag.md)。

## 配置与待实现边界

`config/agent.json` 配置网络、线程池和 Python HTTP 地址，HTTP 时限为 125 秒。
模型和检索方式由 Python 启动参数选择。真实入口客户端需使用 `--timeout-ms 130000`。
模型单次超时与 Token 上限由环境提供。不带配置文件启动 C 服务时使用固定响应。

真实命令执行器尚未实现；其权限、结构化输入、超时、输出限制和子进程回收需在实现前确定。
自动写操作必须限制在隔离工作区，隔离不可用时禁用，不向 Agent 暴露 Docker 管理套接字。
