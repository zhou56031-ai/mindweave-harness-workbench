"""剪枝优先：把可再生的历史工具结果替换为可回溯指针。

本模块只做确定性的字符串替换，不调用模型、不读写文件，也不修改入参。
指针里保留 call_id、工件路径（若已知）、原文 SHA-256、原长和一段预览，
重放或人工排查时可以凭内容寻址重新取回原始结果。
"""

from dataclasses import dataclass, replace
from hashlib import sha256
import re
from typing import Any


# 固定前缀用于识别已经剪枝过的结果，避免对指针再次剪枝丢失原文指纹。
PRUNE_POINTER_PREFIX = "[prune-first] 可再生的工具结果已替换为指针"


@dataclass(frozen=True)
class PrunedMessage:
    """一条已经被替换为指针的工具结果的结构化描述。"""

    # 该消息在原始列表中的位置。
    index: int

    # 工具调用标识，指针可据此定位原始结果。
    call_id: str

    # 产生该结果的工具名；无法判定时为 None。
    tool_name: str | None

    # 内容寻址工件路径；没有持久化工件时为 None。
    artifact: str | None

    # 被替换前的原始字符数。
    original_chars: int

    def to_dict(self) -> dict[str, Any]:
        """转换为可写入 Transcript 的稳定 JSON 对象。"""
        return {
            "index": self.index,
            "call_id": self.call_id,
            "tool_name": self.tool_name,
            "artifact": self.artifact,
            "original_chars": self.original_chars,
        }


# 指针头部的唯一格式；解析与生成共用同一份正则，避免两边漂移。
_POINTER_PATTERN = re.compile(
    r"^\[prune-first\] 可再生的工具结果已替换为指针："
    r"call_id=(?P<call_id>[^；\n]*)；"
    r"tool=(?P<tool>[^；\n]*)"
    r"(?:；artifact=(?P<artifact>[^；\n]*))?"
    r"；sha256=(?P<sha256>[0-9a-f]{64})；"
    r"原长=(?P<original_chars>\d+) 字符。"
)


def _pointer_text(
    *,
    call_id: str,
    tool_name: str | None,
    artifact: str | None,
    original: str,
    preview: str,
    preview_chars: int,
    digest: str,
) -> str:
    """按固定格式生成指针；artifact 只在已知时出现。"""
    artifact_segment = f"；artifact={artifact}" if artifact else ""
    return (
        f"{PRUNE_POINTER_PREFIX}：call_id={call_id}；"
        f"tool={tool_name or '-'}{artifact_segment}；sha256={digest}；"
        f"原长={len(original)} 字符。\n"
        f"前 {preview_chars} 字预览：\n{preview}"
    )


def _parse_pointer(text: str) -> PrunedMessage | None:
    """从指针正文还原结构化描述；非指针或已被改写时返回 None。"""
    header = text.split("\n", 1)[0]
    matched = _POINTER_PATTERN.match(header)
    if matched is None:
        return None
    tool = matched.group("tool")
    artifact = matched.group("artifact")
    return PrunedMessage(
        index=0,
        call_id=matched.group("call_id"),
        tool_name=None if tool in {"", "-"} else tool,
        artifact=artifact or None,
        original_chars=int(matched.group("original_chars")),
    )


def prune_messages(
    messages: list[Any],
    *,
    preview_chars: int,
    min_output_chars: int,
    artifact_lookup: dict[str, str] | None = None,
) -> tuple[list[Any], tuple[PrunedMessage, ...]]:
    """把够长的 function_call_output 替换为指针，返回新列表和剪枝描述。

    纯函数：不修改 messages，也不修改其中的字典；相同输入总是得到相同结果。
    """
    # 1. 同一批消息里的 function_call 提供 call_id → 工具名 映射。
    tool_names: dict[str, str] = {}
    for message in messages:
        if not isinstance(message, dict) or message.get("type") != "function_call":
            continue
        call_id = message.get("call_id")
        name = message.get("name")
        if isinstance(call_id, str) and isinstance(name, str):
            tool_names[call_id] = name

    lookup = artifact_lookup or {}
    pruned_prefix = list(messages)
    records: list[PrunedMessage] = []
    for index, message in enumerate(messages):
        # 2. 只处理字典形式的工具结果，其余消息原样保留。
        if not isinstance(message, dict) or message.get("type") != "function_call_output":
            continue
        output = message.get("output")
        if not isinstance(output, str) or len(output) < min_output_chars:
            continue
        # 3. 已经是指针的结果不再二次剪枝，保留原始 SHA-256 与长度。
        if output.startswith(PRUNE_POINTER_PREFIX):
            continue

        raw_call_id = message.get("call_id")
        call_id = raw_call_id if isinstance(raw_call_id, str) else ""
        raw_name = message.get("name")
        tool_name = tool_names.get(call_id) or (raw_name if isinstance(raw_name, str) else None)
        raw_artifact = lookup.get(call_id)
        artifact = raw_artifact if isinstance(raw_artifact, str) and raw_artifact else None
        preview = output[: max(0, preview_chars)]
        pruned_prefix[index] = {
            **message,
            "output": _pointer_text(
                call_id=call_id,
                tool_name=tool_name,
                artifact=artifact,
                original=output,
                preview=preview,
                preview_chars=preview_chars,
                digest=sha256(output.encode("utf-8")).hexdigest(),
            ),
        }
        records.append(
            PrunedMessage(
                index=index,
                call_id=call_id,
                tool_name=tool_name,
                artifact=artifact,
                original_chars=len(output),
            )
        )
    return pruned_prefix, tuple(records)


def describe_pruned(messages: list[Any]) -> tuple[PrunedMessage, ...]:
    """从已经剪枝的前缀里还原描述，供 boundary 序列化和重放校验。"""
    records: list[PrunedMessage] = []
    for index, message in enumerate(messages):
        if not isinstance(message, dict) or message.get("type") != "function_call_output":
            continue
        output = message.get("output")
        if not isinstance(output, str):
            continue
        parsed = _parse_pointer(output)
        if parsed is not None:
            records.append(replace(parsed, index=index))
    return tuple(records)
