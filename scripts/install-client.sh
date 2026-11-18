#!/usr/bin/env bash
# 客户端和资源回收程序安装到用户目录，从任务目录启动时保留调用者cwd。
set -euo pipefail
umask 077
project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
prefix=${RAGENT_INSTALL_PREFIX:-$HOME/.local}
if [[ $prefix != /* || $prefix == *$'\n'* || $prefix == *'"'* || $prefix == *'%'* || $prefix == *'\'* ]]; then
    echo '安装路径必须是绝对路径，且不能含systemd控制字符。' >&2
    exit 1
fi
mkdir -p "$prefix/bin" "$prefix/libexec/ragent" "$HOME/.config/systemd/user"
staging=$(mktemp -d "$prefix/libexec/.ragent-install-XXXXXX")
trap 'rm -rf -- "$staging"' EXIT
cmake --install "$project_dir/build" --prefix "$staging"
# 同一文件系统原子替换，正在运行的进程继续持有原inode，不覆盖已打开的ELF。
for binary in agent ragent-observe ragent-policy ragent-reaper; do
    mv -f -- "$staging/libexec/ragent/$binary" "$prefix/libexec/ragent/$binary"
done
wrapper=$(mktemp "$prefix/bin/.ragent-XXXXXX")
printf '#!/usr/bin/env bash\nexec %q "$@"\n' "$prefix/libexec/ragent/agent" > "$wrapper"
chmod 0755 "$wrapper"
mv -f -- "$wrapper" "$prefix/bin/ragent"
unit="$HOME/.config/systemd/user/ragent-command-reaper.service"
cat > "$unit" <<UNIT
[Unit]
Description=RAGent command resource reaper
After=ragent-sandbox-docker.service
Requires=ragent-sandbox-docker.service
[Service]
ExecStart="$prefix/libexec/ragent/ragent-reaper"
Restart=on-failure
RestartSec=2
[Install]
WantedBy=default.target
UNIT
systemctl --user daemon-reload
systemctl --user enable ragent-command-reaper.service
systemctl --user restart ragent-command-reaper.service
printf '客户端已安装：%s/bin/ragent\n请将 %s/bin 加入 PATH，在任务目录运行 ragent。\n' "$prefix" "$prefix"
