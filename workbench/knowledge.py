"""离线知识索引：直接读 MindWeave 的 Markdown，不依赖 MySQL/Neo4j。

这不是 MindWeave 完整 RAG 的替代，而是一条可跑的垂直切片：
用它证明"合并后的运行时可以调用知识能力"，并把引用带到回答里。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path

DOCS_ROOT = Path(__file__).resolve().parent / "mindweave" / "docs"
UPLOADS = Path(__file__).resolve().parent / "mindweave" / "demo-docs" / "uploads"
DEMO_DOCS = Path(__file__).resolve().parent / "mindweave" / "demo-docs"

_ASCII = re.compile(r"[a-zA-Z0-9_]+")
_CJK = re.compile(r"[\u4e00-\u9fff]")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")


def chunk_title(index: int) -> str:
    return _CHUNK_REFS[index][0]


def chunk_heading(index: int) -> str:
    return _CHUNK_REFS[index][1]


def tokenize(text: str) -> list[str]:
    """英文按词、中文按二元组；对中文检索来说，二元组比单字更稳。"""
    lowered = text.lower()
    words = _ASCII.findall(lowered)
    chars = _CJK.findall(lowered)
    bigrams = ["".join(chars[i:i + 2]) for i in range(len(chars) - 1)]
    return words + bigrams


@dataclass(frozen=True)
class Chunk:
    doc_id: str
    title: str
    heading: str
    chunk_id: str
    text: str


@dataclass(frozen=True)
class Hit:
    doc_id: str
    title: str
    heading: str
    chunk_id: str
    text: str
    score: float


class KnowledgeIndex:
    """标题感知的切片 + BM25 检索。"""

    def __init__(self, roots: tuple[Path, ...] = (DOCS_ROOT, DEMO_DOCS)) -> None:
        self.chunks: list[Chunk] = []
        self._paths: dict[str, Path] = {}
        self._doc_tokens: list[list[str]] = []
        self._df: dict[str, int] = {}
        global _CHUNK_REFS
        _CHUNK_REFS = []
        self._build(roots)
        self._avg_len = sum(len(t) for t in self._doc_tokens) / max(1, len(self._doc_tokens))

    # ---------- 建索引 ----------
    def _build(self, roots: tuple[Path, ...]) -> None:
        for root in roots:
            if not root.exists():
                continue
            for path in sorted(root.rglob("*.md")):
                if any(part.startswith(".") for part in path.parts):
                    continue
                self._add_document(path)

    def _add_document(self, path: Path) -> None:
        raw = path.read_text(encoding="utf-8", errors="ignore")
        doc_id = path.stem
        self._paths[doc_id] = path
        title = doc_id
        heading = ""
        buffer: list[str] = []
        seq = 0

        def flush() -> None:
            nonlocal seq
            body = "\n".join(buffer).strip()
            if not body:
                return
            # 过长的段落切成块，避免单块过大
            for start in range(0, len(body), 800):
                piece = body[start:start + 800].strip()
                if not piece:
                    continue
                seq += 1
                chunk = Chunk(doc_id, title, heading or title, f"{doc_id}#{seq}", piece)
                self.chunks.append(chunk)
                _CHUNK_REFS.append((title, chunk.heading))
                tokens = tokenize(piece)
                self._doc_tokens.append(tokens)
                for token in set(tokens):
                    self._df[token] = self._df.get(token, 0) + 1

        for line in raw.splitlines():
            match = _HEADING.match(line)
            if match:
                flush()
                buffer = []
                level = len(match.group(1))
                if level == 1 and title == doc_id:
                    title = match.group(2).strip() or doc_id
                heading = match.group(2).strip()
                continue
            buffer.append(line)
        flush()

    # ---------- 检索 ----------
    def _idf(self, token: str) -> float:
        n = max(1, len(self.chunks))
        df = self._df.get(token, 0)
        return math.log(1 + (n - df + 0.5) / (df + 0.5))

    def search(self, query: str, top_k: int = 3, include_guides: bool = False) -> list[Hit]:
        terms = tokenize(query)
        if not terms:
            return []
        k1, b = 1.5, 0.75
        scored: list[tuple[float, int]] = []
        for index, tokens in enumerate(self._doc_tokens):
            if not tokens:
                continue
            doc = self.chunks[index].doc_id
            # 默认只用“产品/知识”文档回答；实现笔记（数字/Q 开头）只进知识库列表
            if not include_guides and (doc[:1].isdigit() or doc.startswith("Q")):
                continue
            length = len(tokens)
            counts: dict[str, int] = {}
            for token in tokens:
                counts[token] = counts.get(token, 0) + 1
            score = 0.0
            for term in set(terms):
                tf = counts.get(term, 0)
                if not tf:
                    continue
                norm = tf * (k1 + 1) / (tf + k1 * (1 - b + b * length / self._avg_len))
                score += self._idf(term) * norm
            # 标题/章节命中加权：让“缓存击穿”这类问题优先落到同名章节
            heading_hits = sum(
                1 for term in set(terms)
                if term in chunk_title(index) or term in chunk_heading(index)
            )
            score += 2.5 * heading_hits
            # 覆盖度：命中查询词越多越可信，避免只靠一个高频词胜出
            matched = sum(1 for term in set(terms) if counts.get(term))
            score += 2.0 * matched / max(1, len(set(terms)))
            # 文档先验：规范文档优先，实现笔记降权
            doc_id = self.chunks[index].doc_id
            if doc_id in CANONICAL_DOCS:
                score += 4.0
            elif doc_id[:1].isdigit():
                score -= 3.0
            if score > 0:
                scored.append((score, index))
        scored.sort(key=lambda item: item[0], reverse=True)
        hits: list[Hit] = []
        seen: set[str] = set()
        per_doc: dict[str, int] = {}
        for score, index in scored:
            chunk = self.chunks[index]
            if chunk.chunk_id in seen:
                continue
            if per_doc.get(chunk.doc_id, 0) >= 2:
                continue
            per_doc[chunk.doc_id] = per_doc.get(chunk.doc_id, 0) + 1
            seen.add(chunk.chunk_id)
            hits.append(Hit(chunk.doc_id, chunk.title, chunk.heading,
                            chunk.chunk_id, chunk.text, round(score, 3)))
            if len(hits) >= top_k:
                break
        return hits

    def add_file(self, path: Path) -> int:
        """把一份新文档加入索引，返回新增切片数。"""
        before = len(self.chunks)
        self._add_document(path)
        if self._doc_tokens:
            self._avg_len = sum(len(t) for t in self._doc_tokens) / len(self._doc_tokens)
        return len(self.chunks) - before

    def reload(self) -> None:
        """清空并重建索引（删除文档后使用）。"""
        global _CHUNK_REFS
        _CHUNK_REFS = []
        self.chunks.clear()
        self._paths.clear()
        self._doc_tokens.clear()
        self._df.clear()
        self._build((DOCS_ROOT, DEMO_DOCS))
        self._avg_len = sum(len(t) for t in self._doc_tokens) / max(1, len(self._doc_tokens))

    def read(self, doc_id: str) -> str | None:
        path = self._paths.get(doc_id)
        if path is None:
            return None
        return path.read_text(encoding="utf-8", errors="ignore")

    def stats(self) -> dict[str, int]:
        return {"documents": len(self._paths), "chunks": len(self.chunks)}


CANONICAL_DOCS = {"缓存与检索", "design-system", "AI-Agent功能全景说明"}

_CHUNK_REFS: list[tuple[str, str]] = []
_INDEX: KnowledgeIndex | None = None


def get_index() -> KnowledgeIndex:
    global _INDEX
    if _INDEX is None:
        _INDEX = KnowledgeIndex()
    return _INDEX
