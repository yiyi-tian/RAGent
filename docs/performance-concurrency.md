# 并发配置与测量结果

本轮性能优化已结束，不再将9请求/秒作为验收要求，也不继续付费压测。
真实模型千在途实测1000/1000成功、整批8.822请求/秒、混合P95为106.894秒。
这证明固定负载下能够接纳并完成千请求突发，不代表每个请求都能立即执行。
默认配置继续使用单进程64/32/8；四进程128/128/16作为手动可选配置保留。

## 当前配置与运行

| 配置项 | 当前值 |
|---|---:|
| C工作线程 | 64 |
| 模型并发 | 32 |
| 独立检索线程 | 8 |
| Python固定工作进程 | 1 |
| 最大连接数 | 2048 |
| 任务/完成队列容量 | 1024 / 256 |
| C请求时限（含排队） | 125秒 |
| Python总时限 / 客户端时限 | 120 / 130秒 |

部署构建使用Release。`bash scripts/start.sh` 读取 `config/agent.json` 与 `.env`；
已有服务须重启才能应用配置。模型并发和检索线程分别通过
`RAGENT_MODEL_CONCURRENCY`、`RAGENT_RETRIEVAL_WORKERS` 设置。
NO_PROXY让当前DeepSeek与百炼接口直连；依赖代理的部署应按实际网络调整，不修改全局代理。

网络仍为单epoll线程、固定工作线程池与有界队列。网络线程立即尝试发送新产生的PONG或
拒绝响应，EAGAIN时等待EPOLLOUT；工作线程不向客户端Socket写入。文件描述符软限制
按连接与线程规模检查，硬限制不足明确失败。HTTP处理器按线程复用，只读TLS配置仍验证
证书；检索复用启动时集合校验并使用Qdrant批量查询，知识库更新后须重启服务。

## 测量口径

- 正常负载：每轮200请求，最多32在途，完成一个再补一个；衡量日常响应速度。
- 真实千在途：独立C客户端一次发出1000请求，包含排队、检索和真实模型调用；
  在途包含排队与执行，不等于1000路模型同时生成。
- 整批吞吐为成功请求数除以整批耗时，包含启动负载后的排空时间；P95取逐请求样本。
- RAG/AGENT各占50%，使用相同问题组、DeepSeek、百炼Embedding与真实Qdrant。
  不减少输出预算或工具轮次，不使用回答缓存或自动重试。

本机为4个可用CPU的Linux虚拟机。模型、上游负载和网络会波动，以下数字不是服务承诺。
测试报告中的业务成功检查协议终态、证据和工具状态，不等于人工核验每份回答的正确性。

## 真实模型结果

单进程默认配置三轮600/600成功，整批合并吞吐为3.944请求/秒，
RAG/AGENT合并P95为7.759/13.635秒。以下四进程测试采用C128线程、模型总并发128、
检索总线程16；整批合并吞吐为6.770请求/秒，相同正常负载下提高约71.7%。

三轮正常负载均为200请求、最多32在途，RAG与AGENT各100，真实DeepSeek、Embedding和
Qdrant，串行执行。与历史正常负载使用相同问题组，未同时运行类型检查或其他压测。

| 轮次 | 成功数 | 吞吐（请求/秒） | RAG P95（秒） | AGENT P95（秒） |
|---|---:|---:|---:|---:|
| 1 | 200/200 | 6.749 | 4.592 | 8.214 |
| 2 | 200/200 | 6.540 | 4.717 | 7.405 |
| 3 | 200/200 | 7.039 | 4.617 | 7.610 |

合并样本的RAG/AGENT P95为4.663/7.733秒，满足正常负载目标。
独立C客户端随后一次发送1000请求，RAG与AGENT各500，没有分批放行：

| 指标 | 实测 |
|---|---:|
| 客户端发送/服务端业务在途峰值 | 1000 / 1000 |
| 成功数 | 1000/1000 |
| 整批耗时 | 113.357秒 |
| 整批吞吐 | 8.822请求/秒 |
| 混合P95 | 106.894秒 |
| C排队P95 | 94.530秒 |
| C活动线程平均数 | 119.6 / 128 |
| 模型调用平均在途 | 73.3 / 128 |
| 实际模型/Embedding放行次数 | 1501 / 1252 |
| C RSS峰值 | 28164 KiB |
| 四个业务进程RSS峰值之和 | 501292 KiB |

业务进程RSS没有包含压测父进程，不能当作整机内存。未对1000份回答逐条进行人工语义审核；
成功检查协议终态、非空回答、证据及工具状态。该负载为固定问题组，不能外推为任意复杂任务
的吞吐承诺。原始数据为`capacity-v7-normal-{1,2,3}.json`、`capacity-v7-real-1000.json`，
源文件与二进制指纹见`capacity-v7-final-source.json`，判定见`capacity-v7-final-verdict.json`。

千请求混合P95主要由排队组成；不能把8.822请求/秒理解成低延迟交互能力。
正常负载达到原定成功率至少99%、吞吐至少2.5请求/秒、RAG/AGENT P95不超过15/25秒。
真实千请求没有达到原9请求/秒门槛，停止该目标后保留实测结果，不追溯修改历史判定文件。

### 当前源码与实测版本的边界

真实千请求之后，增加了固定工具定义绑定的复用并统一采用uvloop事件循环。
请求消息仍逐次独立，没有复用模型回答。这两项调整有自动化回归和本地TLS闭环验证，
但未重新进行付费千请求测试，不能把上面的真实结果当作当前源码的重新验收。
完整TLS兼容接口、真实Qdrant副本的受控千请求对照中，uvloop为1000/1000成功、
9.146请求/秒、P95为101.326秒；模型每轮固定2秒、Embedding固定0.6秒，
这是本地受控数据，不是真实云端吞吐。当前源码16请求TLS闭环全部成功。

## 可选多进程配置

在已加载可信`.env`的终端执行：

```bash
RAGENT_MODEL_CONCURRENCY=128 RAGENT_RETRIEVAL_WORKERS=16 \
  .venv/bin/ragent-server --model deepseek --retrieval qdrant --workers 4
```

另一终端执行：

```bash
./build/agent-server --config config/agent.json --worker-threads 128
```

模型和检索配置是整组总量，按进程均分；每个进程独立持有HTTP客户端和只读手册索引。
全部进程就绪才报告启动完成，任一进程异常退出时停止整组。客户端协议、队列容量与时限不变。
增加进程需要更多内存；上述RSS不包含父进程，不能作为整机内存预算。

## 网络、故障与稳定性

默认64线程配置使用独立C负载发生器完成三轮网络测试。先建立1000个空闲连接，
再执行三波同步PING；冷连接测试同时创建1100个业务连接和两个PING探测连接。
后端受控阻塞，任务队列满时明确拒绝，其余请求取消，不计作业务成功。

| 轮次 | PONG成功数 | 同步PING P95（秒） | 冷连接数 | 拒绝数 | 拒绝P95（秒） |
|---|---:|---:|---:|---:|---:|
| 1 | 3000/3000 | 0.572 | 1100 | 12 | 0.763 |
| 2 | 3000/3000 | 0.605 | 1100 | 12 | 0.574 |
| 3 | 3000/3000 | 0.612 | 1100 | 12 | 0.774 |

三轮均通过，最差轮次的PING/拒绝P95分别为0.612/0.774秒。服务日志确认每轮实际接纳
连接峰值1102（含两个PING探测连接）。其余1088请求在探测后取消，不计作成功。

10项隔离故障恢复全部通过，覆盖Qdrant、Python、本地模型接口停启，以及
401/402/429/500、非法响应、超时和断连。60分钟受控长测8250/8250成功，
RAG/AGENT P95为2.830/4.832秒；后段与预热后RSS中位数相比，C增长1124 KiB、
Python增长204 KiB，排空后队列为0。这些为当时单进程版本的本地验证，
不代表当前四进程的长测或真实云端容量。运行方式见[验证说明](testing.md)。

## 数据与工具保留

常用回归测试保留在`tests/`，可重复使用的测量工具保留在`tools/`，用途见
[工具说明](../tools/README.md)。原始响应、向量、预算账本、逐轮实验和日志保留在
本机被忽略的`artifacts/`，不随仓库分发，其他成员不能直接访问这些路径。
逐轮调参文档与未采用的BM25实验已移入本机`artifacts/maintenance-archive/`。
知识库当前方案与质量结果统一见[多粒度验收](rag-evaluation-v4.md)。

真实模型报告为`artifacts/performance/capacity-v7-normal-{1,2,3}.json`、
`capacity-v7-real-1000.json`和`capacity-v7-final-source.json`；网络报告为
`full-final-network-{1,2,3}-ping.json`及对应`-cold/report.json`。
本轮容量预算账本`full-capacity-budget.json`保留累计预留8575次模型、7292次Embedding，
包含未使用预留，不等于供应商实际费用；停止优化后不清零、不退回、不再扩大预算。

## 复测

构建时保持 `BUILD_TESTING=ON`（默认开启），生成 `build/socket-probe`；CTest中的
`native-probe-unit` 验证半包、递增ID、错误ID、非法JSON、提前断开和多线程样本汇总。

```bash
cmake --build build -j4
.venv/bin/python -m tools.connection_scale --client-io native \
  --output artifacts/performance/new-ping.json
set -a
source .env
set +a
.venv/bin/python -m tools.controlled_scale --counts 1100 --client-io native \
  --output artifacts/performance/new-cold
.venv/bin/python -m tools.controlled_scale --counts 1000 --client-io native \
  --output artifacts/performance/new-controlled
```

过载工具输出目录须不存在。连接复用对照使用 `--preconnect --client-io stream`，
不要拿它替代冷连接测试。受控工具不会访问云端模型；Qdrant须可用。

以下为已验证的工具用法，会产生真实用量；本轮整理不执行。
新任务需设置明确预算并使用独立输出；同一轮继续运行必须沿用原预算账本，不得重置：

```bash
.venv/bin/python -m tools.benchmark_agent --target --mode mixed --clients 32 --count 200 \
  --model-budget 300 --embedding-budget 300 \
  --budget-ledger artifacts/performance/new-round-budget.json \
  --output artifacts/performance/new-round.json
```

连续三次失败，或至少20次完成后失败率超过5%，停止发出剩余请求；记录NOT_SENT和
BUDGET_STOP，不视为成功。报告区分检索线程等待、实际执行、模型等待与执行；固定到达
速率使用 `--arrival-rate`，延迟计入客户端等待发出的时间。
