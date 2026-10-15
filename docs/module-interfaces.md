# RAGent 模块接口草案

状态：待确认，尚未实现。TCP字段与错误码以[协议规范](protocol.md)为准，阶段顺序以[开发计划](development-plan.md)为准。本文确定职责和数据流，不提前生成公共头文件。

## 1. 接口冻结顺序

```text
TCP协议 → Request/Response → 连接与任务所有权 → 线程池
→ 业务处理器 → RAG/LLM HTTP接口 → 配置和日志
```

每层确定输入、输出、创建/持有/释放者及失败表示，再进入下一层。公共头文件的不兼容修改需要三人共同确认。下述类型为逻辑契约，具体C++声明在对应阶段实现，不代表已存在的API。

## 2. 内部请求、响应和所有权

| 逻辑类型 | 最小数据 | 创建与流转 |
|---|---|---|
| Request | connection_id、request_id、请求类型、query | 网络线程创建，移动到任务队列，工作线程取得所有权 |
| Source | source、chunk_id、text、score | 检索层创建，随成功结果返回 |
| Success | text、sources | 业务处理器创建 |
| Error | code、message | 校验或业务失败时创建 |
| Response | connection_id、request_id、Success或Error | 工作线程创建，经完成队列交给网络线程 |

Success和Error互斥，避免同时存在成功数据和错误状态。网络线程的协议校验错误及PING可直接生成回复，不必进入线程池。请求计时等观测数据随需要在内部携带，不加入TCP消息头。

网络线程持有Socket、连接对象、收发缓冲和在途请求状态。connection_id由服务器生成，在进程生命周期内不复用，与fd分离。任务保存稳定ID和自己拥有的数据，不保存指向连接缓冲区的引用，也不持有可操作Socket的连接对象。

结果到达后，网络线程根据connection_id查找仍存活的连接；不存在则丢弃并记录。只根据fd路由会使断开的旧连接结果进入复用该fd的新连接，禁止这种实现。

## 3. 队列与线程边界

| 边界 | 生产者→消费者 | 行为 |
|---|---|---|
| 有界任务队列 | 网络线程→工作线程 | 非阻塞尝试入队；满时返回SERVER_BUSY |
| 完成队列 | 工作线程→网络线程 | 有界，或受全局结果内存上限约束 |
| 每连接发送缓冲 | 网络线程→网络线程 | 有字节上限，部分写入后保留剩余数据 |
| LLM并发许可 | 工作线程→模型调用 | 初始并发建议为1，等待必须有界 |

网络线程不调用Embedding、Qdrant、LLM，也不等待子进程。工作线程只做业务，完成结果入队后通过eventfd通知；网络线程收到通知后排空当前可取结果，不将eventfd计数机械等同于结果条数。

完成队列的背压不得使网络线程阻塞；具体队列策略在阶段2确定，并保证停止流程能唤醒等待线程。停机期间停止接纳任务，让已接纳任务形成成功或失败终态，网络线程在工作线程退出前继续处理完成结果，最后回收线程与连接。连接已断开的结果可丢弃，但要能通过日志追踪。

## 4. 业务处理器

统一逻辑接口：输入一个Request，返回Success或Error，不向客户端Socket写数据。外部异常转换为明确错误，不能让异常逃出工作线程并终止进程。

| 类型 | 处理路径 |
|---|---|
| CHAT | 构造普通Prompt → LLM → Success |
| RAG | 检索 → 构造资料与问题Prompt → LLM → 带sources的Success |
| CMD | 识别操作 → 校验 → 执行器 → Success或Error |

阶段1直接生成固定响应；阶段3接入Mock，不提前部署真实AI服务。检索资料是回答的依据，不作为系统指令；无资料或资料不足时明确说明，不生成虚构来源。

## 5. RAG服务HTTP草案

接口：`POST /retrieve`，请求及响应使用`Content-Type: application/json`和UTF-8。地址由Agent配置，不硬编码部署机器。

请求：

```json
{"query":"项目为什么使用 epoll？","top_k":3}
```

query为非空白字符串；top_k为正整数，由Agent配置提供，建议初始值3，不暴露为第一版CLI参数。服务端需设置检索条数及响应体上限，具体数值在检索阶段确定。

成功（HTTP 200）：

```json
{
  "chunks": [
    {
      "source": "development-plan.md",
      "chunk_id": "chunk-12",
      "text": "采用单 epoll 网络线程 + 全局固定工作线程池。",
      "score": 0.86
    }
  ]
}
```

chunks按相关性由高到低返回，数量不超过top_k，字段与TCP来源项一致。无结果仍返回200和`{"chunks":[]}`。返回块必须保留来源标识，不能只返回拼接后的匿名文本。

错误体统一为`{"code":"...","message":"..."}`：

| HTTP状态 | code | 场景 |
|---|---|---|
| 400 | INVALID_REQUEST | 参数非法 |
| 503 | UPSTREAM_UNAVAILABLE | Embedding或Qdrant不可用 |
| 504 | UPSTREAM_TIMEOUT | 依赖调用超时 |
| 500 | INTERNAL_ERROR | 检索服务内部失败 |

Agent的HTTP适配层将连接失败、其他非成功状态及非法响应映射为UPSTREAM_UNAVAILABLE，将调用超时映射为UPSTREAM_TIMEOUT；不原样转发外部错误页面。检索服务报告参数错误意味着Agent与服务契约不一致，须记日志排查。

RAG服务只负责向量化与检索，Agent负责拼Prompt和调用LLM。文档导入是独立工具，不在每次查询时重复导入。

## 6. LLM适配层草案

内部输入为Agent构造的完整Prompt，输出为完整回答字符串或Error。第一版不返回流式Token。CHAT和RAG复用同一适配层，不各自实现HTTP细节。

模型地址、模型标识、请求超时、输出上限和并发上限由配置控制；初始真实推理并发建议为1。等待请求不得绕过任务或结果内存上限。

具体HTTP路径、请求JSON及回答提取方式，在阶段3引入Mock前，根据拟使用的llama-server版本核实并记录；阶段5接入真实服务时复验。Mock与真实适配层应遵守相同服务契约，不能先假定未经验证的API。

连接或服务失败映射UPSTREAM_UNAVAILABLE，超时映射UPSTREAM_TIMEOUT；错误日志不得包含密钥、完整敏感文档或无限长输出。RAG最终sources来自检索结果，不从模型回答文本中解析生成。

## 7. 命令执行器草案

```text
用户query → 确定性操作映射 → CommandSpec → 白名单校验
→ 固定程序路径及argv → fork/exec/pipe → CommandResult
```

CommandSpec包含操作枚举和已校验参数，不接受任意程序路径。CommandResult包含退出码或终止信号、stdout、stderr、超时状态及输出超限状态；执行失败形成明确错误，不能把非零退出当作成功。

第一批候选操作：当前目录、系统时间、系统信息、磁盘空间、内存信息，分别对应pwd、date、uname -a、df -h、free -h。阶段6冻结输入别名、固定程序路径、允许参数和输出展示规则，未识别操作直接拒绝。

禁止system(user_input)、/bin/sh -c及Shell拼接。使用fork/exec/pipe/dup2/waitpid，持续读取stdout/stderr以免管道阻塞；统一限制输出总字节数和执行时间，超限或超时后终止并回收子进程，关闭所有不再需要的管道端。

成功输出转换为TCP的text且sources为空；失败分别使用COMMAND_NOT_ALLOWED、COMMAND_TIMEOUT、COMMAND_OUTPUT_LIMIT或COMMAND_FAILED。失败信息可展示受限诊断，但不能原样输出无限长stderr。

## 8. 配置、日志与后续验收

配置需覆盖监听地址、线程数、各队列/缓冲上限、服务地址、HTTP超时、LLM并发与输出限制、命令超时与输出限制。具体配置键在实现相应模块时冻结，不把机器环境参数写入公共协议。

日志至少可关联connection_id、request_id、类型、阶段、worker_id、队列等待时间、处理时间、总时间和最终状态；连接断开、结果丢弃及资源拒绝要有原因。敏感内容不进入日志。

验收按阶段进行：

- 阶段2：并发慢任务、队列满、连接提前断开、fd复用、完成队列背压及有序停止。
- 阶段3：三类可预测Mock响应；超时、非成功HTTP、非法JSON及错误响应映射。
- 阶段4：正确来源、空结果、依赖停机及重复导入控制。
- 阶段5：CHAT/RAG真实回答、资料不足、模型超时及推理并发限制。
- 阶段6：白名单、非法参数与Shell拼接拒绝、stdout/stderr捕获、非零退出、超时和输出上限、无僵尸进程。

当前仅形成设计文档，不表示上述接口或验收已经实现。
