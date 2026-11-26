"""红灯 乙：对比矩阵不得把未升正的草案当成已对齐。

复现夹具：正式规则 10%，另有一条未升正的 30% 草案（影子绑定）。
修复前：/api/compare/matrix 只读正式规则，草案既不显形也不报差异，
        矩阵看起来「平稳/已对齐」，本文件见红。
修复后：矩阵单元须暴露 has_pending_shadow / aligned / pending_percent；
        未升正草案与正式不一致时 aligned=false，且正式 percent 不被草案顶替。
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
    key = f"cmp.{label}.{uuid.uuid4().hex[:8]}"
    r = client.post("/api/flags", headers=h, json={"key": key, "title": f"对比{label}"})
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
            "summary": "对比夹具十比例",
            "use_shadow": False,
        },
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


def test_unpromoted_draft_is_not_reported_as_aligned():
    with TestClient(app) as client:
        h = _h(client)
        fid, key = _new_flag(client, h, "draft")
        pub = _publish(client, h, fid, 1, 10)

        # 未升正草案：30%，正式仍应保持 10%。
        sb = client.post(
            "/api/rules/shadow-bind",
            headers=h,
            json={"flag_id": fid, "env_id": 1, "kind": "percent", "percent": 30},
        )
        assert sb.status_code == 200, sb.text

        cell = _matrix_cell(client, h, fid, 1)
        assert cell is not None, "矩阵缺单元"
        assert cell["percent"] == 10, f"矩阵正式比例不得被未升正草案顶替：{cell}"
        assert cell["version"] == pub["version"], f"矩阵正式版本不得被顶替：{cell}"
        assert cell.get("has_pending_shadow") is True, f"未升正草案须显形：{cell}"
        assert cell.get("aligned") is False, f"草案未升正不得当成已对齐：{cell}"
        assert cell.get("pending_percent") == 30, f"须报出待升正草案比例：{cell}"

        detail = client.get(f"/api/compare/flag/{fid}/env/1", headers=h).json()
        assert detail["official_rule"]["percent"] == 10, detail["official_rule"]
        assert detail.get("aligned") is False, f"详情不得当成已对齐：{detail}"
        assert any(
            d["field"] == "percent" and d["shadow"] == 30 for d in detail["diffs"]
        ), f"详情须报出草案差异：{detail['diffs']}"

        ev = client.post(
            "/api/evaluate",
            headers=h,
            json={"flag_key": key, "env_code": "dev", "subject_key": "u-cmp"},
        ).json()
        assert ev["percent"] == 10, f"求值不得使用未升正草案：{ev}"


def test_overstep_does_not_leave_fake_aligned_draft():
    with TestClient(app) as client:
        h = _h(client)
        fid, _key = _new_flag(client, h, "overstep")
        _publish(client, h, fid, 1, 10)

        sid = client.post(
            "/api/schedules",
            headers=h,
            json={
                "flag_id": fid,
                "env_id": 1,
                "steps_json": '[{"percent":50,"after_hours":0}]',
                "note": "超步草案夹具",
            },
        ).json()["id"]
        r = client.post(f"/api/schedules/{sid}/apply-next", headers=h, json={})
        assert r.status_code == 400, f"超步须拒写：{r.status_code} {r.text}"

        cell = _matrix_cell(client, h, fid, 1)
        assert cell["percent"] == 10, f"超步不得改正式比例：{cell}"
        assert cell.get("has_pending_shadow") is False, f"甲路径不得留下超步草案：{cell}"
        assert cell.get("aligned") is True, f"无草案时正式即对齐：{cell}"

        rules = client.get("/api/rules", headers=h).json()
        assert not [s for s in rules["shadows"] if s["flag_id"] == fid], "甲路径不得落规则影子"


def test_promoted_draft_becomes_aligned():
    with TestClient(app) as client:
        h = _h(client)
        fid, _key = _new_flag(client, h, "promote")
        _publish(client, h, fid, 1, 10)
        sb = client.post(
            "/api/rules/shadow-bind",
            headers=h,
            json={"flag_id": fid, "env_id": 1, "kind": "percent", "percent": 30},
        )
        assert sb.status_code == 200, sb.text
        sid = sb.json()["id"]

        pr = client.post(f"/api/rules/shadow/{sid}/promote", headers=h, json={})
        assert pr.status_code == 200, pr.text

        cell = _matrix_cell(client, h, fid, 1)
        assert cell["percent"] == 30, cell
        assert cell.get("has_pending_shadow") is False, cell
        assert cell.get("aligned") is True, f"升正后应报对齐：{cell}"
