"""灰度调度草案：预约比例爬坡（本地记录，非外部 cron）。"""
from __future__ import annotations

import time

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from audit_svc import write_audit, write_history
from auth import current_user, require_writer
from db import connect
from locks import acquire, release
from policies import describe_path
from step_policy import assert_ramp_ledger, evaluate_step
from validators import require_int, require_percent, require_str

router = APIRouter(prefix="/api/schedules", tags=["schedules"])


class ScheduleIn(BaseModel):
    flag_id: int
    env_id: int
    steps_json: str  # e.g. [{"percent":10,"after_hours":0},{"percent":30,"after_hours":24}]
    note: str = ""


@router.get("")
def list_schedules(authorization: str | None = Header(default=None)):
    current_user(authorization)
    with connect() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                """
                SELECT s.*, f.key AS flag_key, e.code AS env_code
                FROM rollout_schedules s
                JOIN flags f ON f.id=s.flag_id
                JOIN environments e ON e.id=s.env_id
                ORDER BY s.id DESC
                """
            ).fetchall()
        ]
    return {"items": rows}


@router.post("")
def create_schedule(body: ScheduleIn, authorization: str | None = Header(default=None)):
    user = current_user(authorization)
    require_writer(user)
    fid = require_int(body.flag_id, "开关", 1)
    eid = require_int(body.env_id, "环境", 1)
    steps = require_str(body.steps_json, "爬坡步骤", 8)
    # 轻量校验：必须像 JSON 数组
    if not steps.strip().startswith("["):
        raise HTTPException(400, detail="爬坡步骤须为 JSON 数组文本")
    with connect() as conn:
        if not conn.execute("SELECT 1 FROM flags WHERE id=?", (fid,)).fetchone():
            raise HTTPException(404, detail="开关不存在")
        if not conn.execute("SELECT 1 FROM environments WHERE id=?", (eid,)).fetchone():
            raise HTTPException(404, detail="环境不存在")
        now = time.time()
        cur = conn.execute(
            "INSERT INTO rollout_schedules(flag_id, env_id, steps_json, status, note, created_at, created_by) "
            "VALUES(?,?,?,?,?,?,?)",
            (fid, eid, steps, "planned", body.note or "", now, user["username"]),
        )
        sid = cur.lastrowid
        write_audit(conn, user["username"], "schedule_create", "schedule", sid, steps[:120])
        write_history(conn, user["username"], "schedule", f"爬坡计划 {sid}", flag_id=fid, env_id=eid)
        conn.commit()
    return {"id": sid, "status": "planned"}


@router.post("/{schedule_id}/cancel")
def cancel_schedule(schedule_id: int, authorization: str | None = Header(default=None)):
    user = current_user(authorization)
    require_writer(user)
    with connect() as conn:
        row = conn.execute("SELECT * FROM rollout_schedules WHERE id=?", (schedule_id,)).fetchone()
        if not row:
            raise HTTPException(404, detail="调度不存在")
        if row["status"] != "planned":
            raise HTTPException(400, detail="仅计划中可取消")
        conn.execute("UPDATE rollout_schedules SET status='cancelled' WHERE id=?", (schedule_id,))
        write_history(
            conn,
            user["username"],
            "schedule_cancel",
            f"取消爬坡 {schedule_id}",
            flag_id=row["flag_id"],
            env_id=row["env_id"],
        )
        conn.commit()
    return {"id": schedule_id, "status": "cancelled"}


@router.post("/{schedule_id}/apply-next")
def apply_next(schedule_id: int, authorization: str | None = Header(default=None)):
    """推进到下一步：甲路径同事务写正式规则 + 发布册。

    - 争用段：以「同一开关上的爬坡互斥」为锁键，输家只落争用痕（409）。
    - 取舍段：单步增量 > 20 整笔拒写，正式规则册零增，不落影子草案。
    - 联检段：正式规则与发布册版本须同事务对齐（跨表账实）。
    """
    import json as _json

    user = current_user(authorization)
    require_writer(user)
    with connect() as conn:
        row = conn.execute("SELECT * FROM rollout_schedules WHERE id=?", (schedule_id,)).fetchone()
        if not row or row["status"] not in ("planned", "running"):
            raise HTTPException(400, detail="调度不可推进")
        fid, eid = row["flag_id"], row["env_id"]
        key = f"flag-ramp:{fid}"
        acquire(conn, key, user["username"])
        held = True
        try:
            # 抢到锁后再读一次，避免拿旧游标重复推进。
            row = conn.execute("SELECT * FROM rollout_schedules WHERE id=?", (schedule_id,)).fetchone()
            if not row or row["status"] not in ("planned", "running"):
                raise HTTPException(400, detail="调度不可推进")
            try:
                steps = _json.loads(row["steps_json"])
            except Exception as e:
                raise HTTPException(400, detail="步骤 JSON 无效") from e
            idx = int(row["cursor_step"] or 0)
            if idx >= len(steps):
                raise HTTPException(400, detail="已无下一步")
            step = steps[idx]
            pct = require_percent(step.get("percent", 0))

            now = time.time()
            old = conn.execute(
                "SELECT * FROM flag_rules WHERE flag_id=? AND env_id=? AND official=1", (fid, eid)
            ).fetchone()
            baseline = int(old["percent"]) if old else 0
            verdict = evaluate_step(baseline, pct)

            ver = (old["version"] + 1) if old else 1
            if old:
                conn.execute("DELETE FROM flag_rules WHERE id=?", (old["id"],))
            conn.execute(
                "INSERT INTO flag_rules(flag_id, env_id, kind, percent, cohort_id, version, official, updated_at) "
                "VALUES(?,?,?,?,NULL,?,1,?)",
                (fid, eid, "percent", pct, ver, now),
            )
            conn.execute(
                "INSERT INTO releases(flag_id, env_id, version, summary, status, official, published_at, published_by) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (fid, eid, ver, f"爬坡步进 {baseline}% -> {pct}%", "published", 1, now, user["username"]),
            )
            conn.execute(
                "UPDATE flags SET status='active' WHERE id=? AND status IN ('draft','killed','active')",
                (fid,),
            )
            new_idx = idx + 1
            status = "done" if new_idx >= len(steps) else "running"
            conn.execute(
                "UPDATE rollout_schedules SET cursor_step=?, status=? WHERE id=?",
                (new_idx, status, schedule_id),
            )
            assert_ramp_ledger(conn, fid, eid, ver)
            write_history(
                conn,
                user["username"],
                "schedule_step",
                f"推进到 {pct}%（{baseline}% -> {pct}%）",
                flag_id=fid,
                env_id=eid,
            )
            write_audit(
                conn,
                user["username"],
                "schedule_step",
                "schedule",
                schedule_id,
                f"percent={pct} delta={verdict['delta']} path={describe_path('ramp', False)}",
            )
            conn.commit()
            held = False
        except Exception:
            # 取舍/联检失败即整笔回滚，正式规则册保持零增；再释放锁并提交解锁。
            conn.rollback()
            release(conn, key, user["username"])
            conn.commit()
            held = False
            raise
        finally:
            if held:
                release(conn, key, user["username"])
                conn.commit()
    return {
        "id": schedule_id,
        "applied_percent": pct,
        "baseline_percent": verdict["baseline_percent"],
        "delta": verdict["delta"],
        "status": status,
        "cursor_step": new_idx,
        "version": ver,
    }
