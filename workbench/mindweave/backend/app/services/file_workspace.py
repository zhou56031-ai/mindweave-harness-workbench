"""用户工作区文件：路径限制在 workspace/<user_id> 内，写操作需要审批。"""

from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException

from app.core.settings import REBUILD_ROOT

ROOT = REBUILD_ROOT / "workspace"
MAX_BYTES = 200_000


def _user_root(user_id: str) -> Path:
    root = (ROOT / user_id).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _safe(user_id: str, relative: str) -> Path:
    root = _user_root(user_id)
    target = (root / relative).resolve()
    if target != root and root not in target.parents:
        raise HTTPException(status_code=422, detail="路径必须位于个人工作区内")
    return target


def write_file(user_id: str, path: str, content: str) -> str:
    if len(content.encode("utf-8")) > MAX_BYTES:
        raise HTTPException(status_code=422, detail="文件过大（上限 200KB）")
    target = _safe(user_id, path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"已写入 {target.relative_to(_user_root(user_id))}（{len(content)} 字符）"


def read_file(user_id: str, path: str) -> str:
    target = _safe(user_id, path)
    if not target.exists():
        raise HTTPException(status_code=404, detail="文件不存在")
    return target.read_text(encoding="utf-8", errors="ignore")[:20_000]


def list_files(user_id: str) -> str:
    root = _user_root(user_id)
    items = [str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()]
    return "\n".join(sorted(items)[:200]) or "工作区里还没有文件"
