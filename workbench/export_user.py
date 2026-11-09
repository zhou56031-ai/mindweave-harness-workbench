"""一键导出当前用户的笔记、知识库清单与会话列表（JSON）。"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8100"
OUT = Path(__file__).resolve().parent / "exports"


def main() -> None:
    client = httpx.Client(timeout=60)
    login = client.post(f"{BASE}/user/login/", json={"username": "demo", "password": "demo1234"})
    login.raise_for_status()
    token = login.json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    user = login.json().get("user", {})

    notes = client.get(f"{BASE}/note/list", headers=headers).json().get("data", {})
    documents = client.get(f"{BASE}/knowledge/list", headers=headers).json().get("data", {})
    sessions = client.get(f"{BASE}/chat/sessions/{user.get('uuid')}", headers=headers).json().get("data", {})

    payload = {
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "user": user,
        "notes": notes,
        "documents": documents,
        "sessions": sessions,
    }
    OUT.mkdir(exist_ok=True)
    path = OUT / f"export-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已导出：{path}")
    print(f"笔记 {len(notes.get('notes', []))} · 文档 {documents.get('total_count', 0)} · 会话 {len(sessions.get('sessions', []))}")


if __name__ == "__main__":
    main()
