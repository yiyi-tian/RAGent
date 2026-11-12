#!/usr/bin/env bash
# 独立rootless守护进程，不切换Docker默认上下文或修改现有数据库容器。
set -euo pipefail
umask 077
project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$project_dir"
if [[ $(id -u) == 0 ]]; then
    echo '请使用普通用户运行此脚本。' >&2
    exit 1
fi
for executable in newuidmap newgidmap slirp4netns; do
    if ! command -v "$executable" >/dev/null; then
        sudo apt-get update
        sudo apt-get install -y uidmap slirp4netns fuse-overlayfs dbus-user-session
        break
    fi
done
if [[ ! -x /usr/bin/dockerd-rootless.sh ]]; then
    echo '缺少dockerd-rootless.sh，请安装Docker Engine的rootless组件。' >&2
    exit 1
fi
mkdir -p "$HOME/.config/systemd/user"
unit_path="$HOME/.config/systemd/user/ragent-sandbox-docker.service"
cat > "$unit_path" <<'UNIT'
[Unit]
Description=RAGent rootless sandbox Docker
After=default.target

[Service]
Environment=PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
Environment=DOCKERD_ROOTLESS_ROOTLESSKIT_STATE_DIR=%t/ragent-sandbox-rootlesskit
ExecStart=/usr/bin/dockerd-rootless.sh \
    --host=unix://%t/ragent-sandbox-docker.sock \
    --data-root=%h/.local/share/ragent-sandbox-docker \
    --exec-root=%t/ragent-sandbox-docker-exec \
    --pidfile=%t/ragent-sandbox-docker.pid
Restart=on-failure
RestartSec=2
TimeoutStartSec=120
Delegate=yes
KillMode=mixed
TasksMax=512
LimitNOFILE=65536

[Install]
WantedBy=default.target
UNIT
systemctl --user daemon-reload
systemctl --user enable --now ragent-sandbox-docker.service
socket_path="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/ragent-sandbox-docker.sock"
for attempt in {1..60}; do
    if docker --host "unix://$socket_path" info >/dev/null 2>&1; then
        break
    fi
    sleep 1
done
docker --host "unix://$socket_path" info --format '{{json .SecurityOptions}}'
if [[ ${1:-} == --daemon-only ]]; then
    exit 0
fi
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release -DPython3_EXECUTABLE="$PWD/.venv/bin/python"
cmake --build build --target ragent-file-worker -j 2
mkdir -p artifacts
image_context=$(mktemp -d "$project_dir/artifacts/sandbox-image-XXXXXX")
trap 'rm -rf -- "$image_context"' EXIT
python3 scripts/sandbox-image.py "$image_context"
docker --host "unix://$socket_path" build -f sandbox/Dockerfile \
    -t ragent-file-sandbox:v1 "$image_context"
echo '沙箱镜像已构建；启动服务时使用 --commands sandbox。'
