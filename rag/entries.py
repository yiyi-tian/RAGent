"""按选项、子命令和段落建立独立检索单元，保留所属条目的语境。"""

import hashlib
import re
import uuid

from .chunking import NAMESPACE

VERSION = "entry-v3"
ENTRY_OPTION = re.compile(r"^\s{3,8}(?:--?\S+|[A-Za-z]\s{2,})")


def entry_chunks(manual, target=500, limit=900):
    text = manual["text"]
    digest = hashlib.sha256(text.encode()).hexdigest()
    lines = list(re.finditer(r"[^\n]+", text))
    boundaries = [(0, "正文", "正文")]
    section = "正文"
    for index, line in enumerate(lines):
        value = line.group()
        if not value.strip():
            continue
        indentation = len(value) - len(value.lstrip())
        if indentation == 0 and len(value) < 100:
            section = value.strip()
            boundaries.append((line.start(), section, section))
            continue
        next_indent = 0
        for following_index in range(index + 1, len(lines)):
            following = lines[following_index]
            if following.group().strip():
                next_indent = len(following.group()) - len(following.group().lstrip())
                break
        option = ENTRY_OPTION.match(value) is not None
        subheading = 3 <= indentation <= 8 and next_indent > indentation
        if option or subheading:
            boundaries.append((line.start(), section, value.strip()))
    unique = {}
    for start, section, title in boundaries:
        unique[start] = (section, title)
    boundaries = [(start, *unique[start]) for start in sorted(unique)]
    chunks = []
    paragraph_pattern = re.compile(r"\S[\s\S]*?(?=\n\s*\n|\Z)")
    for index, (entry_start, section, title) in enumerate(boundaries):
        entry_end = boundaries[index + 1][0] if index + 1 < len(boundaries) else len(text)
        pieces = []
        for paragraph in paragraph_pattern.finditer(text, entry_start, entry_end):
            start, end = paragraph.span()
            while end - start > limit:
                boundary = text.rfind("\n", start, start + limit)
                if boundary <= start:
                    boundary = text.rfind(" ", start, start + limit)
                if boundary <= start:
                    boundary = start + limit
                pieces.append((start, boundary))
                start = boundary
                while start < end and text[start].isspace():
                    start += 1
            if start < end:
                pieces.append((start, end))
        ranges = []
        for start, end in pieces:
            if ranges and end - ranges[-1][0] <= target:
                ranges[-1] = (ranges[-1][0], end)
            else:
                ranges.append((start, end))
        for start, end in ranges:
            body = text[start:end]
            decoration = re.search(r"[A-Z][A-Z0-9_.+-]*\(1\)", title)
            edge = entry_start == 0 or entry_end == len(text)
            if decoration and edge and body.strip() == title:
                continue
            identity = f"{VERSION}:{manual['source']}:{digest}:{start}:{end}"
            chunk = {
                key: manual[key]
                for key in ("source", "language", "package", "package_version", "license_file")
            }
            chunk.update(
                {
                    "chunk_id": str(uuid.uuid5(NAMESPACE, identity)),
                    "chunking_version": VERSION,
                    "section": section,
                    "entry": title,
                    "entry_start": entry_start,
                    "entry_end": entry_end,
                    "start": start,
                    "end": end,
                    "body": body,
                    "text": f"{manual['source']} — {section} — {title}\n{body}",
                    "document_sha256": digest,
                    "content_sha256": hashlib.sha256(body.encode()).hexdigest(),
                }
            )
            chunks.append(chunk)
    return chunks
