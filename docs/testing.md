# 验证说明

## 构建与运行

完整依赖安装与构建步骤见 [README](../README.md)。在仓库根目录使用同一 Python 环境：

```bash
cmake -S . -B build -DCMAKE_BUILD_TYPE=Debug -DPython3_EXECUTABLE="$PWD/.venv/bin/python"
cmake --build build -j 2
ctest --test-dir build --output-on-failure
.venv/bin/pyright --pythonpath .venv/bin/python
.venv/bin/black --check python rag tools tests
```

CTest 包含线程池背压、部署脚本、RAG、并发、网络和 Agent 六项。Agent 测试依赖已安装的项目及
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
- 独立假服务器按字节发送响应，验证客户端接收半包；错误响应退出码、非法响应ID/类型、超时退出。
- 有连接时发送SIGINT，验证服务器及时停止。

大响应超过服务器每轮64 KiB写入预算，用于验证重复EPOLLOUT和发送偏移；慢客户端测试额外耗尽内核发送缓冲，验证用户态待发送数据上限。这些测试不代表端到端性能基准。

## Agent、模型与检索覆盖

- 四种模式、独立上下文、多轮工具调用、并行、模式授权、参数类型和部分失败。
- 重复调用 ID、单轮工具数与模型调用轮数限制、请求总超时。
- C HTTP 连接失败、超时、非法 JSON、字段缺失、错误码映射和响应大小边界。
- DeepSeek 适配器的受控 HTTP 往返、工具 ID 回填、认证、限流、超时、空回答及输出截断。
- RAG 来源投影、空结果、检索失败，以及 Embedding/Qdrant 的集合一致性和依赖故障。
- 客户端检索来源展示与非法来源响应拒绝。

## 验收结果与限制

网络、Agent 与 RAG 的 Debug 构建及回归检查通过；部署脚本隔离测试通过。
Pyright、Black、依赖一致性检查通过。
Python 可编辑安装、仓库外模块及命令入口、wheel 构建与安装均已验证。
受控 HTTP 测试不调用云端服务，不代表真实模型表现或语义检索质量。

真实服务验收通过 C 客户端覆盖 DeepSeek CHAT、真实手册 RAG、AGENT 自主检索和
CMD 模拟诊断。来源与证据质量见[来源级评测](rag-evaluation-v1.md)及
[证据块评测](rag-evidence-v1.md)。

当前没有真实命令执行或沙箱隔离，未进行 1000 个端到端在途请求压测。客户端没有跨请求
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
`scripts/start.sh` 的创建流程、启动清理、空库和端口占用处理，不安装软件、不访问云端。
这不代替另一台机器上 Docker 拉取、手册实际入库和真实模型调用的部署验收。
