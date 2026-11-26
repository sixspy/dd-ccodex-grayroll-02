"""红灯 甲：单步爬坡增量 > 20% 必须整笔拒写，正式规则册零增。

正确性口径（当班封死）：走甲路径。
- 超步：整笔拒写，正式规则 percent/version 零增，游标不动，不落任何影子草案。
- 合法步进（增量 <= 20）：同事务写正式规则 + 发布册同版本，求值/矩阵同源。

复现夹具：先造「下一步增量已大于二十」的爬坡计划（正式 10% -> 下一步 50%）。
修复前：apply-next 返回 200 并把正式规则写成 50%，本文件见红。
"""
from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from app import app


def _h(client, user="admin", password="admin123"):
    r = client.post("/api/login", json={"username": user, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _new_flag(client, h, label):
    key = f"cap.{label}.{uuid.uuid4().hex[:8]}"
    r = client.post("/api/flags", headers=h, json={"key": key, "title": f"步长{label}"})
    assert r.status_code == 200, r.text
    return r.json()["id"], key


def _publish(client, h, fid, env, pct, summary="步长夹具十比例"):
    r = client.post(
        "/api/releases/publish",
        headers=h,
        json={
            "flag_id": fid,
            "env_id": env,
            "kind": "percent",
            "percent": pct,
            "summary": summary,
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


def _schedule(client, h, fid, env, steps_json):
    r = client.post(
        "/api/schedules",
        headers=h,
        json={"flag_id": fid, "env_id": env, "steps_json": steps_json, "note": "步长夹具"},
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _schedule_row(client, h, sid):
    items = client.get("/api/schedules", headers=h).json()["items"]
    return next((x for x in items if x["id"] == sid), None)


def _evaluate(client, h, key, env="dev", subject="u-step"):
    r = client.post(
        "/api/evaluate",
        headers=h,
        json={"flag_key": key, "env_code": env, "subject_key": subject},
    )
    assert r.status_code == 200, r.text
    return r.json()


def _matrix_cell(client, h, fid, env=1):
    body = client.get("/api/compare/matrix", headers=h).json()
    for row in body["matrix"]:
        if row["flag_id"] == fid:
            for cell in row["cells"]:
                if cell["env_id"] == env:
                    return cell
    return None


def test_overstep_rejected_official_zero_increment():
    with TestClient(app) as client:
        h = _h(client)
        fid, _key = _new_flag(client, h, "overstep")
        pub = _publish(client, h, fid, 1, 10)
        ver0 = pub["version"]

        # 夹具：正式 10%，下一步 50%，单步增量 40 > 20。
        sid = _schedule(client, h, fid, 1, '[{"percent":50,"after_hours":0}]')

        r = client.post(f"/api/schedules/{sid}/apply-next", headers=h, json={})
        assert r.status_code == 400, (
            f"超步必须整笔拒写（甲路径），实际 {r.status_code}: {r.text}"
        )

        rule = _official(client, h, fid, 1)
        assert rule is not None, "正式规则不应消失"
        assert rule["percent"] == 10, f"正式规则被超步改写：{rule}"
        assert rule["version"] == ver0, f"正式版本被超步推进：{rule}"

        row = _schedule_row(client, h, sid)
        assert row["cursor_step"] == 0, f"超步后游标不得推进：{row}"
        assert row["status"] == "planned", f"超步后状态不得变化：{row}"

        # 甲路径不许把超步改落影子草案（禁止第三路）。
        rules = client.get("/api/rules", headers=h).json()
        assert not [s for s in rules["shadows"] if s["flag_id"] == fid], "甲路径不得落规则影子"
        rels = client.get("/api/releases", headers=h).json()
        assert not [s for s in rels["shadows"] if s["flag_id"] == fid], "甲路径不得落影子发布"
        assert not [
            x for x in rels["official"] if x["flag_id"] == fid and x["version"] == ver0 + 1
        ], "超步不得新增正式发布册"


def test_legal_step_same_source_across_rule_release_eval_matrix():
    with TestClient(app) as client:
        h = _h(client)
        fid, key = _new_flag(client, h, "legal")
        pub = _publish(client, h, fid, 1, 10)
        ver0 = pub["version"]
        sid = _schedule(client, h, fid, 1, '[{"percent":30,"after_hours":0}]')

        r = client.post(f"/api/schedules/{sid}/apply-next", headers=h, json={})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["applied_percent"] == 30, body
        assert body["baseline_percent"] == 10, f"须以正式规则为基线联检：{body}"
        assert body["delta"] == 20, f"单步增量须如实记账：{body}"

        rule = _official(client, h, fid, 1)
        assert rule["percent"] == 30, rule
        assert rule["version"] == ver0 + 1, rule

        ev = _evaluate(client, h, key)
        assert ev["percent"] == 30, ev
        assert ev["version"] == rule["version"], f"求值版本须与正式规则同源：{ev} vs {rule}"

        cell = _matrix_cell(client, h, fid, 1)
        assert cell["percent"] == 30, cell
        assert cell["version"] == rule["version"], f"对比矩阵版本须与正式规则同源：{cell}"

        rels = client.get("/api/releases", headers=h).json()
        ledger = [
            x
            for x in rels["official"]
            if x["flag_id"] == fid and x["env_id"] == 1 and x["version"] == rule["version"]
        ]
        assert len(ledger) == 1, f"爬坡步进须与规则版本同事务落发布册：{rels['official']}"
