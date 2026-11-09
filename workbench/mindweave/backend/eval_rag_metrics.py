"""业务集检索评测：Hit@5 / 候选缺失 / 排序缺失 / 重排效果 / P95。"""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path

from sqlalchemy import select

from app.db.db_config import AsyncSessionLocal
from app.models.user_model import User
from app.rag.hybrid_retrieval import search as hybrid_search
from app.rag.evidence_selection import rerank

DATASET = Path(__file__).resolve().parents[1] / "eval" / "dataset" / "test.jsonl"


def norm(text: str) -> str:
    text = text.replace("*", "").replace("`", "").replace(" ", "").replace("\n", "")
    return re.sub(r"[，。；：、！？,.!?;:()（）]", "", text)


async def main() -> None:
    cases = [json.loads(line) for line in DATASET.read_text(encoding="utf-8").splitlines() if line.strip()]
    cases = [c for c in cases if c.get("answerable")]
    async with AsyncSessionLocal() as db:
        user = (await db.execute(select(User).where(User.username == "demo"))).scalars().first()
        uid = user.uuid
        stats = {"n": len(cases), "hit5": 0, "hit5_rerank": 0, "cand_miss": 0, "rank_miss": 0}
        lat_hybrid, lat_rerank = [], []
        for case in cases:
            quote = norm(case["quote"])
            started = time.perf_counter()
            hits = await hybrid_search(db, uid, case["question"])
            lat_hybrid.append((time.perf_counter() - started) * 1000)
            snippets = [norm(h.snippet) for h in hits]
            in_candidates = any(quote in s for s in snippets)
            in_top5 = any(quote in s for s in snippets[:5])
            if in_top5:
                stats["hit5"] += 1
            elif in_candidates:
                stats["rank_miss"] += 1
            else:
                stats["cand_miss"] += 1

            started = time.perf_counter()
            ranked = await rerank(case["question"], hits)
            lat_rerank.append((time.perf_counter() - started) * 1000)
            if any(quote in norm(h.snippet) for h in ranked[:5]):
                stats["hit5_rerank"] += 1

    p95 = lambda xs: sorted(xs)[max(0, int(len(xs) * 0.95) - 1)]
    print(json.dumps({
        "cases": stats["n"],
        "hit@5": round(stats["hit5"] / stats["n"], 4),
        "hit@5_after_rerank": round(stats["hit5_rerank"] / stats["n"], 4),
        "candidate_miss_rate": round(stats["cand_miss"] / stats["n"], 4),
        "ranking_miss_rate": round(stats["rank_miss"] / stats["n"], 4),
        "hybrid_p50_ms": round(sorted(lat_hybrid)[len(lat_hybrid) // 2], 1),
        "hybrid_p95_ms": round(p95(lat_hybrid), 1),
        "rerank_p95_ms": round(p95(lat_rerank), 1),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
