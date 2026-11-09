"""SciFact 重排实验：在 RRF 候选上做 cross-encoder 重排，看 Recall@5 变化。"""

from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np

from eval_scifact import BM25, load, rrf, tokenize

DATA = Path(__file__).resolve().parent / "eval_data" / "scifact"
DENSE_MODEL = "BAAI/bge-small-en-v1.5"
RERANK_MODEL = "BAAI/bge-reranker-base"


def main() -> None:
    corpus, queries, qrels = load()
    docs = [tokenize(f"{d['title']} {d['text']}") for d in corpus]
    bm25 = BM25(docs)

    from sentence_transformers import SentenceTransformer, CrossEncoder
    dense = SentenceTransformer(DENSE_MODEL)
    texts = [f"{d['title']}. {d['text'][:1200]}" for d in corpus]
    doc_vecs = dense.encode(texts, batch_size=64, normalize_embeddings=True, show_progress_bar=False)
    reranker = CrossEncoder(RERANK_MODEL)

    before = after = 0
    lat = []
    n = 0
    for qid, query in queries.items():
        if qid not in qrels:
            continue
        relevant = set(qrels[qid])
        n += 1
        b_rank = np.argsort(-bm25.score(tokenize(query)))[:100].tolist()
        q_vec = dense.encode([query], normalize_embeddings=True, show_progress_bar=False)[0]
        d_rank = np.argsort(-(doc_vecs @ q_vec))[:100].tolist()
        cand = rrf([b_rank, d_rank])[:20]

        top5 = [corpus[i]["_id"] for i in cand[:5]]
        if relevant & set(top5):
            before += 1

        started = time.perf_counter()
        pairs = [(query, f"{corpus[i]['title']}. {corpus[i]['text'][:1200]}") for i in cand]
        scores = reranker.predict(pairs, show_progress_bar=False)
        order = np.argsort(-np.asarray(scores))[:5]
        reranked_ids = [corpus[cand[i]]["_id"] for i in order]
        lat.append((time.perf_counter() - started) * 1000)
        if relevant & set(reranked_ids):
            after += 1

    lat.sort()
    print(json.dumps({
        "queries": n,
        "hit@5_before_rerank": round(before / n, 4),
        "hit@5_after_rerank": round(after / n, 4),
        "delta": round((after - before) / n, 4),
        "rerank_p50_ms": round(lat[n // 2], 1),
        "rerank_p95_ms": round(lat[max(0, int(n * 0.95) - 1)], 1),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
