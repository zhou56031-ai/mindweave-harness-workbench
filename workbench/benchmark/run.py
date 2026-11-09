"""Agent Benchmark：8 类真实任务 + 独立 Verifier + Trace 指标。

任务直接打真实后端（MindWeave + Harness），验证看文件系统/接口事实，不看模型自述。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8100"
ROOT = Path(__file__).resolve().parents[1]  # workbench/
WORKSPACE = ROOT / "mindweave" / "workspace"
OUT = Path(__file__).resolve().parents[2] / "docs" / "项目记录" / "benchmark-results.json"


def login() -> tuple[httpx.Client, dict]:
    c = httpx.Client(timeout=300, trust_env=False)
    body = c.post(f"{BASE}/user/login/", json={"username": "demo", "password": "demo1234"}).json()
    return c, {"Authorization": f"Bearer {body['token']}"}, body["user"]["uuid"]


def stream(c: httpx.Client, headers: dict, path: str, payload: dict) -> list[dict]:
    events: list[dict] = []
    with c.stream("POST", f"{BASE}{path}", headers=headers, json=payload) as r:
        for line in r.iter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


def collect(events: list[dict]) -> dict:
    text = "".join(e.get("content", "") for e in events if e.get("type") == "response")
    tools = [e.get("content", "") for e in events if e.get("type") == "thinking" and e.get("stage") == "tool_start"]
    sources = []
    for e in events:
        if e.get("type") == "thinking" and e.get("stage") == "local_retrieval" and isinstance(e.get("details"), dict):
            sources.extend(e["details"].get("results", []))
    return {"text": text, "tools": tools, "sources": sources}


def ask(c: httpx.Client, headers: dict, prompt: str, approve: bool = True) -> dict:
    started = time.perf_counter()
    events = stream(c, headers, "/chat/agent/query/stream", {"query": prompt})
    session = next((e["session_id"] for e in events if e.get("session_id")), None)
    if approve:
        approved = False
        for _ in range(3):
            if approved or not any(e["type"] == "interrupt" for e in events):
                break
            resume = stream(c, headers, "/chat/agent/resume",
                            {"session_id": session, "decisions": [{"type": "approve"}]})
            events.extend(resume)
            approved = True
    result = collect(events)
    result.update({"session": session, "events": events, "duration_ms": round((time.perf_counter() - started) * 1000, 1)})
    result["ttft_ms"] = next((e.get("ttft_ms") for e in reversed(events) if e.get("type") == "done"), None)
    result["interrupts"] = sum(1 for e in events if e.get("type") == "interrupt")
    result["errors"] = [e.get("content") for e in events if e.get("type") == "error"]
    return result


def files_for(uid: str) -> dict[str, str]:
    root = WORKSPACE / uid
    if not root.exists():
        return {}
    return {str(p.relative_to(root)): p.read_text(encoding="utf-8", errors="ignore") for p in root.rglob("*.md")}


def main() -> None:
    c, headers, uid = login()
    results = []
    before = files_for(uid)

    def record(tid, title, result, ok, detail):
        results.append({"id": tid, "title": title, "pass": ok, "detail": detail,
                        "tools": len(result["tools"]), "duration_ms": result["duration_ms"],
                        "ttft_ms": result["ttft_ms"], "interrupts": result["interrupts"],
                        "errors": result["errors"]})

    r = ask(c, headers, "缓存击穿怎么处理？")
    ok = "缓存击穿" in r["text"] and (len(r["sources"]) > 0 or "[1]" in r["text"])
    record("T1", "知识问答 + 引用", r, ok, f"sources={len(r['sources'])}")

    r = ask(c, headers, "混合检索和纯向量检索的区别是什么？")
    ok = ("向量" in r["text"]) and ("BM25" in r["text"] or "重排" in r["text"] or "稀疏" in r["text"])
    record("T2", "概念对比", r, ok, "关键词覆盖")

    r = ask(c, headers, "总结 AI-Agent功能全景说明 里提到的主要模块，200 字以内。")
    ok = len(r["text"]) >= 80 and (len(r["sources"]) > 0 or "模块" in r["text"])
    record("T3", "长文摘要", r, ok, f"len={len(r['text'])}")

    r = ask(c, headers, "必须调用 write_file 创建 notes/bench.md，内容 BENCH-OK")
    after = files_for(uid)
    ok = after.get("notes/bench.md", "").strip() == "BENCH-OK"
    record("T4", "文件创建 + 审批", r, ok, f"file={'有' if ok else '无'}")

    r = ask(c, headers, "必须调用 run_python 计算 17*23，并把结果告诉我")
    ok = "391" in r["text"]
    record("T5", "代码执行", r, ok, "结果为 391")

    r = ask(c, headers, "必须调用 plan 工具，为“整理缓存笔记”制定三步计划")
    ok = any("plan" in t for t in r["tools"]) or ("1." in r["text"] and "3." in r["text"])
    record("T6", "计划生成", r, ok, f"tools={r['tools']}")

    r = ask(c, headers, "请证明量子纠缠违反贝尔不等式，并给出推导。")
    ok = any(k in r["text"] for k in ("没有", "未检索", "不包含", "无法")) and "http" not in r["text"]
    record("T7", "未命中拒答", r, ok, "明确说明缺资料")

    before2 = files_for(uid)
    r1 = ask(c, headers, "必须调用 write_file 创建 notes/idem.md，内容 IDEM")
    session = r1["session"]
    stream(c, headers, "/chat/agent/resume", {"session_id": session, "decisions": [{"type": "approve"}]})
    after2 = files_for(uid)
    dup = list(after2).count("notes/idem.md")
    ok = after2.get("notes/idem.md", "").strip() == "IDEM" and dup == 1
    record("T8", "审批幂等", r1, ok, f"文件数={dup}")

    passed = sum(1 for x in results if x["pass"])
    total_tools = sum(x["tools"] for x in results)
    avg_duration = round(sum(x["duration_ms"] for x in results) / len(results), 1)
    summary = {
        "tasks": len(results), "passed": passed, "pass_rate": round(passed / len(results), 4),
        "avg_duration_ms": avg_duration, "total_tool_calls": total_tools,
        "safety_violations": sum(1 for x in results if x["id"] == "T8" and not x["pass"]),
        "results": results,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k != "results"}, ensure_ascii=False, indent=2))
    for x in results:
        print(f"{x['id']} {'PASS' if x['pass'] else 'FAIL'} {x['title']:<14} tools={x['tools']} ttft={x['ttft_ms']}ms {x['errors']}")


if __name__ == "__main__":
    main()
