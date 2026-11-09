"""合并切片的最小工作台：检索、并行对比、崩溃恢复演示。

启动：
    ./.venv/bin/python -m workbench.web
然后打开 http://127.0.0.1:8090
"""

from __future__ import annotations

from time import perf_counter

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from workbench import demo
from workbench.knowledge import get_index
from workbench.tools import search_knowledge

app = FastAPI(title="Workbench Slice")


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return HTML


@app.get("/api/search")
def api_search(q: str, k: int = 5) -> dict:
    started = perf_counter()
    result = search_knowledge(q, max(1, min(8, k)))
    elapsed = (perf_counter() - started) * 1000
    hits = (result.data or {}).get("hits", [])
    return {"ms": round(elapsed, 2), "hits": hits, "index": get_index().stats()}


@app.get("/api/compare")
def api_compare() -> dict:
    result = demo.run_parallel(latency_ms=40)
    return {
        "serial_ms": result["serial"],
        "parallel_ms": result["parallel"],
        "speedup": result["speedup"],
        "latency_ms": result["latency_ms"],
    }


@app.get("/api/recover")
def api_recover() -> dict:
    return demo.run_recover()


HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>织识 × Hexgent · 合并切片</title>
<style>
  :root { --bg:#0f1115; --card:#171a21; --line:#262b36; --text:#e6e8ee; --muted:#8b93a7; --accent:#6ea8fe; --good:#3ddc97; }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--text); font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",sans-serif; }
  .wrap { max-width: 980px; margin: 0 auto; padding: 32px 20px 64px; }
  h1 { font-size: 26px; margin: 0 0 6px; }
  .sub { color: var(--muted); margin-bottom: 26px; }
  .card { background: var(--card); border: 1px solid var(--line); border-radius: 14px; padding: 20px; margin-bottom: 18px; }
  .card h2 { font-size: 16px; margin: 0 0 12px; font-weight: 600; }
  .row { display: flex; gap: 10px; }
  input { flex:1; background:#0b0d12; border:1px solid var(--line); color:var(--text); padding:12px 14px; border-radius:10px; font-size:15px; }
  input:focus { outline:none; border-color:var(--accent); }
  button { background:var(--accent); color:#0b0d12; border:0; padding:12px 18px; border-radius:10px; font-weight:600; cursor:pointer; }
  button.secondary { background:#222736; color:var(--text); border:1px solid var(--line); }
  button:disabled { opacity:.5; cursor:default; }
  .hint { color:var(--muted); font-size:13px; margin-top:10px; }
  .hit { border-top:1px solid var(--line); padding:14px 0; }
  .hit:first-child { border-top:0; }
  .hit .ref { color:var(--accent); font-weight:700; margin-right:6px; }
  .hit .meta { color:var(--muted); font-size:13px; margin-bottom:6px; }
  .hit .text { color:#c9cede; font-size:14px; }
  .badge { display:inline-block; background:#222736; border:1px solid var(--line); color:var(--muted); border-radius:999px; padding:3px 10px; font-size:12px; margin-left:8px; }
  .bars { display:grid; grid-template-columns: 90px 1fr 90px; gap:10px; align-items:center; margin-top:8px; }
  .bar { height: 22px; border-radius:6px; background:#2a3247; overflow:hidden; }
  .bar > i { display:block; height:100%; background:var(--accent); }
  .bar.slow > i { background:#ff9f6e; }
  .num { color:var(--muted); font-size:13px; text-align:right; }
  pre { background:#0b0d12; border:1px solid var(--line); border-radius:10px; padding:12px; color:#b9c2d6; font-size:13px; overflow:auto; }
  .ok { color: var(--good); }
  .foot { color:var(--muted); font-size:12px; margin-top:24px; }
</style>
</head>
<body>
<div class="wrap">
  <h1>织识 × Hexgent · 合并切片</h1>
  <div class="sub">知识检索来自 MindWeave（117 篇文档 / 2,473 个切片）；调度与恢复来自 Hexgent。</div>

  <div class="card">
    <h2>① 检索（带引用）</h2>
    <div class="row">
      <input id="q" placeholder="例如：缓存击穿怎么处理？" value="缓存击穿怎么处理？" />
      <button onclick="search()">检索</button>
    </div>
    <div class="hint" id="searchMeta">输入问题，返回带文档与章节的片段。</div>
    <div id="hits"></div>
  </div>

  <div class="card">
    <h2>② 并行 vs 串行（同一批 3 个检索，每次模拟 40ms 远程 KB）</h2>
    <button class="secondary" id="cmpBtn" onclick="compare()">跑一次对比</button>
    <div id="cmp"></div>
  </div>

  <div class="card">
    <h2>③ 崩溃恢复（模拟 kill -9 后判定未闭合工具）</h2>
    <button class="secondary" id="recBtn" onclick="recover()">跑一次</button>
    <pre id="rec">点击上面的按钮。</pre>
  </div>

  <div class="foot">
    说明：这是合并切片，不是完整 MindWeave。完整版需要 MySQL / Redis / Neo4j 与模型 Key；
    本页用离线 BM25 近似检索，用来验证“运行时能驱动知识能力”和并行/恢复的收益。
  </div>
</div>

<script>
async function search() {
  const q = document.getElementById('q').value.trim();
  if (!q) return;
  const meta = document.getElementById('searchMeta');
  meta.textContent = '检索中…';
  const res = await fetch('/api/search?q=' + encodeURIComponent(q) + '&k=5').then(r => r.json());
  meta.textContent = `耗时 ${res.ms} ms ｜ 索引 ${res.index.documents} 篇 / ${res.index.chunks} 片 ｜ 命中 ${res.hits.length} 条`;
  document.getElementById('hits').innerHTML = res.hits.map(h => `
    <div class="hit">
      <div><span class="ref">[${h.ref}]</span><b>${escapeHtml(h.title)}</b> › ${escapeHtml(h.heading)}
        <span class="badge">${h.doc_id}</span></div>
      <div class="meta">chunk ${h.chunk_id} ｜ score ${h.score}</div>
      <div class="text">${escapeHtml(h.text).slice(0, 220)}…</div>
    </div>`).join('');
}

async function compare() {
  const btn = document.getElementById('cmpBtn');
  btn.disabled = true; btn.textContent = '跑…';
  const r = await fetch('/api/compare').then(x => x.json());
  const max = Math.max(r.serial_ms, r.parallel_ms);
  document.getElementById('cmp').innerHTML = `
    <div class="bars">
      <div>串行</div><div class="bar slow"><i style="width:${r.serial_ms/max*100}%"></i></div>
      <div class="num">${r.serial_ms} ms</div>
      <div>并行</div><div class="bar"><i style="width:${r.parallel_ms/max*100}%"></i></div>
      <div class="num">${r.parallel_ms} ms</div>
    </div>
    <div class="hint">加速 <b class="ok">${r.speedup}x</b>（3 个调用 × ${r.latency_ms}ms 模拟延迟）</div>`;
  btn.disabled = false; btn.textContent = '再跑一次';
}

async function recover() {
  const btn = document.getElementById('recBtn');
  btn.disabled = true; btn.textContent = '跑…';
  const r = await fetch('/api/recover').then(x => x.json());
  const lines = [
    `子进程退出码: ${r.exit_code}（73 = 模拟 kill -9）`,
    `恢复读到的记录: ${r.records} 条`,
    ...r.unclosed.map(u => `未闭合: ${u.call_id} / ${u.tool} → ${u.action}`),
    '结论：没有把未完成的写操作当成成功，也没有自动重试。'
  ];
  document.getElementById('rec').textContent = lines.join('\\n');
  btn.disabled = false; btn.textContent = '再跑一次';
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
</script>
</body>
</html>
"""


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8090, log_level="warning")
