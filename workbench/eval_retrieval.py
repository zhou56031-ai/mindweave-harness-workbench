"""检索质量评测：用标题当 query，看能否找回自己的章节。"""
from __future__ import annotations

from workbench.knowledge import get_index

# 从索引里挑 15 个章节标题作为 query，期望命中同一 (doc, heading)
def build_cases(limit: int = 15):
    index = get_index()
    seen, cases = set(), []
    for chunk in index.chunks:
        key = (chunk.doc_id, chunk.heading)
        if len(chunk.heading) < 6 or key in seen:
            continue
        seen.add(key)
        cases.append((chunk.heading, chunk.doc_id, chunk.heading))
        if len(cases) >= limit:
            break
    return cases


def evaluate(k: int = 3) -> dict:
    index = get_index()
    cases = build_cases()
    top1 = recall = 0
    mrr = 0.0
    for query, doc_id, heading in cases:
        hits = index.search(query, k)
        ranks = [i + 1 for i, h in enumerate(hits) if h.doc_id == doc_id and h.heading == heading]
        if ranks and ranks[0] == 1:
            top1 += 1
        if ranks:
            recall += 1
            mrr += 1.0 / ranks[0]
    n = len(cases)
    return {"n": n, "top1": top1 / n, "recall": recall / n, "mrr": mrr / n}


if __name__ == "__main__":
    print(evaluate())
