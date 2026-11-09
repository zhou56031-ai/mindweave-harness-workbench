"""把 MindWeave docs 批量导入真实后端（每批 5 个，按 SSE 看结果）。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8100"
ROOT = Path(__file__).resolve().parent / "mindweave"
SKIP = {"uploads"}


def collect() -> list[Path]:
    files: list[Path] = []
    for folder in ("docs", "demo-docs"):
        for path in sorted((ROOT / folder).rglob("*.md")):
            if any(part in SKIP for part in path.parts):
                continue
            files.append(path)
    return files


def main() -> None:
    client = httpx.Client(timeout=600)
    login = client.post(f"{BASE}/user/login/", json={"username": "demo", "password": "demo1234"})
    login.raise_for_status()
    token = login.json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    files = collect()
    print(f"待导入 {len(files)} 篇", flush=True)
    ok = failed = skipped = 0
    for start in range(0, len(files), 5):
        batch = files[start:start + 5]
        multipart = [("files", (p.name, p.read_bytes(), "text/markdown")) for p in batch]
        try:
            with client.stream("POST", f"{BASE}/knowledge/add/multiple/stream",
                               headers=headers, files=multipart) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if not line.startswith("data: "):
                        continue
                    event = json.loads(line[6:])
                    kind = event.get("event_type")
                    if kind == "completed":
                        ok += 1
                    elif kind == "error":
                        message = str(event.get("error_message") or "")
                        if "已导入" in message:
                            skipped += 1
                        else:
                            failed += 1
                            print(f"  失败 {event.get('filename')}: {message}", flush=True)
        except Exception as exc:  # noqa: BLE001
            failed += len(batch)
            print(f"  批次失败 {start}-{start+len(batch)}: {type(exc).__name__} {exc}", flush=True)
        print(f"进度 {min(start+5, len(files))}/{len(files)}  成功 {ok} 跳过 {skipped} 失败 {failed}", flush=True)

    listing = client.get(f"{BASE}/knowledge/list", headers=headers).json()["data"]
    print(f"完成：成功 {ok}，跳过 {skipped}，失败 {failed}，知识库现有 {listing['total_count']} 篇", flush=True)


if __name__ == "__main__":
    main()
