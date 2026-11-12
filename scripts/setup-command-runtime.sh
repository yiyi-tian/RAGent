#!/usr/bin/env bash
# 使用官方Ubuntu基础文件系统，避免依赖Docker Hub；命令容器与原有服务分开。
set -euo pipefail
umask 077
project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$project_dir"
for binary in curl openssl socat; do
    if ! command -v "$binary" >/dev/null; then
        sudo apt-get update
        sudo apt-get install -y curl openssl socat
        break
    fi
done
bash scripts/setup-sandbox.sh --daemon-only
socket_path="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/ragent-sandbox-docker.sock"
control_group=$(systemctl --user show ragent-sandbox-docker.service -p ControlGroup --value)
if ! python3 - "$control_group" <<'PYTHON'
from pathlib import Path
import sys
controllers = Path("/sys/fs/cgroup" + sys.argv[1]) / "cgroup.controllers"
raise SystemExit(0 if {"cpu", "memory", "pids"} <= set(controllers.read_text().split()) else 1)
PYTHON
then
    sudo systemctl set-property "user@$(id -u).service" 'Delegate=cpu memory pids'
    systemctl --user restart ragent-sandbox-docker.service
fi
if [[ $(uname -m) != x86_64 ]]; then
    echo '当前固定基础镜像仅验证Ubuntu 24.04 amd64。' >&2
    exit 1
fi
mkdir -p artifacts/command-runtime
archive=artifacts/command-runtime/ubuntu-base-24.04.5-base-amd64.tar.gz
checksum=e77b6f10c2590cef872b33ee9f635a0e3fd1f57fb074c0e52b5c7f56147a0c86
if [[ ! -f $archive ]]; then
    curl --fail --location --max-time 180 \
        https://cdimage.ubuntu.com/ubuntu-base/releases/24.04/release/ubuntu-base-24.04.5-base-amd64.tar.gz \
        --output "$archive"
fi
printf '%s  %s\n' "$checksum" "$archive" | sha256sum --check --status
docker --host "unix://$socket_path" import \
    --change "LABEL org.ragent.base.sha256=$checksum" \
    "$archive" ragent-ubuntu-base:24.04.5 >/dev/null
if [[ ! -f build/CMakeCache.txt ]]; then
    cmake -S . -B build -DCMAKE_BUILD_TYPE=Release \
        -DPython3_EXECUTABLE="$PWD/.venv/bin/python"
fi
cmake --build build -j 2
context=$(mktemp -d "$project_dir/artifacts/command-runtime/context-XXXXXX")
trap 'rm -rf -- "$context"' EXIT
cmake --build build --target ragent-launcher ragent-sni-check -j 2
cp build/ragent-launcher build/ragent-sni-check "$context/"
docker --host "unix://$socket_path" build \
    --build-arg HTTP_PROXY --build-arg HTTPS_PROXY --build-arg NO_PROXY \
    -f sandbox/commands.Dockerfile -t ragent-command-sandbox:v2 "$context"

docker --host "unix://$socket_path" build \
    --build-arg HTTP_PROXY --build-arg HTTPS_PROXY --build-arg NO_PROXY \
    -f sandbox/network.Dockerfile -t ragent-network-gateway:v2 "$context"
bash scripts/install-client.sh
