"""从同一手册章节补齐相邻段落，保留检索锚点及相关块 ID。"""

from collections import defaultdict

from .chunking import distinct_matches


class ManualContext:
    """构建后只读，可由检索线程共享；重新入库后需重建。"""

    def __init__(self, chunks):
        self.documents = defaultdict(list)
        self.entries = defaultdict(list)
        self.chunks = {chunk["chunk_id"]: chunk for chunk in chunks}
        self.parents = {}
        self.multigrain = False
        for chunk in chunks:
            if chunk.get("document_sha256"):
                self.documents[
                    (chunk["source"], chunk["document_sha256"], chunk.get("chunking_version"))
                ].append(chunk)
                if "entry_start" in chunk:
                    identity = (chunk["source"], chunk["document_sha256"], chunk["entry_start"])
                    self.entries[identity].append(chunk)
        for entry in self.entries.values():
            entry.sort(key=lambda chunk: chunk["start"])
        for document in self.documents.values():
            document.sort(key=lambda chunk: chunk["start"])

        coarse = defaultdict(list)
        for chunk in chunks:
            if chunk.get("chunking_version") == "paragraph-v2":
                coarse[(chunk["source"], chunk["document_sha256"])].append(chunk)
        for chunk in chunks:
            if chunk.get("chunking_version") != "entry-v3":
                continue
            candidates = coarse[(chunk["source"], chunk["document_sha256"])]
            best = None
            best_overlap = 0
            for parent in candidates:
                overlap = min(chunk["end"], parent["end"]) - max(chunk["start"], parent["start"])
                if overlap > best_overlap:
                    best, best_overlap = parent, overlap
            if best is not None:
                self.parents[chunk["chunk_id"]] = best["chunk_id"]
        self.multigrain = bool(self.parents)

    def expand(self, match, max_chars=1800):
        if "entry_start" in match:
            return self.expand_entry(match, max_chars)
        document = self.documents.get(
            (match["source"], match.get("document_sha256"), match.get("chunking_version"))
        )
        if not document:
            return match
        position = next(
            i for i, chunk in enumerate(document) if chunk["chunk_id"] == match["chunk_id"]
        )
        selected = [document[position]]
        start, end = match["start"], match["end"]
        for neighbor in (position + 1, position - 1):
            if not 0 <= neighbor < len(document):
                continue
            chunk = document[neighbor]
            left, right = min(start, chunk["start"]), max(end, chunk["end"])
            if chunk["section"] == match["section"] and right - left <= max_chars:
                selected.append(chunk)
                start, end = left, right
        if len(selected) == 1:
            return match
        # 分块未保存的空白位置填空格；保留原文字符坐标和全部非空白内容。
        characters = [" "] * (end - start)
        selected.sort(key=lambda chunk: chunk["start"])
        for chunk in selected:
            left = chunk["start"] - start
            characters[left : left + len(chunk["body"])] = chunk["body"]
        context = "".join(characters)
        return {
            **match,
            "context_text": context,
            "context_chunk_ids": [chunk["chunk_id"] for chunk in selected],
            "text": f"{match['source']} — {match['section']}\n{context}",
        }

    def expand_entry(self, match, max_chars):
        entry = self.entries[(match["source"], match["document_sha256"], match["entry_start"])]
        position = next(
            i for i, chunk in enumerate(entry) if chunk["chunk_id"] == match["chunk_id"]
        )
        pieces = {position: match["body"]}
        remaining = max_chars - len(match["body"])
        if position != 0 and remaining > 8:
            head = entry[0]["body"][: min(500, remaining - 8)]
            pieces[0] = head
            remaining -= len(head) + 8
        for neighbor in (position + 1, position - 1):
            if neighbor in pieces or not 0 <= neighbor < len(entry):
                continue
            body = entry[neighbor]["body"]
            if len(body) + 8 <= remaining:
                pieces[neighbor] = body
                remaining -= len(body) + 8
        if len(pieces) == 1:
            return match
        fragments = []
        previous = None
        for index in sorted(pieces):
            if previous is not None:
                contiguous = index == previous + 1 and len(pieces[previous]) == len(
                    entry[previous]["body"]
                )
                fragments.append("\n\n" if contiguous else "\n[…]\n")
            fragments.append(pieces[index])
            previous = index
        body = "".join(fragments)
        return {
            **match,
            "context_text": body,
            "context_chunk_ids": [entry[index]["chunk_id"] for index in sorted(pieces)],
            "text": f"{match['source']} — {match['section']} — {match['entry']}\n{body}",
        }

    def select_multigrain(self, coarse, fine, limit=5, rank_offset=10, fine_weight=0.5):
        scores = defaultdict(float)
        matches = {}
        related = {}
        for rank, match in enumerate(coarse, 1):
            identity = match["chunk_id"]
            scores[identity] = 1 / (rank_offset + rank)
            matches[identity] = match
        for rank, match in enumerate(fine, 1):
            parent_id = self.parents.get(match["chunk_id"])
            if parent_id is None or parent_id in related:
                continue
            # 同一段落的多个短条目只投一票，避免长章节按条目数量占优。
            related[parent_id] = match
            scores[parent_id] += fine_weight / (rank_offset + rank)
            matches.setdefault(parent_id, self.chunks[parent_id])
        ordered = sorted(scores, key=lambda identity: (-scores[identity], identity))
        ranked = [{**matches[identity], "score": scores[identity]} for identity in ordered]
        selected = []
        seen = set()
        for match in distinct_matches(ranked, len(ranked)):
            item = self.expand(match)
            fine_match = related.get(match["chunk_id"])
            if fine_match is not None:
                head = self.entries[
                    (fine_match["source"], fine_match["document_sha256"], fine_match["entry_start"])
                ][0]
                ids = item.get("context_chunk_ids", [item["chunk_id"]])
                left = min(self.chunks[identity]["start"] for identity in ids)
                right = max(self.chunks[identity]["end"] for identity in ids)
                head_text = head["body"][:500]
                if not left <= head["start"] < head["start"] + len(head_text) <= right:
                    # 只补充与命中位置关联的条目开头，不拼接其他检索结果来凑覆盖。
                    body = head_text + "\n[…]\n" + match["body"]
                    item = {
                        **match,
                        "context_text": body,
                        "context_chunk_ids": [head["chunk_id"], match["chunk_id"]],
                        "text": f"{match['source']} — {match['section']}\n{body}",
                    }
            identity = tuple(item.get("context_chunk_ids", [item["chunk_id"]]))
            if identity in seen:
                continue
            seen.add(identity)
            selected.append(item)
            if len(selected) == limit:
                break
        return selected

    def select(self, matches, limit=5):
        selected = []
        seen_contexts = set()
        for match in distinct_matches(matches, len(matches)):
            expanded = self.expand(match)
            identity = tuple(expanded.get("context_chunk_ids", [expanded["chunk_id"]]))
            if identity in seen_contexts:
                continue
            seen_contexts.add(identity)
            selected.append(expanded)
            if len(selected) == limit:
                break
        return selected
