# Linux 手册知识库

知识库提供 Linux 手册离线入库、向量检索与在线 Agent 检索。当前集合包含 8 本手册、
306 个分块，使用百炼 `text-embedding-v4`（1024 维）和 Qdrant 1.20.0。
DeepSeek RAG 与 AGENT 检索闭环已通过 C 客户端验证，质量限制见下文评测。
项目安装要求 Python 3.11 以上，步骤见 [README](../README.md)；离线模块使用标准库。
导入还需要 Linux 的 `man`、`groff`、`dpkg-query` 和手册包。

默认导入 `passwd`、`man`、`ls`、`grep`、`chmod`、`ps`、`gpasswd`、`systemctl` 的第 1 节。优先选择 `/usr/share/man/zh_CN`，无中文条目时使用英文；一份手册内部也可能混有中英文。可通过命令行指定安装好的其他第 1 节手册。来源元数据记录手册名、章节标题、语言、发行包版本、包的版权文件和文本哈希；使用前应根据实际版权文件审查来源许可。

每次导入先准备手册，再以最多 10 个分块为一组调用向量接口并写入 Qdrant。结果输出手册及块数的入库清单；导入中途失败时，已成功写入的批次保留，修复后再次运行可通过稳定 ID 覆盖重复项。手册内容或发行包版本变化会生成新 ID；入库前会检查同一手册已有块，若不属于本次内容则拒绝导入，需使用新集合并验证后切换。不得对同一集合并发执行导入。导入的文本会发送到配置的云端 Embedding 服务。

## 配置与运行

可复制仓库根目录的 `.env.example` 为本地 `.env`，填写百炼业务空间地址和 API Key。程序不会自动读取该文件；在可信的 `.env` 所在 shell 中执行以下命令，将变量导出后再运行入库或检索：

```bash
cp .env.example .env
# 编辑 .env，填写必要配置。
set -a
source .env
set +a
```

在运行进程环境中配置以下变量。`RAGENT_EMBEDDING_BASE_URL` 为不含 `/embeddings` 的基础地址，必须是服务实际支持的兼容接口；外部服务必须使用 HTTPS；仅本机测试地址允许 HTTP。密钥不要写进仓库或命令历史。

- `RAGENT_EMBEDDING_BASE_URL` 和 `RAGENT_EMBEDDING_API_KEY`：必填；Base URL 使用百炼北京地域业务空间提供的 HTTPS 地址。
- `RAGENT_EMBEDDING_MODEL`：默认 `text-embedding-v4`，使用其默认 1024 维；更换模型需要使用新集合。
- `RAGENT_QDRANT_URL`：默认 `http://127.0.0.1:6333`。
- `RAGENT_QDRANT_COLLECTION`：默认 `linux_manual_v1`。
- `RAGENT_QDRANT_API_KEY`：可选，连接受保护的 Qdrant 服务时使用。

集合记录 Embedding 模型名及基础地址的哈希，导入和检索都检查模型、地址、维度和余弦距离是否一致；缺少这些元数据的现有集合会被拒绝。接口不会自动清空或迁移集合。Embedding 单批最多 10 条文本，单条 UTF-8 文本最多 8192 字节；服务仍会按模型 token 上限验证输入。服务 URL 不接受嵌入的凭据和查询参数，HTTP 客户端不会跟随重定向。

## 组员启动 Qdrant

Docker 前置安装、官方 Qdrant 镜像与数据卷创建、健康检查和最小手册自行入库步骤统一见
[README](../README.md#docker-与-qdrant)。该流程从空数据库开始，无需获取本机快照。
自行导入的块数取决于安装的手册版本；要复现完全相同的集合，才需要下文的快照。

## 当前机器的 Qdrant

当前机器已有官方 Qdrant 1.20.0 容器 `ragent-qdrant-official`，使用相同镜像摘要，REST 和 gRPC 端口仅映射到 `127.0.0.1`。它已挂载 `/home/cheese/qdrant_storage` 到容器内的 `/qdrant/storage`；这是当前用户的主机数据目录，不是组员需要照搬的路径。容器配置为 `unless-stopped`，遥测已关闭。Docker 守护进程仍需运行。

Docker 服务已启用开机启动；docker0 使用 `172.17.0.1/16`，宿主机 Qdrant
6333 健康检查通过。Docker 服务重启后地址及容器恢复正常；当前开机启动配置的再次
宿主机重启验证尚未完成。集合快照恢复已验证，完整数据目录备份恢复尚未验收。
不要让两个 Qdrant 容器同时挂载同一份数据。

百炼的接口参数和地域地址以[官方文档](https://help.aliyun.com/zh/model-studio/embedding-interfaces-compatible-with-openai)为准。
配置好服务后，执行以下命令以导入指定手册并查询：

```bash
.venv/bin/python -m rag import passwd ls
.venv/bin/python -m rag search '如何列出目录中的文件？' --limit 5
```

已使用百炼北京地域 `text-embedding-v4` 和 Qdrant 1.20.0 导入默认 8 本手册，共 306 个分块，逐本核对了分块 ID 与数量。重复导入通过稳定块 ID 覆盖，避免重复条目。完整语料中，“如何修改用户密码？”首条来源为 passwd(1)，“如何列出目录中的文件？”首条来源为 ls(1)。该小样本不代表全面检索质量。分数为向量相似度，不是概率；当前没有配置相关度阈值，非空集合可能对无关问题也返回结果，使用方必须判断证据是否足够。

## 已执行的本地检查

```bash
.venv/bin/python -m unittest discover -s tests/rag -v
```

测试使用受控 HTTP 服务验证批次、重复导入、版本冲突、检索字段、集合不匹配、
认证、限流和超时。在线检索、模型和 C HTTP 适配测试见[验证说明](testing.md)。
测试通过不证明语义检索质量。

## 首版知识库快照

首版 `linux-manual-v1` 本机交付文件位于 `artifacts/knowledge-base/linux-manual-v1-osejvh4r/`，包含集合快照、`manifest.json` 和 `SHA256SUMS`。该目录已加入 Git 忽略规则，快照暂未上传；清单包含 Qdrant 1.20.0、模型名、向量维度、分块规则与脚本哈希、手册来源、发行包版本及版权文件路径。版权文件路径是溯源线索，不代表已经完成向组员分发的许可审查。

导出时通过 Qdrant 的 `POST /collections/linux_manual_v1/snapshots?wait=true` 创建集合快照，并以返回的快照名从 `GET /collections/linux_manual_v1/snapshots/{快照名}` 下载；核对下载文件的 SHA-256 和返回的校验值与大小。清单需要同步记录集合配置、来源元数据和分块脚本的 SHA-256，并为快照和清单生成 `SHA256SUMS`。确认本地文件有效后，只删除本次创建的服务端临时快照，不删除集合或其他快照。

校验现有交付文件：

```bash
cd artifacts/knowledge-base/linux-manual-v1-osejvh4r
sha256sum -c SHA256SUMS
```

恢复前应确认本地 Qdrant 使用 1.20.0、已备份原数据，并确认目标集合不存在：上传恢复会覆盖同名集合，不能在现有数据上盲目执行。使用独立的空 Qdrant 后，从快照文件所在目录执行：

```bash
curl --noproxy '*' -fsS -X POST -F 'snapshot=@linux_manual_v1.snapshot' \
  'http://127.0.0.1:6333/collections/linux_manual_v1/snapshots/upload?priority=snapshot&wait=true'
```

上述恢复流程已在独立临时 Qdrant 1.20.0 容器中验证；恢复后集合配置和模型元数据，以及全部 306 个分块的 ID、载荷和向量与原集合一致。Qdrant 对跨版本快照恢复有限制，详见[官方快照文档](https://qdrant.tech/documentation/operations/snapshots/)。

快照包含手册文本、向量和集合元数据，但不包含 API Key；查询时仍需配置有权限的百炼密钥，并使用与原集合相同的模型及 Base URL。清单只记录地址哈希，不记录 Base URL。后续手册、分块规则或模型改变时应发布新版本快照和清单，不覆盖现有交付文件；组员收到快照前应先完成来源许可和分发范围确认。

## 检索评测

评测集、方法、真实基线和人工复核结果见[检索评测 v1](rag-evaluation-v1.md)。先按本文配置 Embedding 与 Qdrant 环境变量，再从仓库根目录运行：

```bash
.venv/bin/python -m tools.evaluate_rag --dataset tests/rag/evaluation_v1.json
```

输出包括每个问题的来源、分数和受限摘录，以及来源级统计；评测只读 Qdrant，但会调用百炼 Embedding 并产生 API 用量。可使用 `--output` 保存到新文件；文件已存在时拒绝覆盖。`artifacts/evaluation/` 已被 Git 忽略。服务出错时报告中保留失败计数，进程返回非零状态。

### 证据块对比评测

[证据块检索对比 v1](rag-evidence-v1.md)记录了基线和实验性来源过滤的真实结果。配置好百炼和 Qdrant，确保首版快照在本机，然后从仓库根目录运行：

```bash
.venv/bin/python -m tools.evaluate_evidence --dataset tests/rag/evidence_v1.json
```

评测会核对快照哈希、集合点数和标注来源；默认只打印报告，不修改 Qdrant 或快照。每个问题调用一次百炼 Embedding，会产生 API 用量。可用 `--snapshot` 指定已校验的首版快照，`--output` 写入不存在的报告文件。命令来源过滤仅用于实验，不是 Agent 可调用工具或生产默认策略。

## 在线 Agent 检索入口

按上文导出环境变量，按 README 安装项目后运行：

```bash
.venv/bin/ragent-server --model deepseek --retrieval qdrant
./build/agent-server --config config/agent.json
./build/agent --timeout-ms 130000 --mode rag --query '如何列出目录中的文件？'
```

RAG 强制先检索，AGENT 可由模型自主调用检索工具，来源与 chunk_id 在客户端展示。
检索适配器只读集合。服务总时限默认 120 秒，底层检索 HTTP 时限为 20 秒；
异步取消不终止已开始的同步线程请求。必需检索失败时 RAG 返回上游错误。
使用 `--model mock` 可验证真实检索加模拟回答，不能据此评估模型回答质量。
