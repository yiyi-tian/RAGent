"""读取发行版安装的手册，生成带来源的稳定分块。"""

import hashlib
import os
from pathlib import Path
import re
import subprocess
import uuid

DEFAULT_MANUALS = ("passwd", "man", "ls", "grep", "chmod", "ps", "gpasswd", "systemctl")
MAN_ROOT = Path("/usr/share/man")
NAMESPACE = uuid.UUID("41e596b2-0699-45b6-b4eb-8b3284b18b72")


def _run(command, **kwargs):
    try:
        return subprocess.run(
            command, check=True, capture_output=True, text=True, timeout=15, **kwargs
        ).stdout
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError(f"无法处理手册：{command[0]}") from error


def find_manual(name, root=MAN_ROOT):
    if not re.fullmatch(r"[A-Za-z0-9_.+-]+", name):
        raise ValueError("手册名称格式错误")
    for locale, language in (("zh_CN", "zh_CN"), ("", "en")):
        directory = root / locale / "man1"
        for suffix in (".1.gz", ".1"):
            candidate = directory / (name + suffix)
            if candidate.is_file():
                return candidate, language
    raise ValueError(f"未找到手册：{name}(1)")


def package_info(path):
    output = _run(["dpkg-query", "-S", str(path)]).strip()
    package = output.split(": ", 1)[0].split(", ")[0]
    version = _run(["dpkg-query", "-W", "-f=${Version}", package]).strip()
    license_path = Path("/usr/share/doc") / package / "copyright"
    if not version or not license_path.is_file():
        raise ValueError(f"无法确定手册软件包版本或许可：{package}")
    return package, version, str(license_path)


def render_manual(path):
    environment = os.environ.copy()
    environment.update({"MANWIDTH": "80", "MANPAGER": "cat", "PAGER": "cat", "LC_ALL": "C.UTF-8"})
    raw = _run(["man", "-l", str(path)], env=environment)
    # man 的粗体通过退格叠印；去掉叠印字符，不改变正文语义。
    raw = re.sub(r"[^\n]\x08", "", raw).replace("\x08", "")
    return raw.replace("\x1b", "").strip()


def split_sections(text, limit=1000):
    if limit < 128:
        raise ValueError("分块长度过小")
    sections = []
    heading = "正文"
    lines = []
    for line in text.splitlines():
        if line and not line[0].isspace() and len(line) < 100:
            if lines:
                sections.append((heading, "\n".join(lines).strip()))
            heading, lines = line.strip(), []
        else:
            lines.append(line.rstrip())
    if lines:
        sections.append((heading, "\n".join(lines).strip()))
    for heading, section in sections:
        for offset in range(0, len(section), limit):
            chunk = section[offset : offset + limit].strip()
            if chunk:
                yield heading, chunk


def prepare_manual(name, root=MAN_ROOT):
    path, language = find_manual(name, root)
    package, version, license_path = package_info(path)
    chunks = []
    for index, (heading, text) in enumerate(split_sections(render_manual(path))):
        identity = f"{name}:1:{language}:{package}:{version}:{index}:{text}"
        chunk_id = str(uuid.uuid5(NAMESPACE, identity))
        chunks.append(
            {
                "chunk_id": chunk_id,
                "source": f"{name}(1)",
                "section": heading,
                "language": language,
                "package": package,
                "package_version": version,
                "license_file": license_path,
                "text": text,
                "content_sha256": hashlib.sha256(text.encode()).hexdigest(),
            }
        )
    if not chunks:
        raise ValueError(f"手册没有可导入文本：{name}(1)")
    return chunks
