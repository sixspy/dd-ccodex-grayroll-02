"""duel_schedule_step_cap：同一开关上的爬坡互斥并发双请求。

用法（默认打本机 uvicorn）：
    .venv/bin/python scripts/duel_schedule_step_cap.py
    BASE_URL=http://127.0.0.1:11812 .venv/bin/python scripts/duel_schedule_step_cap.py

行为：
1. 用 admin 造夹具：新开关 -> 正式 10% -> 爬坡下一步 30%（合法步进）。
2. admin / writer 两个会话同时 POST /api/schedules/{id}/apply-next。
3. 断言恰一路 200 落正式、另一路 409 且只落争用痕。
服务未起时自动回落到进程内 TestClient，便于无容器自检。
"""
from __future__ import annotations

import os
import sys
import threading
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:11812")


def _make_client():
    if os.environ.get("DUEL_INPROCESS") == "1":
        from fastapi.testclient import TestClient
        from app import app

        return TestClient(app), TestClient(app)

    import httpx

    return httpx.Client(base_url=BASE_URL, timeout=20), httpx.Client(base_url=BASE_URL, timeout=20)


def _login(client, user: str, password: str) -> dict:
    r = client.post("/api/login", json={"username": user, "password": password})
    r.raise_for_status()
    return {"Authorization": f"Bearer {r.json()['token']}"}


def main() -> int:
    c_admin, c_writer = _make_client()
    h_admin = _login(c_admin, "admin", "admin123")
    h_writer = _login(c_writer, "writer", "writer123")

    key = f"duel.{uuid.uuid4().hex[:8]}"
    fid = c_admin.post("/api/flags", headers=h_admin, json={"key": key, "title": "双写爬坡夹具"}).json()["id"]
    c_admin.post(
        "/api/releases/publish",
        headers=h_admin,
        json={
            "flag_id": fid,
            "env_id": 1,
            "kind": "percent",
            "percent": 10,
            "summary": "双写夹具十比例",
            "use_shadow": False,
        },
    ).raise_for_status()
    sid = c_admin.post(
        "/api/schedules",
        headers=h_admin,
        json={
            "flag_id": fid,
            "env_id": 1,
            "steps_json": '[{"percent":30,"after_hours":0}]',
            "note": "duel_schedule_step_cap",
        },
    ).json()["id"]

    results: dict[str, tuple[int, str]] = {}
    barrier = threading.Barrier(2)

    def fire(name, client, headers):
        barrier.wait()
        r = client.post(f"/api/schedules/{sid}/apply-next", headers=headers, json={})
        results[name] = (r.status_code, r.text)

    t1 = threading.Thread(target=fire, args=("admin", c_admin, h_admin))
    t2 = threading.Thread(target=fire, args=("writer", c_writer, h_writer))
    t1.start(); t2.start(); t1.join(); t2.join()

    print("双请求结果:")
    for name, (code, text) in results.items():
        print(f"  {name}: {code} {text}")

    marks = [
        m
        for m in c_admin.get("/api/locks/contention", headers=h_admin).json()["items"]
        if m["lock_key"] == f"flag-ramp:{fid}"
    ]
    print("争用痕:", marks)

    wins = [n for n, (code, _) in results.items() if code == 200]
    losses = [n for n, (code, _) in results.items() if code == 409]
    if len(wins) != 1 or len(losses) != 1 or not marks:
        print("FAIL: 爬坡互斥未按预期工作")
        return 1

    row = next(x for x in c_admin.get("/api/schedules", headers=h_admin).json()["items"] if x["id"] == sid)
    if row["cursor_step"] != 1:
        print(f"FAIL: 游标推进了 {row['cursor_step']} 次")
        return 1
    print(f"OK: {wins[0]} 抢到锁并落正式；{losses[0]} 仅落争用痕；游标={row['cursor_step']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
