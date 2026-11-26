"""行锁与争用痕（嵌进发布/熔断/爬坡主写路径）。

争用段只负责「谁抢到同一把锁、输家留痕」，不含任何步长/取舍口径；
取舍口径见 step_policy.py，两段物理分开。

锁行必须即时提交，跨连接才看得见：否则两个写者会各自读到空表而双成。
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Header, HTTPException

from auth import current_user
from db import connect

router = APIRouter(prefix="/api/locks", tags=["locks"])
TTL = 20.0


def acquire(conn, key: str, holder: str) -> None:
    """原子抢占：条件 UPSERT + RETURNING，未抢到只落争用痕并抛 409。"""
    now = time.time()
    row = conn.execute(
        "INSERT INTO row_locks(lock_key, holder, expires_at) VALUES(?,?,?) "
        "ON CONFLICT(lock_key) DO UPDATE SET holder=excluded.holder, expires_at=excluded.expires_at "
        "WHERE row_locks.expires_at <= ? OR row_locks.holder = excluded.holder "
        "RETURNING holder",
        (key, holder, now + TTL, now),
    ).fetchone()
    if not row:
        winner = conn.execute(
            "SELECT holder FROM row_locks WHERE lock_key=?", (key,)
        ).fetchone()
        conn.execute(
            "INSERT INTO contention_marks(lock_key, loser, winner, created_at) VALUES(?,?,?,?)",
            (key, holder, winner["holder"] if winner else "", now),
        )
        conn.commit()
        raise HTTPException(409, detail="同一开关上的写锁争用，请稍后重试")
    conn.commit()


def release(conn, key: str, holder: str) -> None:
    conn.execute("DELETE FROM row_locks WHERE lock_key=? AND holder=?", (key, holder))


@router.get("/contention")
def list_contention(authorization: str | None = Header(default=None), limit: int = 40):
    current_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM contention_marks ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return {"items": [dict(r) for r in rows]}
