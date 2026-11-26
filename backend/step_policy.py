"""爬坡取舍段：单步增量口径 + 跨表账实联检。

与争用段（locks.py 的行锁/争用痕）物理分开：
- 本模块只回答「这一步合不合法、账实是否对齐」，不碰锁。
- 单个爬坡计划每推进一次，正式比例相对「当前正式规则」的增量不得超过 20。
- 口径择定甲路径：超步整笔拒写，正式规则册零增；不走影子草案（禁止第三路）。
"""
from __future__ import annotations

from fastapi import HTTPException

MAX_STEP_DELTA = 20


def evaluate_step(baseline: int, target: int) -> dict:
    """以正式规则为基线联检单步增量；超步抛 400，调用方不得落任何正式写入。"""
    delta = target - baseline
    if delta > MAX_STEP_DELTA:
        raise HTTPException(
            400,
            detail=(
                f"单步增量超限：{baseline}% -> {target}%（增量 {delta} > {MAX_STEP_DELTA}），"
                "整笔拒写，正式规则零改"
            ),
        )
    return {"baseline_percent": baseline, "target_percent": target, "delta": delta}


def assert_ramp_ledger(conn, flag_id: int, env_id: int, version: int) -> None:
    """同事务内联查正式规则与发布册，账实版本必须一致，否则整笔回滚。"""
    row = conn.execute(
        """
        SELECT r.version AS rule_version, rel.version AS release_version
        FROM flag_rules r
        JOIN releases rel
          ON rel.flag_id = r.flag_id
         AND rel.env_id = r.env_id
         AND rel.version = r.version
         AND rel.official = 1
        WHERE r.flag_id=? AND r.env_id=? AND r.official=1 AND r.version=?
        ORDER BY rel.id DESC
        LIMIT 1
        """,
        (flag_id, env_id, version),
    ).fetchone()
    if not row:
        raise HTTPException(
            500, detail="爬坡账实不一致：正式规则与发布册未在同一事务内对齐版本"
        )
