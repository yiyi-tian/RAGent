"""从本机构建的受信任二进制生成最小运行镜像上下文，不复制项目配置与密钥。"""

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

output = Path(sys.argv[1])
output.mkdir(parents=True, exist_ok=True)
manifest = {}
for source, destination in (
    (Path("build/ragent-file-worker"), Path("usr/local/bin/ragent-file-worker")),
    (Path("/usr/bin/sleep"), Path("usr/bin/sleep")),
):
    files = [(source, destination)]
    dependencies = subprocess.check_output(["ldd", str(source)], text=True)
    for path in re.findall(r"(/[^\s()]+)", dependencies):
        library = Path(path)
        if library.is_file():
            files.append((library, Path(path.lstrip("/"))))
    for original, relative in files:
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(original, target, follow_symlinks=True)
        target.chmod(0o755 if os.access(original, os.X_OK) else 0o644)
        manifest[str(relative)] = hashlib.sha256(target.read_bytes()).hexdigest()
for package in ("libc6", "libjson-c5", "coreutils"):
    copyright_file = Path("/usr/share/doc") / package / "copyright"
    target = output / "usr/share/doc" / package / "copyright"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(copyright_file, target)
(output / "workspace").mkdir(exist_ok=True)
(output / "runtime-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
# 部署脚本使用umask077；镜像中的非root用户仍需遍历运行库目录。
for directory in output.rglob("*"):
    if directory.is_dir():
        directory.chmod(0o755)
