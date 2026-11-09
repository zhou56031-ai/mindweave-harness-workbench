"""模型 → 工具 → 结果 → 模型；最终答案也来自同一循环。"""
import json
import re
import time
from pathlib import Path
from app.core.settings import settings
from app.agent.loop_protocol import TOOLS, ContextLimitError
from app.agent.loop_tools import dispatch, pack_result
from app.services.deepseek_client import deepseek_client
from app.core.agent_trace import emit, span, text_info
from app.services.user_context import augment_system
from app.db.db_config import AsyncSessionLocal
from app.rag.agentic_service import collect_evidence
from app.agent.approval_allowlist import is_allowed
from app.agent.tools import execute_approved_write

POLICY = Path(__file__).resolve().parents[1] / "prompt/agent_loop.txt"



GREETING = re.compile(r"^(你好|您好|hi|hello|在吗|谢谢|早上好|晚上好|晚安)", re.I)
CREATIVE = re.compile(r"^(写|帮我写|翻译|讲个|来首|生成一首|作一首)")
FILE_HINTS = re.compile(r"文件|创建|写入|读取|目录|运行|执行|代码|脚本|Python|命令|创建笔记|计划|规划|步骤", re.I)


def needs_retrieval(query: str) -> bool:
    """知识型问题首轮强制检索；纯寒暄与创作类放行。"""
    text = query.strip()
    if len(text) < 6 or GREETING.match(text) or CREATIVE.match(text):
        return False
    # 文件/代码类操作不需要预检索，避免无关资料干扰工具调用
    if FILE_HINTS.search(text):
        return False
    return True


async def run_loop(context, store, query: str):
    system = await augment_system(POLICY.read_text(encoding="utf-8"), getattr(store, "user_id", ""))
    context.messages.append({"role": "user", "content": query})
    counts = {}
    rag_calls = 0
    active_tools = TOOLS
    call_ids = {call["id"] for m in context.messages for call in m.get("tool_calls", [])}
    recovered = False
    forced = False
    # 预检索：先取证据再让模型回答，少一轮模型调用，首字更快（①）
    if needs_retrieval(query):
        keep = {"read_artifact", "compact", "write_file", "read_file", "list_files", "run_python", "create_note"}
        try:
            async with AsyncSessionLocal() as db:
                hits, evidence_context, _answerability, _used_web = await collect_evidence(
                    db, getattr(store, "user_id", ""), query,
                )
            if evidence_context.strip():
                context.messages.append({
                    "role": "system",
                    "content": "已预先检索到用户资料，请直接基于以下证据回答，不要再次检索：\n" + evidence_context,
                })
            else:
                context.messages.append({
                    "role": "system",
                    "content": "资料库中没有检索到相关内容。请明确说明这一点，再用通用知识回答；不要调用检索工具。",
                })
            active_tools = [item for item in TOOLS if item["function"]["name"] in keep]
            forced = True
            # 直接流式作答：已经拿到证据（或明确无证据）时不再多跑一轮模型决策（①）
            try:
                parts: list[str] = []
                async for delta in deepseek_client.stream_chat(context.request_messages(system)):
                    parts.append(delta)
                    yield {"type": "response", "content": delta}
                yield {"type": "answer", "content": "".join(parts), "streamed": True}
                return
            except Exception:
                pass
        except Exception:
            pass
    async def finish(reason: str, step: int):
        """到达步数/循环上限时的降级出口：给部分答案，而不是抛错。"""
        emit("agent.loop_stopped", reason=reason, steps=step)
        labels = {
            "tool_budget": "工具调用次数已达上限",
            "wall_clock": "已超过本轮时间预算",
            "max_steps": "已到达轮次上限",
        }
        yield {"type": "thinking", "stage": "agentic_plan",
               "content": f"{labels.get(reason, '已达到本轮上限')}，基于已有结果给出部分答案"}
        try:
            text = await deepseek_client.chat(
                [
                    *context.request_messages(system),
                    {"role": "user", "content": (
                        "请基于以上已经获得的信息直接回答用户问题；"
                        "信息不足时明确说明还缺什么。不要再调用工具。"
                    )},
                ],
                max_tokens=settings.CHAT_ANSWER_MAX_TOKENS,
            )
        except Exception as exc:  # 模型也不可用时，至少给出可行动的说明
            emit("agent.partial_answer_failed", error_type=type(exc).__name__)
            text = ("已达到本轮工具调用或时长上限。已查到的内容可能不完整，"
                    "建议缩小问题范围后重试，或直接指定要查的笔记/文档。")
        yield {"type": "answer", "content": text}

    tool_calls_used = 0
    deadline = time.monotonic() + settings.MAX_WALL_SECONDS
    for step in range(settings.AGENT_MAX_STEPS):
        if time.monotonic() > deadline:
            async for chunk in finish("wall_clock", step + 1):
                yield chunk
            return
        if await context.prepare(system, active_tools, store.archive):
            yield {"type": "thinking", "stage": "agentic_plan", "content": "已整理旧上下文，原文保留在会话归档"}
        yield {"type": "thinking", "stage": "agentic_plan", "content": f"正在处理第 {step + 1} 轮"}
        try:
            with span("agent.model", step=step + 1, input_bytes=context.input_bytes(system, active_tools)):
                message = await deepseek_client.complete_turn(context.request_messages(system), active_tools)
        except ContextLimitError:
            if recovered or not await context.prepare(system, active_tools, store.archive, force=True):
                emit("agent.loop_stopped", reason="context_limit", steps=step + 1)
                yield {"type": "answer", "content": (
                    "上下文超出模型上限，已停止继续调用工具。"
                    "请把问题拆小，或新建会话后重试。"
                )}
                return
            recovered = True
            yield {"type": "thinking", "stage": "agentic_plan", "content": "输入被模型拒绝，已压缩旧轮次并准备重试一次"}
            continue
        calls = message.get("tool_calls", [])
        if calls:
            forced = True  # 已经检索过，不再触发首轮强制
        if not calls:
            if not forced and needs_retrieval(query):
                forced = True
                context.messages.append({
                    "role": "system",
                    "content": "回答前必须先调用 search_rag 检索用户自己的资料；检索不到再说明，不要凭记忆回答。",
                })
                yield {"type": "thinking", "stage": "agentic_plan",
                       "content": "先检索你的资料，再回答"}
                continue
            context.messages.append(message)
            yield {"type": "answer", "content": message["content"]}
            return
        call = calls[0]
        if call["id"] in call_ids:
            async for chunk in finish("duplicate_call", step + 1):
                yield chunk
            return
        call_ids.add(call["id"])
        name = call["function"]["name"]
        try:
            arguments = json.loads(call["function"]["arguments"])
            if not isinstance(arguments, dict):
                raise ValueError("参数必须是对象")
        except (ValueError, TypeError):
            arguments = None
        signature = name + json.dumps(arguments, ensure_ascii=False, sort_keys=True)
        counts[signature] = counts.get(signature, 0) + 1
        if counts[signature] > 2:
            async for chunk in finish("tool_loop", step + 1):
                yield chunk
            return
        if tool_calls_used >= settings.MAX_TOOL_CALLS:
            async for chunk in finish("tool_budget", step + 1):
                yield chunk
            return
        tool_calls_used += 1
        yield {"type": "thinking", "stage": "tool_start", "content": f"正在调用 {name}", "details": {"tool": name}}
        with span("agent.tool", tool=name, arguments=text_info(call["function"]["arguments"])) as details:
            result = ({"result": {"error": "arguments 必须是合法 JSON 对象"}}
                      if arguments is None else await dispatch(name, arguments, store))
            details["proposal"] = bool(result.get("proposal"))
            details["business_error"] = isinstance(result.get("result"), dict) and "error" in result["result"]
        if result.get("proposal"):
            proposal = result["proposal"]
            if is_allowed(getattr(store, "session_id", ""), proposal["tool"]):
                async with AsyncSessionLocal() as db:
                    output = await execute_approved_write(db, store.user_id, proposal)
                context.messages.extend([message, {
                    "role": "tool", "tool_call_id": call["id"], "content": output,
                }])
                yield {"type": "thinking", "stage": "tool_execution",
                       "content": f"{proposal['tool']} 已按本次会话的授权直接执行"}
                continue
            # 待审批的未完成调用不写入持久消息链；AgentRun 保存原请求与提案。
            yield {"type": "proposal", "proposal": proposal}
            return
        packed = await pack_result(store, name, result["result"])
        emit("tool.packed", tool=name, model_payload=text_info(packed))
        context.messages.extend([message, {
            "role": "tool", "tool_call_id": call["id"], "content": packed,
        }])
        if name == "search_rag" and not result.get("evidence"):
            yield {"type": "thinking", "stage": "answerability",
                   "content": "资料里没有直接命中的内容。可能原因：问法太泛、文档还没建完索引，或资料本身不包含这个主题。建议换用资料里的具体术语，或先导入相关文档。",
                   "details": {"status": "empty"}}
        if result.get("evidence"):
            yield {"type": "thinking", "stage": "local_retrieval", "content": "已取得可引用资料", "details": {
                "status": "evidence", "results": [{
                    "id": h["source_id"], "source": h["source_kind"],
                    "title": h["title"], "score": h["score"], "preview": h["snippet"],
                } for h in result["evidence"]],
            }}
        if result.get("compact"):
            changed = await context.prepare(system, active_tools, store.archive, force=True)
            yield {"type": "thinking", "stage": "agentic_plan", "content": "旧轮次已压缩" if changed else "没有需要压缩的旧轮次"}
        yield {"type": "thinking", "stage": "tool_end", "content": f"{name} 已返回结果", "details": {"tool": name}}
    async for chunk in finish("max_steps", settings.AGENT_MAX_STEPS):
        yield chunk
