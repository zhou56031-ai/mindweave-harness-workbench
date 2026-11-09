"""SciFact (BEIR) 检索评测：BM25 / 向量 / RRF 混合，Recall@5、Hit@5、归因、P95。"""

from __future__ import annotations

import json
import math
import re
import time
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

DATA = Path(__file__).resolve().parent / "eval_data" / "scifact"
MODEL = "BAAI/bge-small-en-v1.5"
TOKEN = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    return TOKEN.findall(text.lower())


def load() -> tuple[list[dict], dict[str, str], dict[str, list[str]]]:
    corpus = [json.loads(line) for line in (DATA / "corpus.jsonl").read_text().splitlines() if line.strip()]
    queries = {q["_id"]: q["text"] for q in (json.loads(l) for l in (DATA / "queries.jsonl").read_text().splitlines() if l.strip())}
    qrels: dict[str, list[str]] = defaultdict(list)
    for line in (DATA / "qrels" / "test.tsv").read_text().splitlines()[1:]:
        qid, doc_id, score = line.split("\t")
        if int(score) > 0:
            qrels[qid].append(doc_id)
    return corpus, queries, qrels


class BM25:
    def __init__(self, docs: list[list[str]], k1: float = 1.2, b: float = 0.75) -> None:
        self.k1, self.b = k1, b
        self.docs = docs
        self.doc_len = np.array([len(d) for d in docs], dtype=np.float32)
        self.avg = float(self.doc_len.mean())
        self.tf = [Counter(d) for d in docs]
        df: Counter = Counter()
        for d in docs:
            df.update(set(d))
        n = len(docs)
        self.idf = {t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}

    def score(self, query: list[str]) -> np.ndarray:
        scores = np.zeros(len(self.docs), dtype=np.float32)
        for term in set(query):
            idf = self.idf.get(term)
            if idf is None:
                continue
            for i, tf in enumerate(self.tf):
                f = tf.get(term)
                if not f:
                    continue
                denom = f + self.k1 * (1 - self.b + self.b * self.doc_len[i] / self.avg)
                scores[i] += idf * f * (self.k1 + 1) / denom
        return scores


def rrf(rank_lists: list[list[int]], k: int = 60) -> list[int]:
    scores: dict[int, float] = defaultdict(float)
    for ranks in rank_lists:
        for rank, idx in enumerate(ranks, 1):
            scores[idx] += 1.0 / (k + rank)
    return [i for i, _ in sorted(scores.items(), key=lambda x: -x[1])]


def main() -> None:
    corpus, queries, qrels = load()
    docs = [tokenize(f"{d['title']} {d['text']}") for d in corpus]
    bm25 = BM25(docs)
    print(f"语料 {len(corpus)} 篇，查询 {len(qrels)} 条", flush=True)

    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer(MODEL)
    texts = [f"{d['title']}. {d['text'][:1200]}" for d in corpus]
    started = time.perf_counter()
    doc_vecs = model.encode(texts, batch_size=64, normalize_embeddings=True, show_progress_bar=False)
    print(f"向量编码完成 {time.perf_counter()-started:.1f}s", flush=True)

    metrics = {name: {"recall": 0.0, "hit": 0, "cand_miss": 0, "rank_miss": 0, "lat": []} for name in ("bm25", "dense", "hybrid")}
    for qid, query in queries.items():
        if qid not in qrels:
            continue
        relevant = set(qrels[qid])
        tokens = tokenize(query)
        t0 = time.perf_counter()
        b_scores = bm25.score(tokens)
        b_rank = np.argsort(-b_scores)[:100].tolist()
        t_b = (time.perf_counter() - t0) * 1000

        t0 = time.perf_counter()
        q_vec = model.encode([query], normalize_embeddings=True, show_progress_bar=False)[0]
        d_scores = doc_vecs @ q_vec
        d_rank = np.argsort(-d_scores)[:100].tolist()
        t_d = (time.perf_counter() - t0) * 1000

        t0 = time.perf_counter()
        h_rank = rrf([b_rank, d_rank])
        t_h = (time.perf_counter() - t0) * 1000

        for name, rank, latency in (("bm25", b_rank, t_b), ("dense", d_rank, t_d), ("hybrid", h_rank, t_h)):
            top = rank[:5]
            ids = [corpus[i]["_id"] for i in top]
            hits = len(relevant & set(ids))
            metrics[name]["recall"] += hits / len(relevant)
            if hits:
                metrics[name]["hit"] += 1
            elif relevant & {corpus[i]["_id"] for i in rank}:
                metrics[name]["rank_miss"] += 1
            else:
                metrics[name]["cand_miss"] += 1
            metrics[name]["lat"].append(latency)

    n = len(qrels)
    out = {}
    for name, m in metrics.items():
        lat = sorted(m["lat"])
        out[name] = {
            "recall@5": round(m["recall"] / n, 4),
            "hit@5": round(m["hit"] / n, 4),
            "candidate_miss_rate": round(m["cand_miss"] / n, 4),
            "ranking_miss_rate": round(m["rank_miss"] / n, 4),
            "p50_ms": round(lat[n // 2], 1),
            "p95_ms": round(lat[max(0, int(n * 0.95) - 1)], 1),
        }
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
