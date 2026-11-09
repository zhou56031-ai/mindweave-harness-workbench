"""MindWeave 兼容后端（合并切片）。

前端仍然是 MindWeave 原版 React 应用；这个服务在 8100 端口实现它需要的 API：
- 登录 / 用户
- 聊天流（SSE，事件格式与 MindWeave 一致）
- 会话列表 / 详情
- 知识库列表（来自 workbench 的离线索引）
- 笔记 CRUD（存本地 JSON）
- 复习 / 模板 / 图谱 / 用量：返回空结构，保证页面不崩

它不是 MindWeave 的完整后端（没有 MySQL/Neo4j/向量检索），
但足够让原版前端跑起来，并把“检索 + 引用 + 会话”走通。
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from fastapi import FastAPI, File, Request, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse

from workbench import llm
from workbench.answer import compose_answer, is_grounded
from workbench.knowledge import UPLOADS, get_index

DATA_DIR = Path(__file__).resolve().parent / ".data"
DATA_DIR.mkdir(exist_ok=True)
STORE_FILE = DATA_DIR / "store.json"

app = FastAPI(title="MindWeave-compatible backend (merged slice)")


# ---------------------------------------------------------------- 存储
def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load() -> dict[str, Any]:
    if STORE_FILE.exists():
        return json.loads(STORE_FILE.read_text(encoding="utf-8"))
    return {"sessions": {}, "messages": {}, "notes": [], "users": {}}


def _save(store: dict[str, Any]) -> None:
    STORE_FILE.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")


def _ok(data: Any = None, message: str = "ok") -> dict[str, Any]:
    return {"code": 200, "message": message, "data": data}


def _user(username: str) -> dict[str, Any]:
    return {
        "id": f"u_{username}", "user_id": f"u_{username}", "uuid": f"u_{username}",
        "username": username, "email": f"{username}@demo.local",
        "date_joined": _now(), "is_active": True,
    }


# ---------------------------------------------------------------- 用户
@app.post("/user/login/")
async def login(request: Request) -> dict[str, Any]:
    body = await request.json()
    username = str(body.get("username") or "demo")
    store = _load()
    store.setdefault("users", {})[username] = _user(username)
    _save(store)
    return {"message": "登录成功", "user": _user(username), "token": f"demo-token-{username}"}


@app.post("/user/register/")
async def register(request: Request) -> dict[str, Any]:
    body = await request.json()
    username = str(body.get("username") or "demo")
    return {"status": 200, "message": "注册成功", "user": _user(username),
            "token": f"demo-token-{username}"}


@app.post("/user/logout/")
async def logout() -> dict[str, Any]:
    return {"message": "已退出"}


@app.get("/user/detail/")
async def detail() -> dict[str, Any]:
    return {"success": True, "message": "ok", "data": _user("demo")}


# ---------------------------------------------------------------- 会话
@app.get("/chat/sessions/{user_id}")
async def sessions(user_id: str) -> dict[str, Any]:
    store = _load()
    items = sorted(store["sessions"].values(), key=lambda s: s["updated_at"], reverse=True)
    return _ok({"sessions": items})


@app.get("/chat/session/{session_id}")
async def session_detail(session_id: str) -> dict[str, Any]:
    store = _load()
    messages = store["messages"].get(session_id, [])
    history = [[m["role"], m["content"]] for m in messages if m["role"] in ("user", "assistant")]
    return _ok({"session_id": session_id, "history": history, "pending_run_id": None})


@app.delete("/chat/session/{session_id}")
async def session_delete(session_id: str) -> dict[str, Any]:
    store = _load()
    store["sessions"].pop(session_id, None)
    store["messages"].pop(session_id, None)
    _save(store)
    return _ok(None, "已删除")


@app.get("/chat/session/{session_id}/pending")
async def session_pending(session_id: str) -> dict[str, Any]:
    return _ok(None)


# ---------------------------------------------------------------- 聊天流
def _compose(query: str, hits: list[Any]) -> str:
    if not hits:
        return "知识库里没有检索到直接相关的内容。可以换个说法，或先导入相关资料。"
    lines = [f"根据知识库中检索到的 {len(hits)} 条资料，先给结论：\n"]
    first = hits[0]
    body = first.text.strip().replace("\n", " ")
    lines.append(body[:260] + ("…" if len(body) > 260 else ""))
    lines.append("\n\n依据：\n")
    for index, hit in enumerate(hits, 1):
        snippet = hit.text.strip().replace("\n", " ")[:110]
        lines.append(f"[{index}] {hit.title} › {hit.heading}：{snippet}…")
    lines.append("\n\n（当前为合并切片：检索来自 MindWeave 文档索引，回答由演示合成器生成。）")
    return "\n".join(lines)


@app.post("/chat/agent/query/stream")
async def query_stream(request: Request) -> StreamingResponse:
    body = await request.json()
    query = str(body.get("query") or "").strip()
    session_id = str(body.get("session_id") or "") or f"s_{uuid.uuid4().hex[:12]}"

    store = _load()
    session = store["sessions"].get(session_id)
    if session is None:
        session = {"id": session_id, "user_id": "u_demo", "title": query[:24] or "新会话",
                   "created_at": _now(), "updated_at": _now(), "pending_run_id": None}
        store["sessions"][session_id] = session
    store["messages"].setdefault(session_id, []).append(
        {"id": uuid.uuid4().hex, "role": "user", "content": query, "created_at": _now()})

    hits = get_index().search(query, 5)
    answer = compose_answer(query, hits)
    session["updated_at"] = _now()
    _save(store)

    def frame(payload: dict[str, Any]) -> str:
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    def generate() -> Iterator[str]:
        started = time.perf_counter()
        top_score = hits[0].score if hits else 0.0
        grounded = is_grounded(query, hits)
        coverage = "高" if top_score >= 20 else ("中" if top_score >= 12 else "低")
        model_name = llm.footer().replace("回答由 ", "").replace(" 基于检索结果生成。", "")

        # 1. 规划：先判断这是不是知识库内的问题
        yield frame({"type": "thinking", "stage": "agentic_plan",
                     "content": f"问题类型：{'知识库内问题' if grounded else '通用知识问题'}"
                                f"（最高相关度 {top_score:.1f}）",
                     "details": {"reason": "检索分数决定是否引用资料作答",
                                 "source": "local", "step_count": 1}})
        # 2. 工具调用：检索
        yield frame({"type": "thinking", "stage": "tool_start",
                     "content": f"调用 search_knowledge(query={query!r}, top_k=5)"})
        search_started = time.perf_counter()
        yield frame({"type": "thinking", "stage": "local_retrieval",
                     "content": f"检索知识库：{query}", "details": {"status": "searching"}})
        # 3. 证据
        yield frame({"type": "thinking", "stage": "local_retrieval",
                     "content": f"命中 {len(hits)} 条，最高分 {top_score:.1f}",
                     "details": {"status": "evidence", "results": [
                         {"id": h.chunk_id, "source": h.doc_id, "title": f"{h.title} › {h.heading}",
                          "score": h.score, "preview": h.text[:120]}
                         for h in hits]}})
        elapsed = (time.perf_counter() - search_started) * 1000
        yield frame({"type": "thinking", "stage": "tool_end",
                     "content": f"search_knowledge 完成：命中 {len(hits)} 条，耗时 {elapsed:.0f}ms"})
        # 4. 研判
        yield frame({"type": "thinking", "stage": "answerability",
                     "content": f"资料覆盖度：{coverage}；策略："
                                f"{'引用资料作答' if grounded else '资料库无相关内容，改用通用知识作答'}"})
        if grounded:
            selected = "、".join(f"{h.title} › {h.heading}" for h in hits[:3])
            yield frame({"type": "thinking", "stage": "evidence_fusion",
                         "content": f"选取前 3 条证据：{selected}"})
        # 5. 生成
        yield frame({"type": "thinking", "stage": "agentic_plan",
                     "content": f"开始生成回答（{model_name}）"})

        pieces: list[str] = []
        produced = False
        stream = None
        try:
            stream = llm.stream_answer(query, hits, grounded=grounded) if hits or not grounded else None
        except Exception:
            stream = None
        if stream is not None:
            try:
                for delta in stream:
                    produced = True
                    pieces.append(delta)
                    yield frame({"type": "response", "content": delta, "session_id": session_id})
            except Exception:
                if not produced:
                    pieces.clear()
        if not produced:
            fallback = answer
            for start_at in range(0, len(fallback), 24):
                pieces.append(fallback[start_at:start_at + 24])
                yield frame({"type": "response", "content": fallback[start_at:start_at + 24],
                             "session_id": session_id})
                time.sleep(0.008)
            final = fallback
        else:
            footer = (llm.footer() if grounded
                      else llm.footer().replace("基于检索结果生成", "生成（通用知识，未引用资料）"))
            final = "".join(pieces) + "\n\n> " + footer
            yield frame({"type": "response", "content": "\n\n> " + footer,
                         "session_id": session_id})
        total_ms = (time.perf_counter() - started) * 1000
        yield frame({"type": "thinking", "stage": "tool_end",
                     "content": f"完成：总耗时 {total_ms:.0f}ms，输出 {len(final)} 字"})
        store2 = _load()
        store2["messages"].setdefault(session_id, []).append(
            {"id": uuid.uuid4().hex, "role": "assistant", "content": final, "created_at": _now()})
        _save(store2)
        yield frame({"type": "done", "session_id": session_id})

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/chat/agent/resume")
async def resume(request: Request) -> StreamingResponse:
    body = await request.json()
    session_id = str(body.get("session_id") or "")

    def generate() -> Iterator[str]:
        yield f"data: {json.dumps({'type': 'done', 'session_id': session_id})}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


# ---------------------------------------------------------------- 知识库
@app.get("/knowledge/list")
async def knowledge_list() -> dict[str, Any]:
    index = get_index()
    counts: dict[str, int] = {}
    for chunk in index.chunks:
        counts[chunk.doc_id] = counts.get(chunk.doc_id, 0) + 1
    documents = [{
        "id": doc_id, "user_id": "u_demo", "md5": f"md5_{doc_id}",
        "filename": f"{doc_id}.md", "file_size": 1024, "file_type": "md",
        "status": "done", "chunk_count": count, "created_at": _now(),
    } for doc_id, count in sorted(counts.items())]
    return _ok({"documents": documents, "total_count": len(documents)})


@app.get("/knowledge/chunks")
async def knowledge_chunks(filename: str = "") -> dict[str, Any]:
    doc_id = filename[:-3] if filename.endswith(".md") else filename
    index = get_index()
    chunks = [{"chunk_id": c.chunk_id, "index": i, "content": c.text, "page": 0, "images": []}
              for i, c in enumerate(index.chunks) if c.doc_id == doc_id]
    return _ok({"chunks": chunks})


@app.get("/knowledge/detail")
async def knowledge_detail(filename: str) -> dict[str, Any]:
    doc_id = filename[:-3] if filename.endswith(".md") else filename
    index = get_index()
    content = index.read(doc_id)
    if content is None:
        candidate = UPLOADS / filename
        if candidate.exists():
            content = candidate.read_text(encoding="utf-8", errors="ignore")
    if content is None:
        return _ok(None, "未找到文档")
    return _ok({
        "id": doc_id, "user_id": "u_demo", "md5": f"md5_{doc_id}", "filename": filename,
        "chunk_count": sum(1 for c in index.chunks if c.doc_id == doc_id),
        "content": content[:20000], "images": [], "created_at": _now(),
    })


@app.get("/knowledge/md5/list")
async def knowledge_md5_list() -> dict[str, Any]:
    index = get_index()
    docs = sorted({c.doc_id for c in index.chunks})
    return _ok([{"md5": f"md5_{d}", "filename": f"{d}.md"} for d in docs])


@app.post("/knowledge/add/multiple/stream")
async def knowledge_add_stream(files: list[UploadFile] = File(...)) -> StreamingResponse:
    def frame(payload: dict[str, Any]) -> str:
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    async def generate() -> Iterator[str]:
        index = get_index()
        UPLOADS.mkdir(parents=True, exist_ok=True)
        for upload in files:
            name = upload.filename or "未命名.md"
            yield frame({"event_type": "processing", "filename": name, "progress": 40})
            raw = await upload.read()
            text = raw.decode("utf-8", errors="ignore")
            if not text.strip():
                yield frame({"event_type": "error", "filename": name, "error_message": "空文件"})
                continue
            path = UPLOADS / name
            path.write_text(text, encoding="utf-8")
            index.add_file(path)
            yield frame({"event_type": "completed", "filename": name, "progress": 100})
        yield frame({"event_type": "finish"})

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.post("/knowledge/add/single")
async def knowledge_add_single(file: UploadFile = File(...)) -> dict[str, Any]:
    index = get_index()
    UPLOADS.mkdir(parents=True, exist_ok=True)
    name = file.filename or "未命名.md"
    text = (await file.read()).decode("utf-8", errors="ignore")
    path = UPLOADS / name
    path.write_text(text, encoding="utf-8")
    index.add_file(path)
    return _ok({"filename": name, "chunk_count": sum(1 for c in index.chunks if c.doc_id == path.stem)})


@app.delete("/knowledge/delete/filename")
async def knowledge_delete(filename: str) -> dict[str, Any]:
    path = UPLOADS / filename
    if path.exists():
        path.unlink()
    get_index().reload()
    return _ok(None, "已删除")


@app.delete("/knowledge/md5/delete/{md5}")
async def knowledge_delete_md5(md5: str) -> dict[str, Any]:
    return _ok(None, "已删除")


@app.delete("/knowledge/clean")
async def knowledge_clean() -> dict[str, Any]:
    if UPLOADS.exists():
        for path in UPLOADS.glob("*"):
            if path.is_file():
                path.unlink()
    get_index().reload()
    return _ok(None, "已清空")


# ---------------------------------------------------------------- 笔记
@app.get("/note/list")
async def note_list() -> dict[str, Any]:
    store = _load()
    return _ok({"notes": store.get("notes", []), "total_count": len(store.get("notes", []))})


@app.post("/note/create")
async def note_create(request: Request) -> dict[str, Any]:
    body = await request.json()
    store = _load()
    note = {
        "id": uuid.uuid4().hex, "user_id": "u_demo",
        "title": body.get("title") or "无标题",
        "content": body.get("content") or "",
        "tags": body.get("tags") or [], "category": body.get("category") or "默认",
        "is_pinned": False, "created_at": _now(), "updated_at": _now(),
    }
    store.setdefault("notes", []).append(note)
    _save(store)
    return _ok(note)


@app.get("/note/stats")
async def note_stats() -> dict[str, Any]:
    store = _load()
    return _ok({"total": len(store.get("notes", [])), "pinned": 0, "categories": {}})


@app.get("/review/today")
async def review_today() -> dict[str, Any]:
    return _ok({"notes": [], "count": 0})


@app.get("/note-template/list")
async def template_list() -> dict[str, Any]:
    return _ok({"templates": [], "total_count": 0})


# ---------------------------------------------------------------- 图谱（空实现，避免页面崩）
@app.get("/api/graph/overview")
async def graph_overview() -> dict[str, Any]:
    return _ok({"nodes": [], "edges": [], "stats": {"entities": 0, "relations": 0}})


@app.get("/api/graph/events")
async def graph_events() -> dict[str, Any]:
    return _ok({"events": []})


@app.get("/chat/usage")
async def usage(days: int = 7) -> dict[str, Any]:
    return _ok({"days": days, "sessions": []})


@app.get("/chat/usage/{session_id}")
async def usage_detail(session_id: str, days: int = 7) -> dict[str, Any]:
    return _ok({"requests": []})


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "mode": "merged-slice"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8100, log_level="warning")
