# 开发与验证工具

正常部署只需要根目录的[README](../README.md)和`scripts/start.sh`，无需运行本目录的
性能或评测工具。自动化回归统一使用`ctest --test-dir build --output-on-failure`，
覆盖范围见[验证说明](../docs/testing.md)。

## 保留范围

- 保留源码行为的单元、协议、并发、部署和资源回收测试，以及检索评测标注。
- 保留可以重复测量的工具及它们共用的模块，避免日后排查问题时重新实现测量逻辑。
- 不提交原始模型回答、向量缓存、快照、预算账本、运行日志或构建产物。
- 逐轮调参报告及未采用的BM25实验保存在本机被忽略的`artifacts/maintenance-archive/`，
  不属于部署步骤。当前结果统一在[性能说明](../docs/performance-concurrency.md)和
  [知识库质量说明](../docs/rag-evaluation-v4.md)维护。

## 网络与性能工具

| 入口 | 用途 | 前置条件与用量 |
|---|---|---|
| `tests/benchmark/network_benchmark.py` | 固定响应吞吐与延迟基准 | 已构建C服务；无云端调用 |
| `connection_scale.py` | 千连接PING与接入测量 | 已构建C服务；无云端调用 |
| `overload_probe.py` | 连接复用/冷连接过载对照 | 已构建C服务；无云端调用 |
| `controlled_scale.py` | 本地受控模型的业务并发与过载 | Qdrant与冻结查询向量；无云端调用 |
| `reliability.py` | 隔离故障恢复与持续运行 | Docker、已有Qdrant与冻结查询向量；无云端调用 |
| `benchmark_agent.py` | 真实模型、检索与端到端测量 | 有效云端配置和知识库；会产生用量 |
| `socket_probe.c` | 独立C负载发生器 | CMake测试构建生成`build/socket-probe` |
| `benchmark_workers.py`、`performance_common.py` | 多进程计量与网络公共实现 | 由上述工具调用，无需单独运行 |

Python工具在仓库根目录通过`.venv/bin/python -m tools.<模块名> --help`查看参数。
具体已验证的运行命令见[性能说明](../docs/performance-concurrency.md#复测)。
真实模型测试必须指定输出及预算账本；同一轮沿用原账本，调用前持久化预留，
失败和未使用预留不回退，不能新建账本来绕过原有额度。当前性能优化已停止，不自动运行付费测试。

## 知识库评测工具

| 入口 | 用途 | 前置条件与用量 |
|---|---|---|
| `evaluate_rag.py` | 来源级检索评测 | 当前知识库；调用Embedding |
| `evaluate_evidence.py` | 首版证据块对照 | 对应首版快照和集合；调用Embedding |
| `compare_rag.py` | 冻结语料、候选分块对照与公共评测逻辑 | 原始手册和对应集合；评测可能调用Embedding，`--answers`调用模型 |
| `diagnose_retrieval.py` | 使用冻结向量定位召回/排序问题 | 匹配的语料与查询向量缓存；不调用云端 |
| `evaluate_context.py` | 上下文与多粒度检索评测 | 冻结语料及历次查询缓存；缺缓存调用Embedding，`--answers`调用模型 |
| `assemble_multigrain.py` | 从已校验缓存组装多粒度集合 | 对应语料、段落/条目向量缓存和Qdrant；不调用云端，但写入目标集合 |

旧版及独立验证集文件名中的版本号用于标识固定标注，当前评测仍会合并使用，不应删除。
部分对照工具依赖本机`artifacts/rag-optimization/`等历史冻结资料，仓库不包含这些文件；
它们是维护工具，不保证全新克隆后直接运行。新设备应使用`ragent-manual import`
或部署脚本自行入库，不需要历史缓存或实验组装工具。质量口径和限制见
[多粒度验收](../docs/rag-evaluation-v4.md)。

`validate_cmd.py`执行真实DeepSeek CMD验收，使用独立持久预算账本（80次模型、40次Embedding，
失败调用也计入）。precheck为两条诊断冒烟，formal为冻结的12条自然语言用例；fix可使用
`--case`选择已有用例，13/14是针对已入库ps/ls手册的检索加诊断补充验证。
仅在明确允许云端调用时运行；不要删除或重置账本来重复付费测试。使用方式与本轮结果见
[CMD验证](../docs/testing.md#cmd沙箱与真实模型验证)。
