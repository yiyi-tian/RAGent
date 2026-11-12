# 验证说明

## 构建与运行

完整依赖安装与构建步骤见 [README](../README.md)。在仓库根目录使用同一 Python 环境：

```bash
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release -DPython3_EXECUTABLE="$PWD/.venv/bin/python"
cmake --build build -j 2
ctest --test-dir build --output-on-failure
.venv/bin/python -m compileall -q python rag tools tests
.venv/bin/pyright --pythonpath .venv/bin/python
.venv/bin/black --check python rag tools tests
```

Pyright统一覆盖`python/ragent`、`rag`、`tools`和`tests`，不能只凭单个目录通过宣称全部类型检查通过。
GitHub Actions在push及pull request时执行同范围的语法、类型、格式、Release构建与回归检查，
使用Mock和本地受控接口，无需配置云端密钥。检查失败须修复后再交付；分支保护仍由仓库管理员配置。

CTest 包含线程池背压、部署脚本、RAG、并发、网络、Agent、固定工作进程和
独立 C 负载发生器、执行器及文件工作区共十项。
Agent 测试依赖已安装的项目及
Python 运行依赖；依赖不齐时 CMake 会提示未启用，不能将其当作完整验收。
网络测试仅需 Python 标准库，服务与集成测试使用动态端口。Agent 测试串行运行，
覆盖子进程启动与清理，允许较慢环境中的首次导入。

也可以直接运行以显示每个用例：

~~~bash
python3 tests/integration/network_test.py ./build/agent-server ./build/agent
~~~

## 覆盖范围

- 独立按大端序构造20字节消息头，对照协议样例及高32位非零的request_id。
- CHAT/RAG/CMD/AGENT四种请求的固定响应、中文UTF-8、query原样保留和额外字段忽略。
- Agent作为交互客户端默认模式，普通输入发送AGENT_REQUEST；单次请求支持四种模式。
- 头和消息体各位置拆分、100帧合并发送，以及PING/PONG。
- 空消息体、非对象JSON、非法UTF-8、BOM、尾随垃圾、缺失或错误类型的query、空白query。
- 非法magic/version、超长头、未知消息类型、错误方向、0/重复/倒退请求ID。
- 1 MiB请求边界、1 MiB响应边界与编码后响应超限的INTERNAL_ERROR。
- 10个客户端通过同步屏障同时保持连接，每个客户端连续发送10条独立请求，核验ID及结果。
- 不完整帧中途断开、慢读取的大响应、发送缓冲超限后服务继续可用。
- C客户端单次请求、REPL模式切换、帮助和网络状态检查。
- 伪终端中的等待动画、耗时与清除：覆盖成功、错误、断连、超时和 Ctrl+C；重定向不输出动画。
- 独立假服务器按字节发送响应，验证客户端接收半包；错误响应退出码、非法响应ID/类型、超时退出。
- 有连接时发送SIGINT，验证服务器及时停止。

大响应超过服务器每轮64 KiB写入预算，用于验证重复EPOLLOUT和发送偏移；慢客户端测试额外耗尽内核发送缓冲，验证用户态待发送数据上限。这些测试不代表端到端性能基准。

## Agent、模型与检索覆盖

- 四种模式、独立上下文、多轮工具调用、并行、模式授权、参数类型和部分失败。
- 重复调用 ID、单轮工具数与模型调用轮数限制、请求总超时。
- C HTTP 连接失败、超时、非法 JSON、字段缺失、错误码映射和响应大小边界。
- C 排队过期不调用后端、剩余预算不重置、PING 不受业务排队阻塞；Python 校验并应用剩余预算。
- 断连后跳过排队任务、中止活动 HTTP 请求、复用工作线程；检索线程传播截止时间与协作取消。
- 存活检查、非付费就绪检查及依赖故障后的恢复；CLI 关闭模型客户端与检索线程池。
- DeepSeek 适配器的受控 HTTP 往返、工具 ID 回填、认证、限流、超时、空回答及输出截断。
- 固定四进程同端口请求、整组就绪、正常退出与单进程故障后整组回收。
- JSON发送路径与SDK原路径的请求体、工具响应及Token用量一致性；本地持久连接复用与失败恢复。
- RAG 来源投影、空结果、检索失败，以及 Embedding/Qdrant 的集合一致性和依赖故障。
- 客户端检索来源展示与非法来源响应拒绝。

## 验收结果与限制

网络、Agent 与 RAG 的Release构建及回归检查通过；部署脚本隔离测试通过。
Pyright、Black、依赖一致性检查通过。
Python可编辑安装与wheel构建已验证，已核对包内模块与依赖声明。
受控 HTTP 测试不调用云端服务，不代表真实模型表现或语义检索质量。

当前uvloop实现已完成Release构建及回归、在线代码与多进程压测工具的Pyright检查。
wheel已重新构建并核对固定工作进程模块和uvloop依赖；项目依赖仍使用版本范围，没有锁定文件。
新版本四进程还完成16请求TLS闭环：模型与Embedding均连接本地HTTPS兼容接口，Qdrant使用
真实容器中的独立复制集合，24次模型、16次Embedding本地调用全部对应预期请求，没有云端
调用，临时容器与卷已清理，见`artifacts/performance/worker-uvloop-final-smoke/verdict.json`。
当前整理未重新执行新版本的全新虚拟环境安装或另一台宿主机部署，不能将包构建当作这些场景已通过。

四种模式的协议与工具行为由回归测试覆盖；当前uvloop版本尚未重新执行真实云端四模式冒烟。
来源与证据质量见[来源级评测](rag-evaluation-v1.md)及
[证据块评测](rag-evidence-v1.md)。

诊断与连接临时文件工具已接入真实C组件及rootless容器，验证范围见下文。真实千在途已完成1000/1000成功，但吞吐8.822请求/秒，
本轮不再追求9请求/秒；uvloop版本未重新进行真实云端千请求测试，详见
[高并发配置与测量](performance-concurrency.md)。客户端没有跨请求
历史与 Tab 补全。Docker 已启用开机启动，Qdrant 可用；该启动配置的再次宿主机重启
验收尚未完成。

## 固定响应网络基准

构建后执行 `python3 tests/benchmark/network_benchmark.py build/agent-server --clients 16 --requests 100`。脚本输出请求数、错误数、吞吐、延迟分位数和服务器CPU使用率；需要Linux `/proc`。比较版本时使用相同构建类型、参数和空闲环境，并记录机器配置。该结果只反映固定响应，不代表模型或RAG性能。

## ThreadSanitizer 并发检查

ThreadSanitizer 在运行时检查已执行代码路径中的数据竞争；它不能证明所有线程调度都没有问题。以下命令使用独立构建目录，收集所有子进程的检测报告，并检查测试结果：

~~~bash
cmake -S . -B /tmp/ragent-tsan -DCMAKE_BUILD_TYPE=Debug \
  -DCMAKE_C_FLAGS='-fsanitize=thread -fno-omit-frame-pointer' \
  -DCMAKE_EXE_LINKER_FLAGS='-fsanitize=thread'
cmake --build /tmp/ragent-tsan -j2
report_dir=$(mktemp -d /tmp/ragent-tsan-report-XXXXXX)
TSAN_OPTIONS="halt_on_error=1:log_path=$report_dir/report" \
  setarch "$(uname -m)" -R ctest --test-dir /tmp/ragent-tsan --output-on-failure
find "$report_dir" -type f -print
~~~

本机的地址随机化配置会使ThreadSanitizer启动时报 `unexpected memory mapping`；`setarch -R` 仅关闭测试进程及其子进程的地址随机化，不修改系统设置。若环境不允许使用该选项，需要在允许的测试环境中运行。测试通过且报告目录为空才表示这些测试路径未发现数据竞争。

## 部署脚本验证

`deployment-unit` 使用临时目录、模拟 Docker/包管理命令和本地受控服务，验证
`scripts/start.sh` 的部署创建流程、日常免安装构建与付费查询、启动清理、空库和端口占用处理，不安装软件、不访问云端。
这不代替另一台机器上 Docker 拉取、手册实际入库和真实模型调用的部署验收。


## 千连接负载发生器

`BUILD_TESTING=ON` 时构建独立C客户端 `build/socket-probe`，不复用服务端编解码。
`native-probe-unit` 覆盖半包、递增ID、错误ID、非法JSON尾随数据、提前断开和多线程
原始样本汇总。CTest共九组；千连接PING和冷连接过载各连续三轮通过，测量方法、
客户端线程数和局限见[性能验收](performance-concurrency.md)。这些网络复测没有云端用量。
独立C客户端同时支持RAG/AGENT业务突发，区分完整成功、工具失败、证据为空及协议错误；
受控千请求使用该客户端完成1000/1000成功、P95为97.206秒的回归。
Streams对照的P95为101.167秒，未达100秒目标，记录与方法限制一并保留。

## 隔离故障与持续运行

在仓库根目录加载可信 `.env`，并准备已有知识库与冻结查询向量：

```bash
set -a
source .env
set +a
.venv/bin/python -m tools.reliability --faults-only \
  --output artifacts/performance/new-faults
.venv/bin/python -m tools.reliability --duration 3600 \
  --output artifacts/performance/new-soak
```

输出目录须不存在。工具从 `ragent-qdrant-official` 获取本机镜像，复制当前集合到独立容器
和命名卷，验证数量后使用；只读原集合，结束后删除自建容器和卷。
冻结查询向量来自被忽略的 `artifacts/rag-optimization/final`，缺少时不能直接运行；
这是一项本机回归工具，不是首次部署所需步骤。

故障测试覆盖 Qdrant/Python/本地模型接口停启、401/402/429/500、非法 JSON、超时和断连。
每次故障都核对错误与恢复后新请求。模型接口使用本地兼容服务，不产生云端调用。
持续运行使用真实 C/Python/Qdrant，模型每轮固定2秒、Embedding固定0.6秒，冻结四条问题；
每秒2.5请求、最多32在途，预热10分钟、稳定45分钟、排空5分钟。
记录逐请求结果、每秒RSS/线程/FD、最近观察到的队列深度及退出时队列状态。
少于3600秒只算冒烟，不能通过完整稳定性验收，也不能代替真实模型千在途测试。

## 只读执行器验证

`executor-unit`使用本地C夹具验证无输出超时、输出关闭后继续运行、输出超限、边界长度、
非零退出、密钥环境隔离、遗留后代进程组清理以及信号取消。Release测试保持assert启用。
Agent组覆盖实际free/df/ps、拒绝未知操作与嵌入NUL、非法执行器响应、任务取消与资源回收，
并通过独立C网络服务、Python HTTP、真实C执行器完成CMD和AGENT自然语言请求闭环。
固定工作进程组增加四进程local命令模式验证。测试使用受控模型，不调用云端。
本轮通过sdist重新构建wheel，确认包含ragent.tools；不直接复用旧build/lib中的打包缓存。
Python包不包含C执行器二进制，安装后须提供独立构建的执行器。
真实DeepSeek的CMD结果见下节，不将受控测试替代真实模型验收。


## CMD沙箱与真实模型验证

在已部署独立rootless服务和镜像的本机运行：

```bash
RAGENT_TEST_SANDBOX=1 .venv/bin/python -W error::RuntimeWarning -m unittest discover \
    -s tests/agent -p test_workspace.py -v
```

沙箱测试默认不加入云端依赖；未设置该环境变量时显式跳过，不能将CTest通过视为容器验收。
本机实际执行7项测试：顺序CRUD与模式授权、8工作区容量与跨连接隔离、路径拒绝、
执行失败后关闭工作区且只读仍可用、四Python进程共享工作区、TCP身份防伪造及
正常断连/前端强杀清理，以及非法参数正文不入日志。新容器内探针检查实际cgroup资源值、UID、seccomp、
capabilities、只读根、tmpfs大小与挂载标志，并实际验证外部网络不可达。
C文件单元测试另覆盖符号链接、硬链接、原子拒绝覆盖、128项配额与分页。

付费验收工具自行启动临时端口C/Python服务、连接现有Qdrant，不更改知识库或常规服务：

```bash
set -a
source .env
set +a
.venv/bin/python -m tools.validate_cmd --phase precheck \
    --output artifacts/cmd-validation/precheck.json
.venv/bin/python -m tools.validate_cmd --phase formal \
    --output artifacts/cmd-validation/formal.json
```

账本固定为`artifacts/cmd-validation/budget.json`，总上限80次模型、40次Embedding；
precheck/formal/fix模型额度分别16/48/16，调用前计数，失败和预留不回退，不自动重试。
报告中的passed检查实际工具及状态，最终文本与证据仍须人工核查。

2026-11-12本机正式12条用例覆盖诊断、检索组合、持续文件工作流、第二客户端隔离与越权拒绝。
执行分支均满足预期；其中一次更新流程在写入并读取成功后触及模型轮数限制，
调整工具结果中的真实执行/committed语义后，创建、覆盖和读取两条真实复测均得到完整说明。
该调整未提高每请求最多4次模型调用的上限。
正式用例中df/free未检索到对应手册，模型说明证据不足；该结果只证明失败如实反馈，
不能视为这些手册的答案质量达标。ps/ls检索加诊断的两条补充用例均命中对应手册并完成实际诊断，人工核对参数解释与来源一致。
累计使用45/80次模型、4/40次Embedding（precheck 4次模型，formal 31次，fix 10次）。

原始报告、预算与构建日志保留在忽略的`artifacts/cmd-validation/`和
`artifacts/cmd-review-20261112/`，不提交运行日志、诊断快照或状态文件。
本轮不执行新的吞吐压测，也未验证另一台设备部署；文件为连接级临时数据，不提供下载、
跨连接历史、Shell或宿主机系统写入。
