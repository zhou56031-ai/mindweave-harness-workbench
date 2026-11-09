"""用 Hexgent 的模型配置（.env）真正生成回答。

优先走模型；不可用时由调用方回退到抽取式合成器。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"
_LOADED = False

SYSTEM = """你是「织识」知识工作台里的助手。
回答规则：
1. 优先依据给定资料回答，引用时用 [n] 标注（n 对应资料编号）。
2. 如果资料不能覆盖问题，明确说一句“资料库中没有相关内容”，然后用你自己的通用知识回答；
   通用知识部分不要标 [n]，也不要编造资料来源。
3. 不要拒绝回答：除非问题本身有害，否则总是给出可用答案。
4. 用中文，结构为：结论（1-2 句）→ 要点（2-4 条）→ 必要时一句适用边界。
5. 不要输出代码块或表格。"""


def _load() -> None:
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    if not _ENV_FILE.exists():
        return
    for line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def available() -> bool:
    _load()
    return bool(os.environ.get("OPENAI_API_KEY")) and bool(os.environ.get("MODEL_ID"))


def _client():
    from openai import OpenAI

    _load()
    return OpenAI(
        api_key=os.environ["OPENAI_API_KEY"],
        base_url=os.environ.get("OPENAI_BASE_URL") or None,
    )


def _context(hits: list) -> str:
    parts = []
    for index, hit in enumerate(hits, 1):
        parts.append(f"[{index}] {hit.title} › {hit.heading}\n{hit.text[:1200]}")
    return "\n\n".join(parts)


def stream_answer(query: str, hits: list, *, grounded: bool = True) -> Iterator[str] | None:
    """返回逐段文本；配置不可用时返回 None。"""
    if not available():
        return None
    client = _client()
    model = os.environ.get("MODEL_ID", "")
    if grounded:
        user = f"资料：\n{_context(hits)}\n\n问题：{query}"
    else:
        user = (f"资料库中没有检索到与问题相关的内容。\n问题：{query}\n"
                "请用通用知识回答，并先明确说明“资料库中没有相关内容”。")
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": user},
    ]
    stream = client.chat.completions.create(
        model=model, messages=messages, temperature=0.3, max_tokens=700, stream=True,
    )
    for chunk in stream:
        delta = chunk.choices[0].delta.content if chunk.choices else None
        if delta:
            yield delta


def footer() -> str:
    _load()
    return f"回答由 {os.environ.get('MODEL_ID', '模型')} 基于检索结果生成。"
