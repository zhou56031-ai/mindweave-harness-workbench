"""抽取式回答合成器：没有模型 Key 时，用检索到的原句回答。

规则：
1. 过滤代码、表格、路径，只留“人话”；
2. 结论取最相关片段的前两句；
3. 要点按查询词覆盖度取，每条来自不同片段；
4. 固定结构：结论 / 要点 / 来源，带 [n] 引用。
"""

from __future__ import annotations

import re

from workbench.knowledge import tokenize

_SENTENCE = re.compile(r"[^。！？!?\n]{8,200}[。！？!?]?")
_FENCE = re.compile(r"```.*?```", re.S)
_URL = re.compile(r"https?://\S+")
_BULLET = re.compile(r"^[\-\*•\d\.、\s]+")
_STOP = {"需要","怎么","么处","处理","如何","为什","什么","可以","一个","这个","哪些",
         "是否","以及","就是","没有","如果","因为","所以","但是","并且","进行","使用",
         "问题","时候","介绍","说明","了解","知道"}


def _clean(text: str) -> str:
    text = _FENCE.sub(" ", text)
    text = _URL.sub(" ", text)
    text = text.replace("`", "")
    return re.sub(r"\s+", " ", text).strip()


def _is_prose(sentence: str) -> bool:
    if "|" in sentence or "**" in sentence or sentence.strip().startswith(("#", ">", "!", "[")):
        return False
    if re.search(r"[{}\[\]<>]|self\.|def |==|=>|\(\)|\w+_\w+|/\w+/", sentence):
        return False
    cjk = sum(1 for ch in sentence if "\u4e00" <= ch <= "\u9fff")
    return cjk >= 6 and cjk / max(1, len(sentence)) >= 0.3


def _sentences(text: str) -> list[str]:
    result = []
    for raw in _SENTENCE.findall(_clean(text)):
        sentence = _BULLET.sub("", raw.strip())
        if sentence.startswith(("例如", "来源", "举个例子", "”", "”")):
            continue
        if len(sentence) >= 10 and _is_prose(sentence):
            result.append(sentence)
    return result


def _score(sentence: str, terms: set[str]) -> int:
    return sum(1 for term in terms if term in sentence)


def query_terms(query: str) -> set[str]:
    return {term for term in tokenize(query) if term not in _STOP}


def is_grounded(query: str, hits: list) -> bool:
    """资料是否真的覆盖了问题：既要分数够高，也要命中至少两个查询词。"""
    if not hits:
        return False
    terms = query_terms(query)
    top = hits[0]
    haystack = f"{top.title}{top.heading}{top.text}"
    matched = sum(1 for term in terms if term in haystack)
    return top.score >= 12 and matched >= 2


def compose_answer(query: str, hits: list) -> str:
    terms = query_terms(query)
    if not hits or not terms:
        return "知识库里没有检索到直接相关的内容。可以换个说法，或先导入相关资料。"

    top = hits[0].score
    strong = [h for h in hits if h.score >= 0.55 * top][:3]

    # 结论：优先取最相关片段里“最早出现的相关句”
    conclusion, conclusion_ref = None, 1
    for ref, hit in enumerate(hits[:2], 1):
        for sentence in _sentences(hit.text):
            if _score(sentence, terms) >= 2:
                conclusion, conclusion_ref = sentence, ref
                break
        if conclusion:
            break
    if conclusion is None:
        for ref, hit in enumerate(hits[:1], 1):
            sentences = _sentences(hit.text)
            if sentences:
                conclusion, conclusion_ref = sentences[0], ref
    if conclusion is None:
        return "知识库里没有检索到直接相关的内容。可以换个说法，或先导入相关资料。"

    # 要点：标题命中的片段直接取本节前两句；其余片段要求至少覆盖 1 个查询词
    points: list[tuple[int, str]] = []
    used: set[str] = {conclusion}

    def heading_match(hit) -> bool:
        return any(term in hit.heading or term in hit.title for term in terms)

    for ref, hit in enumerate(strong, 1):
        taken = 0
        for sentence in _sentences(hit.text):
            if sentence in used:
                continue
            if not heading_match(hit) and _score(sentence, terms) < 2:
                continue
            used.add(sentence)
            points.append((ref, sentence))
            taken += 1
            if taken >= 2 or len(points) >= 3:
                break
        if len(points) >= 3:
            break

    lines = ["**结论**", "", f"{conclusion}（[{conclusion_ref}]）", ""]
    if points:
        lines += ["**要点**", ""]
        lines += [f"- {sentence}（[{ref}]）" for ref, sentence in points]
        lines.append("")
    lines += ["**来源**", ""]
    used_refs = [conclusion_ref] + [ref for ref, _ in points]
    seen_refs: list[int] = []
    for ref in used_refs:
        if ref not in seen_refs:
            seen_refs.append(ref)
    for ref in sorted(seen_refs):
        hit = hits[ref - 1]
        lines.append(f"[{ref}] {hit.title} › {hit.heading}")
    lines += ["", "> 检索来自 MindWeave 文档索引；当前未接模型，回答由原句抽取合成。"]
    return "\n".join(lines)
