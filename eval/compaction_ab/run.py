"""压缩 A/B 实验：基线（剪枝关闭） vs 剪枝优先（prune-first）。

用法：
    python -m eval.compaction_ab.run --mode baseline
    python -m eval.compaction_ab.run --mode prune

说明：
- 固定 6 类夹具、固定估算器、固定 FakeClient（不访问网络）。
- 只测“单次压缩”：before/after 都用生产 estimate_request_tokens 计算完整请求。
- 通过 commit_boundary 写入临时 Session，确保 pruning/compaction 走真实提交路径。
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from eval.phase5.scenarios import BenchmarkResponses
from eval.support.agent_contracts import tool_call
from mindweave_harness.context import ContextLimits
from mindweave_harness.context.context_compaction import compact_context_if_needed
from mindweave_harness.context.context_manager import ContextManager
from mindweave_harness.plan import InMemoryPlanStore, Plan, PlanStep
from mindweave_harness.session import Session

# 为让 6 类夹具都在毫秒级触发压缩，窗口按比例缩小（生产为 128K/64K/24K）。
SCALED = dict(
    context_window_tokens=32_000,
    reserved_output_tokens=2_000,
    safety_margin_tokens=2_000,
    proactive_threshold_tokens=12_000,
    recent_context_tokens=3_000,
    summary_output_tokens=500,
    min_compaction_savings_tokens=100,
    min_compaction_savings_ratio=0.01,
)

SCENARIOS = ["tool_heavy", "mixed", "small_outputs", "with_plan", "constraint", "big_single"]


class CountingClient:
    """包一层，统计每一次（提取 + 摘要 + 主任务）模型调用。"""

    def __init__(self, inner):
        self.inner = inner
        self.count = 0
        self.input_tokens = []
        self.responses = self

    def create(self, **kwargs):
        from mindweave_harness.utils.token_estimator import estimate_value_tokens
        self.count += 1
        self.input_tokens.append(estimate_value_tokens(kwargs.get("input") or []))
        return self.inner.responses.create(**kwargs)


def _limits(prune: bool) -> ContextLimits:
    kwargs: dict[str, Any] = dict(SCALED)
    if prune:
        try:
            return ContextLimits(prune_enabled=True, **kwargs)
        except TypeError:
            raise SystemExit("当前代码尚不支持 prune_enabled（实现尚未合入）")
    return ContextLimits(**kwargs)


def _tool_output(call_id: str, *, content: str) -> list[Any]:
    return [
        tool_call(call_id, "read_file", '{"path":"docs/fixture.md"}'),
        {"type": "function_call_output", "call_id": call_id, "output": content},
    ]


def build(kind: str, root: Path):
    """返回 (context_manager, session, plan, expected_constraint)。"""
    ctx = ContextManager()
    session = Session.persistent(root)
    query = ctx.append_user_query("读取资料并完成当前任务")
    session.record_user_query(query)
    plan = None
    constraint = None

    long_doc = "FACT=ALPHA\n" + "资料正文：成本、交付与合规要求。" * 70  # ≈ 1.1K 中文字符
    rounds = 48 if kind == "small_outputs" else 24
    for i in range(rounds):
        if kind == "tool_heavy":
            msgs = _tool_output(f"call-{i}", content=long_doc)
        elif kind == "mixed":
            msgs = (
                _tool_output(f"call-{i}", content=long_doc)
                if i % 2 == 0
                else [{"role": "assistant", "content": "分析：" + "说明。" * 300}]
            )
        elif kind == "small_outputs":
            msgs = _tool_output(f"call-{i}", content="短结果。" * 60)  # ≈ 240 字符，低于剪枝阈值
        elif kind == "with_plan":
            msgs = _tool_output(f"call-{i}", content=long_doc)
        elif kind == "constraint":
            msgs = _tool_output(f"call-{i}", content=long_doc)
        elif kind == "big_single":
            msgs = _tool_output(
                f"call-{i}",
                content=("超长附件：" + "条款。" * 2000) if i == 0 else "短结果。" * 60,
            )
        else:
            raise ValueError(kind)
        ctx.append_messages(msgs, completes_round=True)
        session.record_context_messages(
            msgs, completes_round=True,
            source="tool_results" if isinstance(msgs[0], dict) and msgs[0].get("type") == "function_call" else "model_response",
        )
        if kind == "constraint" and i == 1:
            constraint = ctx.append_user_query("硬约束：原始资料只读，只允许写 result.json")
            session.record_user_query(constraint)

    latest = ctx.append_user_query("继续完成当前任务")
    session.record_user_query(latest)
    if kind == "with_plan":
        plan = Plan("继续任务", "in_progress", [PlanStep("验证", "正在验证", "in_progress", "step-1")])
        session.record_plan_state(plan, action="created")
    return ctx, session, plan, constraint


def run_case(kind: str, prune: bool) -> dict[str, Any]:
    with TemporaryDirectory() as directory:
        ctx, session, plan, constraint = build(kind, Path(directory))
        client = CountingClient(BenchmarkResponses([], summary="FACT=ALPHA；保留当前约束，继续处理。"))
        limits = _limits(prune)
        started = time.perf_counter()
        events, outcome = [], None
        gen = compact_context_if_needed(
            ctx, client, "fixture-model", "系统提示", [],
            limits,
            commit_boundary=lambda b: session.record_compaction(b),
            plan_reference=plan,
        )
        try:
            while True:
                events.append(next(gen))
        except StopIteration as stopped:
            outcome = stopped.value
        duration_ms = (time.perf_counter() - started) * 1000

        record = {
            "scenario": kind,
            "status": outcome.status,
            "before": outcome.before_tokens,
            "after": outcome.after_tokens,
            "ratio": round((outcome.before_tokens - outcome.after_tokens) / outcome.before_tokens * 100, 1)
            if outcome.before_tokens else 0.0,
            "model_calls": client.count,
            "compaction_input_tokens": sum(client.input_tokens),
            "duration_ms": round(duration_ms, 1),
            "constraint_kept": (constraint.content in str(ctx.model_messages())) if constraint else None,
            "events": [type(e).__name__ for e in events],
        }
        return record


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["baseline", "prune"], required=True)
    parser.add_argument("--output", default=None)
    args = parser.parse_args()
    prune = args.mode == "prune"

    rows = [run_case(kind, prune) for kind in SCENARIOS]
    print(f"{'场景':<14}{'状态':<10}{'前':>7}{'后':>7}{'压缩率':>8}{'模型调用':>8}")
    for r in rows:
        print(f"{r['scenario']:<14}{r['status']:<10}{r['before']:>7}{r['after']:>7}"
              f"{r['ratio']:>7}%{r['model_calls']:>8}")
    compressed = [r for r in rows if r["ratio"] > 0 and r["status"] in {"compacted", "partial", "pruned"}]
    if compressed:
        avg = sum(r["ratio"] for r in compressed) / len(compressed)
        calls = sum(r["model_calls"] for r in rows)
        print(f"\n平均压缩率 {avg:.1f}% ｜ 模型调用合计 {calls} 次 ｜ 剪枝生效 {sum(1 for r in rows if r['status']=='pruned')} 例")

    payload = {"mode": args.mode, "rows": rows}
    output = Path(args.output or f"docs/压缩AB-{args.mode}.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print("写入", output)


if __name__ == "__main__":
    main()
