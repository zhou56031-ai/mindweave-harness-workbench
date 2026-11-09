"""合并切片里的三个知识工具：检索、读文档、建笔记。

注册方式与 Hexgent 原生工具一致：进 TOOL_REGISTRY，并把 schema 追加到 prompt.TOOLS。
身份不由模型提供：工具签名里没有 userId/workspaceId。
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any

from mindweave_harness.prompt import TOOLS
from mindweave_harness.tool.tool_registry import TOOL_REGISTRY, RegisteredTool
from mindweave_harness.tool import tool_validation
from mindweave_harness.tool.tool_result import ToolHandlerResult

from workbench.knowledge import get_index

NOTES_DIR = Path(__file__).resolve().parent / "notes"

_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "search_knowledge",
        "description": "检索知识库，返回带文档与章节的引用片段。回答时必须使用 [n] 标注引用。不要传入 userId、workspaceId 等身份字段。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索问题或关键词。"},
                "top_k": {"type": "integer", "description": "返回片段数，1-5，默认 3。"},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "read_document",
        "description": "按 doc_id 读取一篇知识库文档的全文。",
        "parameters": {
            "type": "object",
            "properties": {"doc_id": {"type": "string", "description": "文档标识。"}},
            "required": ["doc_id"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "create_note",
        "description": "在个人工作区创建一篇 Markdown 笔记。写操作需要用户审批。",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "笔记标题。"},
                "body": {"type": "string", "description": "Markdown 正文。"},
            },
            "required": ["title", "body"],
            "additionalProperties": False,
        },
        "strict": True,
    },
]


def _simulated_latency() -> None:
    """模拟一次远程知识库调用，用来测量调度收益（不是真实 IO）。"""
    delay = int(os.environ.get("WORKBENCH_KB_LATENCY_MS", "0") or 0)
    if delay > 0:
        time.sleep(delay / 1000)


def search_knowledge(query: str, top_k: int = 3, context: Any = None) -> ToolHandlerResult:
    """检索并返回带引用的片段。"""
    _simulated_latency()
    hits = get_index().search(str(query), max(1, min(5, int(top_k))))
    if not hits:
        return ToolHandlerResult.success("未检索到相关内容。", {"hits": []})
    lines = ["检索结果（引用格式 [n] 文档 › 章节）："]
    payload = []
    for index, hit in enumerate(hits, 1):
        lines.append(f"[{index}] {hit.title} › {hit.heading}（{hit.doc_id}）")
        lines.append(hit.text[:300].replace("\n", " "))
        payload.append({
            "ref": index, "doc_id": hit.doc_id, "title": hit.title,
            "heading": hit.heading, "chunk_id": hit.chunk_id, "score": hit.score,
        })
    return ToolHandlerResult.success("\n".join(lines), {"hits": payload})


def read_document(doc_id: str, context: Any = None) -> ToolHandlerResult:
    """读取整篇文档。"""
    text = get_index().read(str(doc_id))
    if text is None:
        return ToolHandlerResult.failure("未找到该文档。", "NOT_FOUND")
    return ToolHandlerResult.success(text[:12_000], {"doc_id": doc_id, "chars": len(text)})


def create_note(title: str, body: str, context: Any = None) -> ToolHandlerResult:
    """创建笔记（写操作，由权限管线决定是否审批）。"""
    NOTES_DIR.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", str(title)).strip("-") or "note"
    path = NOTES_DIR / f"{slug}.md"
    suffix = 1
    while path.exists():
        suffix += 1
        path = NOTES_DIR / f"{slug}-{suffix}.md"
    path.write_text(f"# {title}\n\n{body}\n", encoding="utf-8")
    return ToolHandlerResult.success(f"已创建笔记：{path.name}", {"path": str(path)})


_TOOLS = [
    ("search_knowledge", search_knowledge, _SCHEMAS[0], True),
    ("read_document", read_document, _SCHEMAS[1], True),
    ("create_note", create_note, _SCHEMAS[2], False),
]


def install() -> None:
    """把知识工具装进 Hexgent 的注册表与模型可见 schema。"""
    names = {tool["name"] for tool in TOOLS}
    for name, function, schema, safe in _TOOLS:
        if name not in TOOL_REGISTRY:
            TOOL_REGISTRY[name] = RegisteredTool(
                function=function,
                requires_context=True,
                display_argument="query" if name == "search_knowledge" else None,
                concurrency_safe=safe,
                permission_capability="workspace_read" if name != "create_note" else "workspace_write",
                execution_domain="internal_runtime",
            )
        if name not in names:
            TOOLS.append(schema)
        tool_validation.TOOL_SCHEMAS[name] = schema["parameters"]
