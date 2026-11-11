"""按手册段落组织 v2 分块，保留可跨版本核对的原文位置。"""

import hashlib
import re
import uuid

VERSION = "paragraph-v2"
NAMESPACE = uuid.UUID("41e596b2-0699-45b6-b4eb-8b3284b18b72")


def manual_chunks(manual, target=700, limit=1000, overlap=200):
    if not 0 <= overlap < target <= limit:
        raise ValueError("分块参数必须满足 0 <= overlap < target <= limit")
    text = manual["text"]
    digest = hashlib.sha256(text.encode()).hexdigest()
    units = []
    headings = list(re.finditer(r"^[^\s\n][^\n]{0,98}$", text, re.MULTILINE))
    sections = []
    if not headings or headings[0].start() > 0:
        end = headings[0].start() if headings else len(text)
        sections.append((0, end, "正文"))
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        sections.append((heading.start(), end, heading.group().strip()))
    paragraph_pattern = re.compile(r"\S[\s\S]*?(?=\n\s*\n|\Z)")
    for section_start, section_end, heading in sections:
        for paragraph in paragraph_pattern.finditer(text, section_start, section_end):
            start, end = paragraph.span()
            while end - start > limit:
                boundary = text.rfind("\n", start, start + limit)
                if boundary <= start:
                    boundary = text.rfind(" ", start, start + limit)
                if boundary <= start:
                    boundary = start + limit
                units.append((start, boundary, heading))
                start = boundary
                while start < end and text[start].isspace():
                    start += 1
            if start < end:
                units.append((start, end, heading))
    ranges = []
    pending = []
    for unit in units:
        if pending and (unit[2] != pending[0][2] or unit[1] - pending[0][0] > target):
            ranges.append((pending[0][0], pending[-1][1], pending[0][2]))
            tail = pending[-1]
            pending = []
            if tail[2] == unit[2] and tail[1] - tail[0] <= overlap and unit[1] - tail[0] <= limit:
                pending.append(tail)
        pending.append(unit)
    if pending:
        ranges.append((pending[0][0], pending[-1][1], pending[0][2]))
    chunks = []
    for start, end, heading in ranges:
        body = text[start:end]
        identity = f"{VERSION}:{manual['source']}:{digest}:{start}:{end}"
        chunk = {
            key: manual[key]
            for key in ("source", "language", "package", "package_version", "license_file")
        }
        chunk.update(
            {
                "chunk_id": str(uuid.uuid5(NAMESPACE, identity)),
                "section": heading,
                "text": f"{manual['source']} — {heading}\n{body}",
                "body": body,
                "start": start,
                "end": end,
                "document_sha256": digest,
                "chunking_version": VERSION,
                "content_sha256": hashlib.sha256(body.encode()).hexdigest(),
            }
        )
        chunks.append(chunk)
    return chunks


def distinct_matches(matches, limit=5):
    selected = []
    seen = set()
    for match in matches:
        identity = (
            match["source"],
            match.get("entry", ""),
            " ".join(match.get("body", match["text"]).split()),
        )
        if identity in seen:
            continue
        duplicate = False
        for previous in selected:
            if match.get("document_sha256") and match.get("document_sha256") == previous.get(
                "document_sha256"
            ):
                intersection = min(match["end"], previous["end"]) - max(
                    match["start"], previous["start"]
                )
                length = min(match["end"] - match["start"], previous["end"] - previous["start"])
                if length > 0 and intersection / length >= 0.8:
                    duplicate = True
                    break
        if duplicate:
            continue
        seen.add(identity)
        selected.append(match)
        if len(selected) == limit:
            break
    return selected
