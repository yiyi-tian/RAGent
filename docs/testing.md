# 第一阶段验证说明

## 运行方式

在项目根目录执行：

```bash
cmake -S . -B build -DCMAKE_BUILD_TYPE=Debug
cmake --build build -j 2
ctest --test-dir build --output-on-failure
```

测试使用Python 3标准库，无需安装Python第三方依赖。CTest启动`tests/integration/network_test.py`，传入本次构建的服务器和客户端路径。测试服务器绑定127.0.0.1的系统分配端口，不占用固定9000端口。

也可以直接运行以显示每个用例：

```bash
python3 tests/integration/network_test.py ./build/agent-server ./build/agent
```

## 覆盖范围

- 独立按大端序构造20字节消息头，对照协议样例及高32位非零的request_id。
- CHAT/RAG/CMD固定响应，中文UTF-8、query原样保留、额外字段忽略。
- 头和消息体各位置拆分、100帧合并发送，以及PING/PONG。
- 空消息体、非对象JSON、非法UTF-8、BOM、尾随垃圾、缺失或错误类型的query、空白query。
- 非法magic/version、超长头、未知消息类型、错误方向、0/重复/倒退请求ID。
- 1 MiB请求边界、1 MiB响应边界与编码后响应超限的INTERNAL_ERROR。
- 10个客户端通过同步屏障同时保持连接，每个客户端连续发送10条独立请求，核验ID及结果。
- 不完整帧中途断开、慢读取的大响应、发送缓冲超限后服务继续可用。
- C++客户端三种单次请求模式、REPL切换与单次命令不改变模式、帮助和网络状态检查。
- 独立假服务器按字节发送响应，验证客户端接收半包；错误响应退出码、非法响应ID/类型、超时退出。
- 有连接时发送SIGINT，验证服务器及时停止。

大响应超过服务器每轮64 KiB写入预算，用于验证重复EPOLLOUT和发送偏移；慢客户端测试额外耗尽内核发送缓冲，验证用户态待发送数据上限。这些测试不代表线程池、业务过载或性能基准测试。

## 当前限制

第一阶段无工作线程、任务队列或外部服务，因此不测试SERVER_BUSY、任务乱序完成、LLM/RAG故障或实际命令执行。历史记录和Tab补全未接入；客户端当前只支持IPv4地址。日志记录连接ID、请求ID、请求类型和处理/关闭状态，不记录用户完整query。
