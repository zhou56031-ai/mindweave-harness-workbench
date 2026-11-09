"""合并切片演示：并行检索 + 崩溃恢复。

用法：
    python -m workbench.demo parallel     # 3 个检索：串行 vs 并行
    python -m workbench.demo recover      # 崩溃后恢复未闭合工具三态
    python -m workbench.demo all
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

from eval.support.agent_contracts import text_response, tool_call
from eval.phase5.scenarios import BenchmarkResponses
from mindweave_harness.agent_engine import AgentEngine
from mindweave_harness.config import HexgentConfig
from mindweave_harness.plan import InMemoryPlanStore
from mindweave_harness.session.session_store import SessionStore
from mindweave_harness.session.tool_call_recovery import find_unclosed_tool_calls
from mindweave_harness.tool.tool_context import ToolExecutionContext
from mindweave_harness.tool.tool_execution import execute_tool_batch
from mindweave_harness.tool.tool_scheduler import ToolParallelLimits

from workbench import tools

QUERIES = [
    ("缓存击穿的两种处理方式", "缓存与检索"),
    ("RRF 融合为什么只比较名次", "缓存与检索"),
    ("图谱任务失败怎么恢复", "graph"),
]


def _drain(generator):
    events = []
    while True:
        try:
            events.append(next(generator))
        except StopIteration as stopped:
            return events, stopped.value


def _context(root: Path) -> ToolExecutionContext:
    return ToolExecutionContext(InMemoryPlanStore(), root)


def _calls() -> list[SimpleNamespace]:
    return [
        SimpleNamespace(call_id=f"call-{i}", name="search_knowledge",
                        arguments=f'{{"query":"{q}","top_k":2}}')
        for i, (q, _) in enumerate(QUERIES, 1)
    ]


def run_parallel(latency_ms: int = 40) -> dict:
    """同一批 3 个只读检索：串行 vs 并行。"""
    tools.install()
    from workbench.knowledge import get_index
    get_index()  # 预热索引，避免把建索引时间算进串行
    os.environ["WORKBENCH_KB_LATENCY_MS"] = str(latency_ms)
    result = {"latency_ms": latency_ms, "queries": len(QUERIES)}
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        for label, limits in (
            ("serial", ToolParallelLimits(enabled=False, max_workers=1, batch_timeout_seconds=30)),
            ("parallel", ToolParallelLimits(enabled=True, max_workers=4, batch_timeout_seconds=30)),
        ):
            context = _context(root)
            started = time.perf_counter()
            _, batch = _drain(execute_tool_batch(
                _calls(), context=context,
                user_confirmation=lambda tool, message: "allow",
                remaining_tool_calls=3, deadline=time.monotonic() + 30,
                parallel_limits=limits,
            ))
            elapsed = (time.perf_counter() - started) * 1000
            result[label] = round(elapsed, 1)
            result[f"{label}_outputs"] = len(batch.outputs)
    result["speedup"] = round(result["serial"] / max(0.1, result["parallel"]), 2)
    os.environ.pop("WORKBENCH_KB_LATENCY_MS", None)
    return result


def _child(root: str, session_id: str) -> None:
    """子进程：写一条"已派发"记录后立刻崩溃，模拟 kill -9。"""
    store = SessionStore(Path(root), session_id=session_id)
    store.append_transcript("tool_execution_started", {
        "call_id": "call-9", "tool_name": "create_note", "arguments": "{}",
    })
    store.append_transcript("tool_execution_dispatched", {
        "call_id": "call-9", "tool_name": "create_note",
        "recovery_action": "verify_before_retry",
    })
    os._exit(73)


def run_recover() -> dict:
    """父进程：等子进程崩溃，再从 transcript 判定未闭合工具。"""
    with tempfile.TemporaryDirectory() as directory:
        session_id = "session-crash-demo"
        proc = subprocess.run(
            [sys.executable, "-m", "workbench.demo", "--child", directory, session_id],
            cwd=str(Path(__file__).resolve().parents[1]),
            capture_output=True, text=True,
        )
        store = SessionStore(Path(directory), session_id=session_id)
        records = store.read_transcript()
        unclosed = find_unclosed_tool_calls(records, {"call-9"})
        return {
            "exit_code": proc.returncode,
            "records": len(records),
            "unclosed": [
                {"call_id": u.call_id, "tool": u.tool_name, "action": u.recovery_action}
                for u in unclosed
            ],
        }


def run_e2e(latency_ms: int = 40) -> dict:
    """整条链路：模型一轮发起 3 个检索 → 工具执行 → 回填 → 最终回答。"""
    tools.install()
    from workbench.knowledge import get_index
    get_index()  # 预热索引
    os.environ["WORKBENCH_KB_LATENCY_MS"] = str(latency_ms)
    calls = [
        tool_call(f"c{i}", "search_knowledge",
                  json.dumps({"query": q, "top_k": 2}, ensure_ascii=False))
        for i, (q, _) in enumerate(QUERIES, 1)
    ]
    result: dict = {"latency_ms": latency_ms}
    for label, enabled in (("serial", False), ("parallel", True)):
        client = BenchmarkResponses([
            text_response("", output=calls),
            text_response("综合三份资料：缓存击穿可用互斥锁或逻辑过期 [1][2][3]。"),
        ])
        with tempfile.TemporaryDirectory() as directory:
            engine = AgentEngine.create(
                HexgentConfig(tool_parallel=ToolParallelLimits(enabled=enabled, max_workers=4)),
                Path(directory),
            )
            started = time.perf_counter()
            events = list(engine.run("对比三份资料给出结论", client=client,
                                     user_confirmation=lambda tool, message: "allow"))
            elapsed = (time.perf_counter() - started) * 1000
            records = engine.session.store.read_transcript()
            finished = [r for r in records if r["type"] == "tool_execution_finished"]
            result[label] = {
                "ms": round(elapsed, 1),
                "tool_outputs": len(finished),
                "events": len(events),
            }
    result["speedup"] = round(result["serial"]["ms"] / max(0.1, result["parallel"]["ms"]), 2)
    os.environ.pop("WORKBENCH_KB_LATENCY_MS", None)
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", nargs="?", default="all",
                        choices=["parallel", "recover", "e2e", "all"])
    parser.add_argument("--child", nargs=2, metavar=("ROOT", "SESSION"))
    parser.add_argument("--latency-ms", type=int, default=40)
    args = parser.parse_args()

    if args.child:
        _child(*args.child)
        return

    if args.command in ("parallel", "all"):
        result = run_parallel(args.latency_ms)
        print("== 并行检索 ==")
        print(f"3 个检索，模拟单次远程 KB 延迟 {result['latency_ms']}ms")
        print(f"串行: {result['serial']} ms")
        print(f"并行: {result['parallel']} ms")
        print(f"加速: {result['speedup']}x（每个调用都产出了回执）")

    if args.command in ("recover", "all"):
        result = run_recover()
        print("\n== 崩溃恢复 ==")
        print(f"子进程退出码: {result['exit_code']}（73 = 模拟 kill -9）")
        print(f"恢复读到的记录: {result['records']} 条")
        for item in result["unclosed"]:
            print(f"未闭合: {item['call_id']} / {item['tool']} → {item['action']}")
        print("结论：没有把未完成的写操作当成成功，也没有自动重试。")

    if args.command in ("e2e", "all"):
        result = run_e2e(args.latency_ms)
        print("\n== 端到端（一轮 3 个检索） ==")
        print(f"串行: {result['serial']['ms']} ms / 回执 {result['serial']['tool_outputs']} 条")
        print(f"并行: {result['parallel']['ms']} ms / 回执 {result['parallel']['tool_outputs']} 条")
        print(f"加速: {result['speedup']}x")


if __name__ == "__main__":
    main()
