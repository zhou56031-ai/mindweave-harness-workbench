"""会话级审批授权：用户选择“本次会话都允许”后，同类工具不再重复弹审批。"""

from __future__ import annotations

import time

_TTL_SECONDS = 3600
_ALLOWED: dict[tuple[str, str], float] = {}


def allow(session_id: str, tool: str) -> None:
    _ALLOWED[(session_id, tool)] = time.time() + _TTL_SECONDS


def is_allowed(session_id: str, tool: str) -> bool:
    expires = _ALLOWED.get((session_id, tool))
    if expires is None:
        return False
    if time.time() > expires:
        _ALLOWED.pop((session_id, tool), None)
        return False
    return True
