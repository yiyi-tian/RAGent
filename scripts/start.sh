#!/usr/bin/env bash
# 本机部署：保留数据库，由当前终端监督 Python Agent 和 C 服务。
set -euo pipefail
umask 077

project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$project_dir"

skip_import=false
case "${1:-}" in
    --help|-h)
        echo '用法：bash scripts/start.sh [--skip-import]'
        echo '首次填写 .env 后运行；默认导入八本手册，已有匹配知识库可跳过入库。'
        exit 0
        ;;
    --skip-import)
        skip_import=true
        ;;
    '')
        ;;
    *)
        echo '未知参数，使用 --help 查看帮助。' >&2
        exit 1
        ;;
esac
if (( $# > 1 )); then
    echo '参数过多。' >&2
    exit 1
fi
if [[ ! -f .env ]]; then
    cp .env.example .env
    echo '已生成 .env，请填写 DeepSeek 和 Embedding 配置后再次运行。' >&2
    exit 1
fi
# .env 是由开发者维护的可信 shell 配置，不输出其中的值。
set -a
source .env
set +a
for name in RAGENT_API_KEY RAGENT_MODEL RAGENT_BASE_URL \
    RAGENT_EMBEDDING_API_KEY RAGENT_EMBEDDING_BASE_URL; do
    if [[ -z ${!name:-} ]]; then
        echo "缺少环境配置：$name" >&2
        exit 1
    fi
done
if [[ ${RAGENT_QDRANT_URL:-http://127.0.0.1:6333} != http://127.0.0.1:6333 ]]; then
    echo '本机部署脚本要求 RAGENT_QDRANT_URL=http://127.0.0.1:6333。' >&2
    exit 1
fi
if ! command -v docker >/dev/null; then
    echo '请先安装并启动 Docker。' >&2
    exit 1
fi
if ! command -v apt-get >/dev/null || ! command -v python3 >/dev/null; then
    echo '该脚本适用于已安装 Python 3 的 Ubuntu 24.04。' >&2
    exit 1
fi
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || {
    echo '需要 Python 3.11 以上。' >&2
    exit 1
}

missing_packages=()
for package in build-essential cmake pkg-config libjson-c-dev libcurl4-openssl-dev \
    python3-venv curl man-db groff manpages manpages-zh coreutils passwd grep procps systemd; do
    if [[ $(dpkg-query -W -f='${Status}' "$package" 2>/dev/null || true) != 'install ok installed' ]]; then
        missing_packages+=("$package")
    fi
done
if (( ${#missing_packages[@]} )); then
    echo '安装构建与手册依赖（需要 sudo 权限）。'
    sudo apt-get update
    sudo apt-get install -y "${missing_packages[@]}"
fi

docker_command=(docker)
if ! docker info >/dev/null 2>&1; then
    docker_command=(sudo docker)
    "${docker_command[@]}" info >/dev/null
fi

# 在执行付费入库前检查业务端口，避免连到他人运行的服务。
connection_settings=$(python3 - <<'PY'
import json
from urllib.parse import urlsplit
with open('config/agent.json') as file:
    config = json.load(file)
url = urlsplit(config['agent_url'])
if url.scheme != 'http' or url.hostname != '127.0.0.1' or url.path != '/agent':
    raise SystemExit('配置中的 Agent 必须为本机 http://127.0.0.1:端口/agent')
if config['host'] not in ('127.0.0.1', '0.0.0.0'):
    raise SystemExit('本机脚本要求 C 服务监听 127.0.0.1 或 0.0.0.0')
print(config['port'], url.port or 80)
PY
)
read -r server_port agent_port <<< "$connection_settings"
python3 - "$server_port" "$agent_port" <<'PY'
import socket
import sys
for value in sys.argv[1:]:
    port = int(value)
    if not 1 <= port <= 65535:
        raise SystemExit('配置端口必须在 1 至 65535 之间')
    with socket.socket() as listener:
        try:
            listener.bind(('127.0.0.1', port))
        except OSError:
            raise SystemExit(f'端口 {port} 已占用，请先停止现有服务')
if sys.argv[1] == sys.argv[2]:
    raise SystemExit('C 与 Python 服务不能使用相同端口')
PY

if curl --noproxy '*' -fsS --max-time 2 http://127.0.0.1:6333/healthz >/dev/null 2>&1; then
    echo '复用已就绪的本机 Qdrant。'
elif "${docker_command[@]}" container inspect ragent-qdrant >/dev/null 2>&1; then
    # 不重建已有容器，不删除或替换其挂载数据。
    "${docker_command[@]}" start ragent-qdrant >/dev/null
else
    "${docker_command[@]}" volume create ragent-linux-manual-qdrant >/dev/null
    "${docker_command[@]}" run -d --name ragent-qdrant --restart unless-stopped \
        -p 127.0.0.1:6333:6333 -p 127.0.0.1:6334:6334 \
        -v ragent-linux-manual-qdrant:/qdrant/storage \
        -e QDRANT__TELEMETRY_DISABLED=true \
        qdrant/qdrant@sha256:9bfc89f9914fc14876a88c05270685921a55b8877b254a98bcc1cc2f0a424cd2
fi
curl --noproxy '*' --fail --silent --show-error --retry 20 --retry-connrefused \
    --retry-delay 1 --max-time 3 http://127.0.0.1:6333/healthz

python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
# 清除依赖探测缓存，防止 build 仍引用已失效的本机临时目录。
cmake -S . -B build -U '*JSONC*' -U '*CURL*' -DCMAKE_BUILD_TYPE=Debug \
    -DPython3_EXECUTABLE="$project_dir/.venv/bin/python"
cmake --build build -j 2

if [[ $skip_import == false ]]; then
    echo '导入默认八本手册，将调用百炼 Embedding 并产生 API 用量。'
    .venv/bin/ragent-manual import
fi
# 包含 --skip-import 的启动也核验实际模型、集合与检索链路。
.venv/bin/ragent-manual search '如何列出目录中的文件？' --limit 1 | \
    .venv/bin/python -c 'import json, sys; sys.exit(0 if json.load(sys.stdin) else "知识库为空，请先入库")'

mkdir -p artifacts
run_dir=$(mktemp -d "$project_dir/artifacts/deploy-XXXXXX")
agent_pid=''
server_pid=''
cleanup() {
    local pid
    for pid in "$server_pid" "$agent_pid"; do
        if [[ -n $pid ]]; then
            kill "$pid" 2>/dev/null || true
        fi
    done
    # 给服务有限的清理时间；仍在等待上游的进程最终强制终止。
    local attempt alive
    for ((attempt = 0; attempt < 20; attempt++)); do
        alive=false
        for pid in "$server_pid" "$agent_pid"; do
            if [[ -n $pid ]] && kill -0 "$pid" 2>/dev/null; then
                alive=true
            fi
        done
        if [[ $alive == false ]]; then
            break
        fi
        sleep .5
    done
    for pid in "$server_pid" "$agent_pid"; do
        if [[ -n $pid ]]; then
            kill -KILL "$pid" 2>/dev/null || true
            wait "$pid" 2>/dev/null || true
        fi
    done
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

wait_for_service() {
    local pid=$1 port=$2 log=$3
    local attempt
    for ((attempt = 0; attempt < 120; attempt++)); do
        if ! kill -0 "$pid" 2>/dev/null; then
            echo "服务启动失败，日志：$log" >&2
            return 1
        fi
        if .venv/bin/python - "$port" <<'PY' >/dev/null 2>&1
import socket
import sys
with socket.create_connection(('127.0.0.1', int(sys.argv[1])), timeout=.2):
    pass
PY
        then
            return 0
        fi
        sleep .5
    done
    echo "服务启动超时，日志：$log" >&2
    return 1
}

.venv/bin/ragent-server --model deepseek --retrieval qdrant --port "$agent_port" \
    >"$run_dir/agent.log" 2>&1 &
agent_pid=$!
wait_for_service "$agent_pid" "$agent_port" "$run_dir/agent.log"
./build/agent-server --config config/agent.json >"$run_dir/server.log" 2>&1 &
server_pid=$!
wait_for_service "$server_pid" "$server_port" "$run_dir/server.log"

echo '服务已就绪，请保持当前终端运行；Ctrl+C 停止本脚本启动的两个服务。'
printf '另一个终端进入 %q 后运行：\n' "$project_dir"
printf './build/agent --host 127.0.0.1 --port %s --timeout-ms 130000\n' "$server_port"
printf '服务日志：%s\n' "$run_dir"
# 任意服务退出时清理另一个服务；Qdrant 及其数据卷保持原状。
status=0
wait -n "$agent_pid" "$server_pid" || status=$?
echo "服务进程已退出，日志：$run_dir" >&2
if (( status == 0 )); then
    status=1
fi
exit "$status"
