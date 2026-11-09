"""剪枝优先（prune-first）的纯函数、压缩分支与恢复测试。"""

from copy import deepcopy
from hashlib import sha256
from importlib import import_module
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests.support.fact_extraction_fixture import extraction_event

from mindweave_harness.config import MindWeaveHarnessConfig
from mindweave_harness.context.context_budget import ContextLimits
from mindweave_harness.context.context_builder import build_context
from mindweave_harness.context.context_compaction import (
    COMPACTION_INSTRUCTIONS,
    compact_context_if_needed,
)
from mindweave_harness.context.context_manager import ContextManager, UserQuery
from mindweave_harness.context.context_prune import PRUNE_POINTER_PREFIX, prune_messages
from mindweave_harness.context.key_fact_extractor import EXTRACTION_INSTRUCTIONS
from mindweave_harness.events import (
    ContextCompactionFailedEvent,
    ContextCompactionFinishedEvent,
    LoopTransitionEvent,
    RunFinishedEvent,
)
from mindweave_harness.loop.limits import LoopLimits
from mindweave_harness.loop.loop_state import LoopState
from mindweave_harness.plan import InMemoryPlanStore
from mindweave_harness.prompt import TOOLS
from mindweave_harness.run_control import RunControl
from mindweave_harness.session import Session, SessionRestorer
from mindweave_harness.tool.tool_context import ToolExecutionContext
from mindweave_harness.utils.token_estimator import estimate_request_tokens


agent_loop_module = import_module("mindweave_harness.loop.agent_loop")
turn_module = import_module("mindweave_harness.loop.agent_turn")


LONG_OUTPUT = "文件内容" * 1500  # 6000 个字符，足以触发剪枝。


class ForbiddenResponses:
    """任何模型请求都视为用例失败。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.extraction_calls: list[dict] = []

    def create(self, **arguments):
        """剪枝足够时不得触达 Provider。"""
        self.calls.append(arguments)
        raise AssertionError("剪枝已足够，不得调用任何模型")


class ForbiddenClient:
    def __init__(self) -> None:
        self.responses = ForbiddenResponses()


class RecordingResponses:
    """记录提取与摘要请求，并返回预设流式事件。"""

    def __init__(self, response: SimpleNamespace) -> None:
        self.response = response
        self.calls: list[dict] = []
        self.extraction_calls: list[dict] = []

    def create(self, **arguments):
        extraction = extraction_event(self, arguments)
        if extraction is not None:
            return extraction
        self.calls.append(arguments)
        return iter(
            [SimpleNamespace(type="response.completed", response=self.response)]
        )


class RecordingClient:
    def __init__(self, response: SimpleNamespace) -> None:
        self.responses = RecordingResponses(response)


def drain_generator(generator):
    """收集生成器事件并取得最终返回值。"""
    events = []
    while True:
        try:
            events.append(next(generator))
        except StopIteration as stopped:
            return events, stopped.value


def build_conversation() -> tuple[ContextManager, UserQuery]:
    """构造"一条大工具结果 + 最新 Query"的完整轮次上下文。"""
    conversation = ContextManager()
    conversation.append_user_query("请读取大文件并总结")
    conversation.append_message(
        {
            "type": "function_call",
            "call_id": "call-1",
            "name": "read_file",
            "arguments": '{"path":"a.py"}',
        }
    )
    conversation.append_messages(
        [
            {
                "type": "function_call_output",
                "call_id": "call-1",
                "output": LONG_OUTPUT,
            }
        ],
        completes_round=True,
    )
    latest = conversation.append_user_query("给出三点结论")
    return conversation, latest


def prune_limits(**overrides) -> ContextLimits:
    """返回允许剪枝且阈值刚好落在剪枝前后的压缩配置。"""
    values = dict(
        context_window_tokens=128000,
        proactive_threshold_tokens=3000,
        reserved_output_tokens=1,
        safety_margin_tokens=1,
        recent_context_tokens=1,
        min_compaction_savings_tokens=1,
        min_compaction_savings_ratio=0.01,
        prune_enabled=True,
        prune_min_output_chars=400,
        prune_preview_chars=120,
    )
    values.update(overrides)
    return ContextLimits(**values)


class PruneMessagesTest(unittest.TestCase):
    """验证剪枝只处理够长的工具结果，并且是纯函数。"""

    def test_only_long_function_call_output_is_pruned(self) -> None:
        """只有够长的 function_call_output 被替换，其余消息原样保留。"""
        messages = [
            {"role": "user", "content": "问题"},
            {
                "type": "function_call",
                "call_id": "c1",
                "name": "read_file",
                "arguments": "{}",
            },
            {"type": "function_call_output", "call_id": "c1", "output": "长结果" * 500},
            {"type": "function_call_output", "call_id": "c2", "output": "短输出"},
            {"type": "message", "role": "assistant", "content": "结论"},
        ]

        pruned, records = prune_messages(
            messages,
            preview_chars=10,
            min_output_chars=100,
            artifact_lookup={"c1": "sessions/artifacts/a.txt"},
        )

        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record.index, 2)
        self.assertEqual(record.call_id, "c1")
        self.assertEqual(record.tool_name, "read_file")
        self.assertEqual(record.artifact, "sessions/artifacts/a.txt")
        self.assertEqual(record.original_chars, 1500)
        self.assertIsNot(pruned, messages)
        self.assertEqual(pruned[0], messages[0])
        self.assertEqual(pruned[1], messages[1])
        self.assertEqual(pruned[3], messages[3])
        self.assertEqual(pruned[4], messages[4])
        self.assertIsNot(pruned[2], messages[2])
        self.assertEqual(pruned[2]["type"], "function_call_output")
        self.assertEqual(pruned[2]["call_id"], "c1")
        self.assertTrue(pruned[2]["output"].startswith(PRUNE_POINTER_PREFIX))
        self.assertEqual(messages[2]["output"], "长结果" * 500)

    def test_short_output_is_not_pruned(self) -> None:
        """短于 min_output_chars 的结果不剪枝，避免指针比原文更长。"""
        messages = [
            {"type": "function_call_output", "call_id": "c1", "output": "x" * 399},
        ]

        pruned, records = prune_messages(
            messages,
            preview_chars=120,
            min_output_chars=400,
        )

        self.assertEqual(records, ())
        self.assertEqual(pruned, messages)
        self.assertEqual(pruned[0]["output"], "x" * 399)

    def test_pointer_contains_call_id_artifact_sha256_length_and_preview(self) -> None:
        """指针必须可回溯：call_id、工件、原文 SHA-256、原长和预览都在。"""
        original = "abcdefghij" * 100
        messages = [
            {"type": "function_call_output", "call_id": "c9", "output": original},
        ]

        pruned, _ = prune_messages(
            messages,
            preview_chars=12,
            min_output_chars=400,
            artifact_lookup={"c9": "sessions/tool-results/x.txt"},
        )

        pointer = pruned[0]["output"]
        self.assertIn("call_id=c9", pointer)
        self.assertIn("artifact=sessions/tool-results/x.txt", pointer)
        self.assertIn(sha256(original.encode("utf-8")).hexdigest(), pointer)
        self.assertIn("原长=1000 字符", pointer)
        self.assertIn(original[:12], pointer)
        self.assertNotIn(original, pointer)

    def test_prune_messages_is_pure_and_deterministic(self) -> None:
        """不修改入参，且同一输入两次调用得到完全相同的结果。"""
        messages = [
            {"type": "function_call", "call_id": "c1", "name": "read_file"},
            {"type": "function_call_output", "call_id": "c1", "output": LONG_OUTPUT},
            {"role": "user", "content": "继续"},
        ]
        before = deepcopy(messages)

        first, first_records = prune_messages(
            messages, preview_chars=80, min_output_chars=400
        )
        second, second_records = prune_messages(
            messages, preview_chars=80, min_output_chars=400
        )

        self.assertEqual(messages, before)
        self.assertEqual(first, second)
        self.assertEqual(first_records, second_records)
        self.assertEqual(first_records[0].tool_name, "read_file")

    def test_already_pruned_output_is_left_alone(self) -> None:
        """已经是指针的结果不再二次剪枝，避免丢失原始指纹。"""
        messages = [
            {
                "type": "function_call_output",
                "call_id": "c1",
                "output": PRUNE_POINTER_PREFIX + "：sha256=" + "0" * 64,
            },
        ]

        pruned, records = prune_messages(
            messages, preview_chars=120, min_output_chars=10
        )

        self.assertEqual(records, ())
        self.assertEqual(pruned[0], messages[0])


class ContextPruningTest(unittest.TestCase):
    """验证压缩入口的剪枝分支与摘要回退。"""

    def test_pruning_below_threshold_calls_no_model(self) -> None:
        """剪枝后已低于阈值时直接提交，不留任何模型调用。"""
        conversation, latest = build_conversation()
        client = ForbiddenClient()
        commits: list[dict] = []

        def commit(boundary: dict) -> dict:
            commits.append(boundary)
            return {"transcript_sequence": 7, "durability": "TEST"}

        events, outcome = drain_generator(
            compact_context_if_needed(
                conversation,
                client,
                "fake",
                "系统提示词",
                [],
                prune_limits(),
                commit_boundary=commit,
            )
        )

        self.assertEqual(outcome.status, "pruned")
        self.assertEqual(outcome.input_tokens, 0)
        self.assertEqual(outcome.output_tokens, 0)
        self.assertEqual(client.responses.calls, [])
        self.assertEqual(client.responses.extraction_calls, [])
        self.assertEqual(len(commits), 1)
        self.assertIs(commits[0], outcome.boundary)
        self.assertFalse(
            any(isinstance(event, ContextCompactionFailedEvent) for event in events)
        )
        self.assertIsInstance(events[-1], ContextCompactionFinishedEvent)
        self.assertEqual(events[-1].status, "pruned")

        # 1. 边界携带剪枝描述、切点与前后预算，供 Session 与恢复端使用。
        boundary = outcome.boundary
        self.assertEqual(boundary["kind"], "prune")
        self.assertEqual(boundary["cut_index"], 3)
        self.assertEqual(boundary["before_tokens"], outcome.before_tokens)
        self.assertEqual(boundary["after_tokens"], outcome.after_tokens)
        self.assertEqual(
            boundary["pruned_messages"],
            [
                {
                    "index": 2,
                    "call_id": "call-1",
                    "tool_name": "read_file",
                    "artifact": None,
                    "original_chars": 6000,
                }
            ],
        )
        self.assertEqual(boundary["resulting_round_boundaries"], [3])
        self.assertEqual(
            boundary["resulting_message_count"], len(conversation.active_messages)
        )
        self.assertEqual(boundary["publication_metrics"], {"transcript_sequence": 7, "durability": "TEST"})

        # 2. 活动上下文已经发布剪枝结果，最新 Query 与轮次边界仍然合法。
        pointer = conversation.active_messages[2]["output"]
        self.assertIn(sha256(LONG_OUTPUT.encode("utf-8")).hexdigest(), pointer)
        self.assertIn(LONG_OUTPUT[:120], pointer)
        self.assertIs(conversation.active_messages[3], latest)
        self.assertEqual(conversation.round_boundaries, [3])
        self.assertEqual(
            len(conversation.message_ids), len(conversation.active_messages)
        )

    def test_pruning_falls_back_to_summary_when_still_above_threshold(self) -> None:
        """剪枝仍不够时继续调用提取与摘要，但摘要输入使用剪枝后的前缀。"""
        conversation, _ = build_conversation()
        client = RecordingClient(
            SimpleNamespace(
                output_text="<summary>用户要求读取大文件并输出三点结论。</summary>",
                usage=SimpleNamespace(input_tokens=20, output_tokens=8),
            )
        )
        commits: list[dict] = []

        _, outcome = drain_generator(
            compact_context_if_needed(
                conversation,
                client,
                "fake",
                "系统提示词",
                [],
                prune_limits(proactive_threshold_tokens=10),
                commit_boundary=commits.append,
            )
        )

        self.assertIn(outcome.status, {"compacted", "partial"})
        self.assertGreater(len(client.responses.extraction_calls), 0)
        self.assertGreater(len(client.responses.calls), 0)
        self.assertNotEqual(outcome.boundary.get("kind"), "prune")
        self.assertIn(PRUNE_POINTER_PREFIX, client.responses.calls[0]["input"][2]["output"])

    def test_pruning_disabled_keeps_existing_compaction_path(self) -> None:
        """默认关闭剪枝时摘要输入保持原始工具结果。"""
        conversation, _ = build_conversation()
        client = RecordingClient(
            SimpleNamespace(
                output_text="<summary>用户要求读取大文件并输出三点结论。</summary>",
                usage=None,
            )
        )

        _, outcome = drain_generator(
            compact_context_if_needed(
                conversation,
                client,
                "fake",
                "系统提示词",
                [],
                prune_limits(prune_enabled=False),
                commit_boundary=lambda boundary: None,
            )
        )

        self.assertIn(outcome.status, {"compacted", "partial"})
        summary_input = client.responses.calls[0]["input"]
        self.assertTrue(
            any(
                isinstance(message, dict)
                and message.get("type") == "function_call_output"
                and message.get("output") == LONG_OUTPUT
                for message in summary_input
            )
        )


class ContextPruningRecoveryTest(unittest.TestCase):
    """验证剪枝事实的事件分派与 Transcript 重放。"""

    def _record_pruning(self, root: Path) -> tuple[ContextManager, Session, dict]:
        """写入完整上下文与一次剪枝事实，返回 MAIN 投影。"""
        context = ContextManager()
        session = Session.persistent(root)
        first = context.append_user_query("读取大文件")
        session.record_user_query(first)
        call = {
            "type": "function_call",
            "call_id": "call-1",
            "name": "read_file",
            "arguments": '{"path":"a.py"}',
        }
        context.append_message(call)
        session.record_context_messages(
            [call], completes_round=False, source="model_response"
        )
        output = {
            "type": "function_call_output",
            "call_id": "call-1",
            "output": "文件内容" * 800,
        }
        context.append_messages([output], completes_round=True)
        session.record_context_messages(
            [output], completes_round=True, source="tool_results"
        )
        latest = context.append_user_query("给出三点结论")
        session.record_user_query(latest)

        prefix = context.materialize_messages(context.active_messages[:3])
        pruned_prefix, _ = prune_messages(
            prefix, preview_chars=120, min_output_chars=400
        )
        boundary = context.candidate_pruning(3, pruned_prefix, 5000, 200)
        session.record_compaction(boundary)
        context.apply_pruning(boundary)
        return context, session, boundary

    def test_prune_boundary_is_written_as_context_pruned(self) -> None:
        """剪枝边界按 kind 分派到 context_pruned 事件。"""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _, session, boundary = self._record_pruning(root)
            transcript = session.store.read_transcript()

        self.assertEqual(transcript[-1]["type"], "context_pruned")
        self.assertEqual(transcript[-1]["payload"], boundary)

    def test_compaction_boundary_keeps_compaction_event(self) -> None:
        """普通摘要边界仍写入原有 context_compaction_applied 事件。"""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            context = ContextManager()
            session = Session.persistent(root)
            query = context.append_user_query("较早问题")
            session.record_user_query(query)
            answer = {"role": "assistant", "content": "较早回答"}
            context.append_messages([answer], completes_round=True)
            session.record_context_messages(
                [answer], completes_round=True, source="model_response"
            )
            boundary = context.apply_compaction(
                2,
                "此前已经完成较早问题。",
                before_tokens=100,
                after_tokens=30,
                summary_tokens=10,
                recent_tokens=5,
                status="compacted",
            )
            metrics = session.record_compaction(boundary)
            transcript = session.store.read_transcript()

        self.assertEqual(transcript[-1]["type"], "context_compaction_applied")
        self.assertEqual(metrics["transcript_sequence"], transcript[-1]["sequence"])

    def test_restore_replays_context_pruned_without_model_calls(self) -> None:
        """恢复端重放 context_pruned 后与提交前的活动上下文完全一致。"""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            context, session, _ = self._record_pruning(root)
            expected_messages = context.model_messages()
            expected_boundaries = list(context.round_boundaries)
            expected_ids = list(context.message_ids)
            session_id = session.store.session_id

            restored = SessionRestorer(root).restore(session_id)
            records = restored.session.store.read_transcript()

        self.assertEqual(restored.context_manager.model_messages(), expected_messages)
        self.assertEqual(
            restored.context_manager.round_boundaries, expected_boundaries
        )
        self.assertEqual(restored.context_manager.message_ids, expected_ids)
        self.assertEqual(
            restored.context_manager.user_queries[-1].content, "给出三点结论"
        )
        self.assertTrue(
            restored.context_manager.active_messages[2]["output"].startswith(
                PRUNE_POINTER_PREFIX
            )
        )
        self.assertFalse(
            any(record["type"] == "context_compaction_applied" for record in records)
        )


class ApplyPruningTest(unittest.TestCase):
    """验证前缀等长替换时最新 Query 与轮次边界的不变量。"""

    def test_latest_query_inside_prefix_keeps_its_identity(self) -> None:
        """切点覆盖最新 Query 时仍以不可变 Query 保留，不被物化降级。"""
        context = ContextManager()
        query = context.append_user_query("唯一 Query")
        context.append_messages(
            [
                {
                    "type": "function_call_output",
                    "call_id": "call-1",
                    "output": LONG_OUTPUT,
                }
            ],
            completes_round=True,
        )
        prefix = context.materialize_messages(context.active_messages[:2])
        pruned_prefix, records = prune_messages(
            prefix, preview_chars=120, min_output_chars=400
        )
        self.assertEqual(len(records), 1)

        boundary = context.candidate_pruning(2, pruned_prefix, 6000, 200)
        context.apply_pruning(boundary)

        self.assertEqual(context.user_queries, (query,))
        self.assertIs(context.active_messages[0], query)
        self.assertTrue(
            context.active_messages[1]["output"].startswith(PRUNE_POINTER_PREFIX)
        )
        self.assertEqual(context.round_boundaries, [2])
        self.assertEqual(len(context.message_ids), 2)
        self.assertEqual(len(set(context.message_ids)), 2)


if __name__ == "__main__":
    unittest.main()


class MainOnlyResponses:
    """只允许主模型请求；任何压缩或提取请求都视为用例失败。"""

    def __init__(self, response: SimpleNamespace) -> None:
        self.response = response
        self.calls: list[dict] = []

    def create(self, **arguments):
        if arguments.get("instructions") in {
            COMPACTION_INSTRUCTIONS,
            EXTRACTION_INSTRUCTIONS,
        }:
            raise AssertionError("剪枝足够时不得调用压缩或提取模型")
        self.calls.append(arguments)
        return iter(
            [SimpleNamespace(type="response.completed", response=self.response)]
        )


class MainOnlyClient:
    def __init__(self, response: SimpleNamespace) -> None:
        self.responses = MainOnlyResponses(response)


class PruningLoopTransitionTest(unittest.TestCase):
    """验证 status=pruned 在 Agent Loop 中形成显式迁移并跳过压缩模型。"""

    def test_loop_transitions_with_context_pruned(self) -> None:
        """剪枝成功后应重试主模型，并写入 context_pruned Checkpoint。"""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            session = Session.persistent(root)
            conversation, latest = build_conversation()
            tool_context = ToolExecutionContext(InMemoryPlanStore(), root)
            instructions = build_context(tool_context, runtime_injection_tokens=12_000)
            pruned, _ = prune_messages(
                conversation.materialize_messages(
                    conversation.active_messages[:3]
                ),
                preview_chars=120,
                min_output_chars=400,
            )
            # 阈值取剪枝后（含工具 Schema）的估计，保证剪枝刚好够用。
            after_tokens = estimate_request_tokens(
                instructions,
                [*pruned, *conversation.active_messages[3:]],
                TOOLS,
            )
            limits = ContextLimits(
                context_window_tokens=128000,
                proactive_threshold_tokens=after_tokens + 1,
                reserved_output_tokens=1,
                safety_margin_tokens=1,
                recent_context_tokens=1,
                min_compaction_savings_tokens=1,
                prune_enabled=True,
            )
            session.record_user_query(conversation.user_queries[0])
            state = LoopState(context_manager=conversation, tool_context=tool_context)
            client = MainOnlyClient(
                SimpleNamespace(
                    output=[],
                    output_text="已按剪枝后的上下文继续",
                    usage=SimpleNamespace(input_tokens=10, output_tokens=5),
                )
            )

            with (
                patch.object(agent_loop_module, "get_client", return_value=client),
                patch.object(turn_module, "get_model", return_value="fake"),
            ):
                events = list(
                    agent_loop_module.agent_loop(
                        state,
                        config=MindWeaveHarnessConfig(
                            loop=LoopLimits(), context=limits
                        ),
                        session=session,
                        run_control=RunControl(),
                    )
                )
            transcript = session.store.read_transcript()

        transitions = [
            event.reason
            for event in events
            if isinstance(event, LoopTransitionEvent)
        ]
        self.assertIn("context_pruned", transitions)
        # 只有剪枝后重试的那一次主模型调用，没有提取/摘要调用。
        self.assertEqual(len(client.responses.calls), 1)
        self.assertEqual(
            [record["type"] for record in transcript].count("context_pruned"), 1
        )
        checked_checkpoints = [
            record["payload"].get("reason")
            for record in transcript
            if record["type"] == "checkpoint_committed"
        ]
        self.assertIn("context_pruned", checked_checkpoints)
        finished = [
            event for event in events if isinstance(event, RunFinishedEvent)
        ]
        self.assertEqual(len(finished), 1)
        self.assertEqual(finished[0].result.output_text, "已按剪枝后的上下文继续")
        self.assertTrue(
            conversation.active_messages[2]["output"].startswith(
                PRUNE_POINTER_PREFIX
            )
        )
        self.assertIs(conversation.active_messages[3], latest)
