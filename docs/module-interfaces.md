# RAGent 模块接口

本文描述当前网络、Agent、模型与检索接口。诊断与沙箱管理通过独立C组件执行；文件操作只作用于连接临时工作区。
TCP 帧格式以[协议规范](protocol.md)为准。

## C 网络与资源所有权

`Frame` 包含类型、request_id、body 和 body_length；所有者通过 `frame_destroy`
释放 body。编码函数返回独立帧缓冲，由调用方释放。`FrameDecoder` 持有累计接收缓冲。

网络线程拥有 Socket、连接状态和发送缓冲，处理 accept、帧解码、PING、部分写入。
工作任务持有完整请求数据和稳定 connection_id，不借用网络缓冲，也不写客户端 Socket。
完成结果通过有界队列和 eventfd 回到网络线程；断连结果丢弃，队列满返回 SERVER_BUSY。
连接与已提交任务共同持有引用计数取消状态。连接销毁后，排队任务取出时直接释放，
运行中的 libcurl 请求通过进度回调中止；迟到结果仍按 connection_id 丢弃。

HTTP 配置在线程启动前复制，调用期间不变。C 工作线程使用 libcurl 调用 Agent，
限制响应为 1 MiB，并检查 HTTP 状态、JSON 字段及错误码。返回的 Frame 仍由调用方释放。
未配置 agent_url 时使用固定响应处理器。

## 内部 HTTP 接口

Python 默认监听 `127.0.0.1:9001`，提供 `POST /agent`：

HTTP服务使用uvloop事件循环；单进程与固定多进程入口采用相同的事件循环实现。

`--workers 1..4`（环境变量 `RAGENT_AGENT_WORKERS`，默认1）设置固定HTTP工作进程数。
各进程使用同一端口，独立持有模型客户端、检索线程池和只读手册索引。模型并发与检索线程
配置表示整组总量，启动时均分，余数按进程序号分配；不因增加进程而倍增上游并发。
整组资源初始化并开始监听后才报告启动完成；任一工作进程异常退出时停止整组，交由启动方
重新启动，不在请求中隐式重试。C网络线程与客户端TCP协议保持原有接口。

```json
{"request_id":"1","mode":"rag","query":"如何列出隐藏文件？"}
```

request_id 使用字符串避免 64 位整数精度问题；mode 为 chat、rag、cmd 或 agent。
成功返回 `text` 字符串、`sources` 数组和 `actions` 数组。来源形状见 TCP 协议；
actions 每项包含 `tool`、`status`、`summary`，status 为 success 或 failed。
失败返回 `code`、`message`，C 负责映射为 TCP ERROR 并保留原 request_id。
内部请求可携带 timeout_ms，类型为 1 至 600000 的整数，表示扣除 C 排队后的剩余毫秒数。
Python 取其与服务总时限的较小值；未提供时使用 Python 服务总时限。该字段不改客户端 TCP。

`GET /health` 仅表示 Python HTTP 服务存活；`GET /ready` 检查当前 Qdrant 集合身份、
向量维度和上下文索引是否可用，依赖失败返回 HTTP 503，恢复后可重新就绪。
两者均不调用云端模型或 Embedding，`cloud_checked=false`，不能证明云端余额和配额可用。

非法请求使用 INVALID_REQUEST；模型限流使用 SERVER_BUSY；模型超时使用
UPSTREAM_TIMEOUT；模型连接、认证、非法响应及必需检索失败使用 UPSTREAM_UNAVAILABLE。
未预期异常使用 INTERNAL_ERROR。外部服务错误正文不直接返回客户端。

## Agent 与工具

每请求创建独立消息上下文，保存原始问题、模型实际返回的 AIMessage、工具调用 ID
及对应 ToolMessage。工具执行前校验工具名、参数和模式权限，独立调用并行执行。
工具失败保留失败摘要与成功结果，不伪造调用历史。
AGENT 调用真实手册检索时，保留用户原问题；模型提供不同查询词时追加为检索重点，
避免将具体操作缩成一个命令名；合并超过检索长度上限时仅保留原问题。
RAG 和已调用检索的 AGENT 会加入只依据证据作答的
系统提示，证据不足时要求说明缺口；提示约束不代表模型一定遵守。CHAT 不受此约束。

| 模式 | 工具策略 |
|---|---|
| CHAT | 无工具 |
| RAG | 编排层强制检索后提供证据，不插入伪造的模型工具调用 |
| CMD | 按配置提供诊断与工作区工具，见下文 |
| AGENT | search_linux_manual 与diagnose |

每请求最多 4 次模型调用，每轮最多 4 个工具。服务总时限默认 120 秒，可通过
`--request-timeout` 设置；无成功结果时总超时返回错误，有成功工具结果时返回未完成摘要。
达到调用限制返回限制说明及已有摘要。请求结束不保留跨请求会话。

## 模型与检索

`--model mock|deepseek` 选择模型，默认 mock。DeepSeek 适配器使用 LangChain
ChatOpenAI，按模式绑定工具，采用非流式、非思考模式，不自动重试，默认并发上限为 32，可用 RAGENT_MODEL_CONCURRENCY 设置。
检索使用独立固定线程池，默认 8 个线程，可用 RAGENT_RETRIEVAL_WORKERS 设置；
创建者负责在服务退出时关闭检索器并回收线程。
环境变量见 `.env.example`；启动时由 shell 导出。配置缺失或非法时启动失败。
空回答、非法工具参数与输出截断形成明确错误。

`--retrieval mock|qdrant` 选择检索，默认 mock。在线适配器调用
`rag.search.search_linux_manual(query, embedding, store, limit=5)`，在工作线程执行同步
Embedding 与 Qdrant HTTP 请求，避免阻塞事件循环。来源仅投影为协议字段，不泄露内部路径。
模型证据逐条标注序号及手册来源，避免从正文猜测来源。
默认 `linux_manual_v4 + context` 在启动时加载只读索引，一次查询 Embedding 后，分别
查询段落和条目两种粒度各前 20 条，通过原文位置关联并融合排名，再补齐语境返回最多
5 条。chunk_id 为锚点，text 可包含同源相关块，score 为排名融合相关性分数。
单粒度集合的 context 策略仍使用余弦分数。更新集合后需重启 Python 服务；
vector 策略不加载该索引，直接执行单路向量检索。
空结果提供未找到资料的说明。请求截止时间与取消状态通过 Context 传播到检索线程；
每次 HTTP 调用使用剩余时限，分段读取及下一次调用前再次检查。已阻塞的同步调用
只能等待返回或 Socket 超时，不能强行终止线程；取消后不会继续后续云端调用。

离线入库不使用 LangChain，不参与在线请求处理，负责抽取、分块、来源元数据和向量写入。
集合一致性、重复导入和快照操作见[知识库说明](rag.md)。

## 配置与待实现边界

`config/agent.json` 配置网络、线程池和 Python HTTP 地址，agent_timeout_ms 为 125 秒，包含入队后的等待和 HTTP 执行。
模型和检索方式由 Python 启动参数选择。真实入口客户端需使用 `--timeout-ms 130000`。
模型单次超时与 Token 上限由环境提供。不带配置文件启动 C 服务时使用固定响应。

只读执行器接口见下节；命令由服务用户执行，不提升权限。
自动写操作必须限制在隔离工作区，隔离不可用时禁用，不向 Agent 暴露 Docker 管理套接字。

排队过期任务在工作线程取出时返回 UPSTREAM_TIMEOUT，不再发起后端调用。
C HTTP 使用剩余时限，Python 模型信号量等待与工具循环均计入 Python 请求时限。
完成队列与 Socket 发送仍受原有容量边界约束；此预算不承诺响应一定送达。
HTTP 调用方断开会取消 Python 请求。已提交云端的调用可能仍产生用量，取消不保证
撤销远端执行；写工具需单独落实取消与回收语义。

CLI在线服务持有的模型客户端、检索线程池与执行器在应用退出时关闭；模型初始化失败时回收已创建的检索器。
通过 `create_app` 注入的共享资源由调用者管理，应用不自动关闭。模型异步客户端关闭失败时，
仍释放同步客户端并保留原异常。

## 首批只读执行器接口

Python使用`asyncio.create_subprocess_exec`启动独立`ragent-executor`，每进程处理一次请求后退出。
stdin为不超过4095字节的UTF-8 JSON对象，写入后关闭；stdout为一个JSON结果；stderr不作为工具输出。
请求仅接受`{"target":"memory","timeout_ms":3000}`，timeout_ms为1至3000的整数，取请求剩余预算。
`target`支持memory、disk、processes、load；进程详情使用process_detail及正整数pid，
监听端口使用listeners及protocol=tcp|udp|all。程序与参数在C端固定，不接受任意命令。
没有任意路径、命令字符串、环境变量或Shell参数接口。业务不进入协议编解码模块。

结果为`ok`布尔值、`code`字符串、`output`字符串、`exit_code`整数和`signal`整数。
成功时code为空、exit_code与signal为0；错误码为既有COMMAND_NOT_ALLOWED、COMMAND_TIMEOUT、
COMMAND_OUTPUT_LIMIT或COMMAND_FAILED。失败输出不作为成功证据回填；Agent保留错误码及失败摘要，
其他工具成功结果仍可继续使用。模型实际tool_call_id与ToolMessage一一对应。

C执行器使用单调时钟、非阻塞管道和有界等待，输出最多16384字节，超限直接失败；不截断成成功。
命令独立进程组，超时和取消时杀死整组并waitpid回收直接子进程；子进程stdin为已关闭写端的管道，
只继承固定PATH和LC_ALL，不继承云端密钥。Python取消时通知C执行器清理，退出时回收活动任务。
全组命令最多4个并发，按固定工作进程数均分；等待名额也计入请求总时限。

`--commands mock|local|sandbox`默认mock；local模式启动时校验执行器可用，/ready也检查本机执行器文件，
不执行诊断或调用云端。`--executor`或RAGENT_EXECUTOR_PATH指定路径，默认build/ragent-executor。
通过create_app注入的共享执行器与模型、检索器一样由调用者关闭。

## 连接临时工作区接口

`sandbox`配置额外提供下列结构化LangChain工具；CMD和AGENT可调用，CHAT和RAG不提供。

| 工具 | 参数 | 行为 |
|---|---|---|
| workspace_list | path=".", offset=0 | 名称排序，每页32项，返回next_offset |
| workspace_read | path | 读取最多16 KiB UTF-8文本 |
| workspace_write | path, content, overwrite=false | 原子提交，默认拒绝覆盖 |
| workspace_mkdir | path | 创建目录，父目录须存在 |
| workspace_copy | source, destination | 复制普通文本文件，拒绝覆盖 |
| workspace_move | source, destination | 移动文件或目录，拒绝覆盖 |
| workspace_remove | path | 删除普通文件或空目录，禁止递归及根目录删除 |

路径按UTF-8字节限制为512、每段128、最多16段；拒绝绝对路径、`..`、控制字符、NUL、
符号链接及保留前缀`.ragent-`。C使用openat2的BENEATH和NO_SYMLINKS/NO_MAGICLINKS落实边界。
每工作区最多128项；读写不接受二进制/NUL，不执行文本或脚本。
同轮包含文件工具时按模型调用顺序执行；同一连接跨请求由C锁串行，其他连接可并行。
不自动重试写操作；同请求遇到unknown后停止后续写入，重复相同写调用也拒绝。

C前端使用随机实例ID及稳定连接ID覆盖外部session字段；公共TCP v1保持不变。
Python内部HTTP接收`session={server_id,connection_id}`，模型参数不含此身份。
`--sandbox-sessions`要求至少2个C工作线程，其中1个专门维护连接；仍属于固定线程总数。
该线程每秒将`server_id,pid,start,boot,sequence,through,active`快照发送到
`/internal/sessions/sync`；最多1个在途和1个最新待发快照。
through为已接受的最大连接ID，新连接可先于下一快照执行；旧sequence不覆盖新快照。
维护失联超过10秒暂停文件操作，不按心跳超时猜测删除仍活着的连接；PID、启动ticks和boot ID
确认前端死亡后可回收。正常断连后由快照清理，后台每2秒对账；无故障环境目标10秒内。
内部HTTP仅绑定本机回环地址，不应直接暴露给客户端。

Python启动独立`ragent-sandbox --socket PATH --state FILE`，每次stdin一个JSON请求，stdout一个结果。
操作为probe、sync或invoke；invoke携带上述session、tool、args及受信任request_id/operation_id。
结果沿用只读结果字段，新增effect=none|committed|unknown，表示未提交、已提交、结果未知。
执行中断或非法响应关闭工作区；同一连接不能重建，须重新连接，不承诺多步骤事务回滚。
C通过固定Docker argv启动容器及`ragent-file-worker`，后者只打开固定`/workspace`。
所有文件读取及权限落实均在C边界，不存在宿主机裸执行备用路径。

沙箱使用独立rootless Docker socket，固定镜像`ragent-file-sandbox:v1`在启动时解析为镜像ID。
新容器必须通过真实隔离探针：UID/GID1000、capabilities清空、no-new-privileges、seccomp、
只读根、无外部网络，以及cgroup限制128 MiB/0.5 CPU/32进程。
`/workspace`为16 MiB tmpfs，nosuid/nodev/noexec，不挂载宿主机目录或Docker socket。
最多8个连接工作区；全组诊断和文件工具共用4个业务执行槽，维护不占用业务槽。
状态文件和审计由服务用户持有，目录0700、文件0600；管理进程通过flock协调多进程。
审计只保留标识、工具、受限路径、结果、耗时与退出信息，不记录正文或完整工具输出；8 MiB轮转共3份。
