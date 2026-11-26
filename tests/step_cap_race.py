"""红灯 争用：同一开关上的爬坡互斥。

复现夹具：正式 10%，爬坡计划下一步 30%（合法步进）。两个写者同时推进同一条计划。
修复前：apply-next 无锁，两路都 200、游标/发布册双写，且无争用记录，本文件见红。
修复后：恰一路 200 落正式；另一路仅产生争用记录（409），不得落正式。

争用段（锁定/争用痕）与取舍段（步长口径）分别落在 locks 与 schedules 两个模块。
"""
from __future__ import annotations

import threading
import time
import uuid

from fastapi.testclient import TestClient

from app import app


def _h(client, user="admin", password="admin123"):
    r = client.post("/api/login", json={"username": user, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _new_flag(client, h, label):
    key = f"race.{label}.{uuid.uuid4().hex[:8]}"
    r = client.post("/api/flags", headers=h, json={"key": key, "title": f"竞态{label}"})
    assert r.status_code == 200, r.text
    return r.json()["id"], key


def _publish(client, h, fid, env, pct):
    r = client.post(
        "/api/releases/publish",
        headers=h,
        json={
            "flag_id": fid,
            "env_id": env,
            "kind": "percent",
            "percent": pct,
            "summary": "竞态夹具十比例",
            "use_shadow": False,
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def _official(client, h, fid, env=1):
    rows = client.get("/api/rules", headers=h).json()["official"]
    for row in rows:
        if row["flag_id"] == fid and row["env_id"] == env:
            return row
    return None


def test_two_writers_same_switch_ramp_only_one_lands_official():
    with TestClient(app) as c1, TestClient(app) as c2:
        h_admin = _h(c1, "admin", "admin123")
        h_writer = _h(c2, "writer", "writer123")
        fid, _key = _new_flag(c1, h_admin, "duel")
        _publish(c1, h_admin, fid, 1, 10)
        sid = c1.post(
            "/api/schedules",
            headers=h_admin,
            json={
                "flag_id": fid,
                "env_id": 1,
                "steps_json": '[{"percent":30,"after_hours":0}]',
                "note": "双写争用夹具",
            },
        ).json()["id"]

        # 争用窗口：仅在该开关的爬坡锁已落库后人为停留，保证两路真实重叠。
        # 修复前 schedules_svc 尚无 acquire，故不注入；两路会无锁双成而见红。
        import schedules_svc

        real_acquire = getattr(schedules_svc, "acquire", None)
        if real_acquire is not None:

            def _slow_acquire(conn, key, holder):
                real_acquire(conn, key, holder)
                time.sleep(0.25)

            schedules_svc.acquire = _slow_acquire

        results: dict[str, tuple[int, str]] = {}
        barrier = threading.Barrier(2)

        def _fire(name, client, headers):
            barrier.wait()
            r = client.post(f"/api/schedules/{sid}/apply-next", headers=headers, json={})
            results[name] = (r.status_code, r.text)

        try:
            t1 = threading.Thread(target=_fire, args=("admin", c1, h_admin))
            t2 = threading.Thread(target=_fire, args=("writer", c2, h_writer))
            t1.start()
            t2.start()
            t1.join()
            t2.join()
        finally:
            if real_acquire is not None:
                schedules_svc.acquire = real_acquire

        winners = [name for name, (code, _t) in results.items() if code == 200]
        assert len(winners) == 1, (
            f"同一开关上的爬坡互斥失效：应恰一路成功，实际 {results}"
        )

        losers = [name for name in results if name not in winners]
        assert losers, results
        for name in losers:
            code, text = results[name]
            assert code == 409, f"争用败方须 409 且只落争用记录，实际 {name}={code}: {text}"

        marks = [
            m
            for m in c1.get("/api/locks/contention", headers=h_admin).json()["items"]
            if m["lock_key"] == f"flag-ramp:{fid}"
        ]
        assert marks, "争用败方必须留下争用记录"
        assert marks[0]["loser"] in ("admin", "writer"), marks[0]

        rule = _official(c1, h_admin, fid, 1)
        assert rule["percent"] == 30, f"只允许落一次正式步进：{rule}"
        assert rule["version"] == 2, f"版本只许推进一版：{rule}"

        row = next(
            x
            for x in c1.get("/api/schedules", headers=h_admin).json()["items"]
            if x["id"] == sid
        )
        assert row["cursor_step"] == 1, f"游标只许推进一次：{row}"
        assert row["status"] == "done", row

        rels = [
            x
            for x in c1.get("/api/releases", headers=h_admin).json()["official"]
            if x["flag_id"] == fid and x["env_id"] == 1 and x["version"] == rule["version"]
        ]
        assert len(rels) == 1, f"发布册只许落一条爬坡版本：{rels}"

        steps = [
            x
            for x in c1.get("/api/history", headers=h_admin).json()["items"]
            if x.get("flag_id") == fid and x["action"] == "schedule_step"
        ]
        assert len(steps) == 1, f"争用败方不得落正式变更历史：{steps}"
