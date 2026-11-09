"""受限的 Python 执行器：临时目录、超时、输出截断。

不是沙箱：没有内核隔离，也没有网络限制。定位是"让 Agent 算一下、验一下"，
所以必须经过人工审批，并且只允许短代码、短输出。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile

MAX_OUTPUT = 8000
TIMEOUT_SECONDS = 10


def run_python(code: str) -> str:
    """执行一段 Python，返回 stdout/stderr/退出码。"""
    if len(code) > 6000:
        return "代码过长（上限 6000 字符），请拆分后再试。"
    with tempfile.TemporaryDirectory(prefix="mw-code-") as workdir:
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "PYTHONIOENCODING": "utf-8",
            "HOME": workdir,
            "TMPDIR": workdir,
        }
        try:
            proc = subprocess.run(
                [sys.executable, "-I", "-c", code],
                cwd=workdir, env=env, capture_output=True, text=True,
                timeout=TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            return f"执行超时（{TIMEOUT_SECONDS} 秒），进程已终止。"
        parts: list[str] = []
        if proc.stdout.strip():
            parts.append("stdout:\n" + proc.stdout.strip()[:MAX_OUTPUT])
        if proc.stderr.strip():
            parts.append("stderr:\n" + proc.stderr.strip()[:MAX_OUTPUT])
        parts.append(f"退出码：{proc.returncode}")
        return "\n\n".join(parts)
