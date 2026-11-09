"""④ 重排 A/B：混合检索 vs 完整证据链（含图谱/重排）。"""

from __future__ import annotations

import asyncio
import time

from sqlalchemy import select

from app.db.db_config import AsyncSessionLocal
from app.models.user_model import User
from app.rag.agentic_service import collect_evidence
from app.rag.hybrid_retrieval import search as hybrid_search
from app.rag.evidence_selection import rerank as rerank_hits

CASES = [
    ("缓存击穿怎么处理", "缓存"),
    ("RRF 融合为什么只比较名次", "缓存"),
    ("混合检索为什么需要重排", "缓存"),
    ("知识图谱的实体抽取怎么做", "图谱"),
    ("审批流怎么防止重复执行", "审批"),
    ("笔记的复习间隔怎么定", "回顾"),
]


async def main() -> None:
    async with AsyncSessionLocal() as db:
        user = (await db.execute(select(User).where(User.username == "demo"))).scalars().first()
        uid = user.uuid if user else ""
        print("user", uid)
        rows = []
        for query, keyword in CASES:
            started = time.perf_counter()
            base = await hybrid_search(db, uid, query)
            base_ms = (time.perf_counter() - started) * 1000
            started = time.perf_counter()
            rr = await rerank_hits(query, base)
            rr_ms = (time.perf_counter() - started) * 1000
            started = time.perf_counter()
            full, _ctx, _ans, _web = await collect_evidence(db, uid, query)
            full_ms = (time.perf_counter() - started) * 1000
            base_hit = any(keyword in h.title for h in base[:3])
            rr_hit = any(keyword in h.title for h in rr[:3])
            full_hit = any(keyword in h.title for h in full[:3])
            rows.append((query, base_hit, rr_hit, full_hit, round(base_ms), round(rr_ms), round(full_ms)))
            print(f"{query:<22} 混合={base_hit} 重排={rr_hit} 完整={full_hit} 延迟 {base_ms:.0f}/{rr_ms:.0f}/{full_ms:.0f}ms")
        base_recall = sum(1 for r in rows if r[1]) / len(rows)
        rr_recall = sum(1 for r in rows if r[2]) / len(rows)
        full_recall = sum(1 for r in rows if r[3]) / len(rows)
        print(f"Recall@3：混合 {base_recall:.0%} ｜ 重排 {rr_recall:.0%} ｜ 完整 {full_recall:.0%}")
        print(f"平均延迟：混合 {sum(r[4] for r in rows)/len(rows):.0f}ms ｜ 重排 {sum(r[5] for r in rows)/len(rows):.0f}ms ｜ 完整 {sum(r[6] for r in rows)/len(rows):.0f}ms")


if __name__ == "__main__":
    asyncio.run(main())
