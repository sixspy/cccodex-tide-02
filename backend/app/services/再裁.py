"""处理时缓冲再裁：处理时暂存 → 批末/关窗前按事件时裁净。

形态（窗规 `再裁形态`）：
- 切流：窗外点不入正式缓冲（并从正式缓冲删除），只钉裁指纹，不物化归档。
- 双写：窗外点另进只读归档，正式缓冲只读窗内集，两轨读引用不同。

硬约束：
- 暂存、裁剪、物化同一事务，同成败（关窗服务内一次 BEGIN IMMEDIATE）。
- 关窗与补装并发靠写锁串行，不半裁、不双写。
- 中途失败整批回滚，并写 valid=0 的收口账；正式缓冲不得留窗外点。
- 批末后正式缓冲是否仍含窗外点可经 `只读()` 区分。
"""
from __future__ import annotations

import hashlib
import uuid

from app.db import 连接
from app.seed import 此刻
from app.services import 时间

策略名 = "处理时缓冲再裁"
形态集 = ("切流", "双写")


def _窗规() -> dict:
    conn = 连接()
    try:
        return {r["键"]: r["值"] for r in conn.execute("SELECT 键,值 FROM 窗规").fetchall()}
    finally:
        conn.close()


def 状态(规: dict | None = None) -> dict:
    规 = 规 if 规 is not None else _窗规()
    启用 = str(规.get("缓冲再裁", "关")) == "开"
    形态 = str(规.get("再裁形态", "切流"))
    if 形态 not in 形态集:
        形态 = "切流"
    return {"启用": 启用, "形态": 形态}


def _指纹(窗号: str, 入窗: list[str], 剔除: list[str]) -> str:
    原 = f"{窗号}|入窗:{','.join(sorted(入窗))}|剔除:{','.join(sorted(剔除))}"
    return hashlib.sha256(原.encode("utf-8")).hexdigest()[:16]


def 暂存(conn, ev: dict, 处理时: str, 批次: str) -> None:
    """处理时先塞暂存，不判事件时。调用方负责同一事务。"""
    编号 = ev["事件编号"]
    conn.execute(
        """INSERT INTO 暂存缓冲(事件编号,运单键,网点键,事件时,处理时,批次编号,窗号,命运)
           VALUES (?,?,?,?,?,?,?,?)""",
        (编号, ev["运单键"], ev["网点键"], ev["事件时"], 处理时, 批次, "", "暂存"),
    )
    conn.execute("UPDATE 观测点 SET 命运=? WHERE 事件编号=?", ("已暂存", 编号))


def _裁一批(
    conn,
    where: str,
    参数: tuple,
    阶段: str,
    批次: str,
    窗号: str,
    窗起: str,
    窗止: str,
    形态: str,
) -> dict:
    rows = conn.execute(f"SELECT * FROM 暂存缓冲 WHERE 命运='暂存' AND {where}", 参数).fetchall()
    入窗: list[str] = []
    剔除: list[str] = []
    for r in rows:
        if 时间.在窗内(r["事件时"], 窗起, 窗止):
            入窗.append(r["事件编号"])
            conn.execute(
                """INSERT INTO 入窗缓冲(事件编号,运单键,网点键,事件时,窗号,命运,入缓冲时)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(事件编号) DO UPDATE SET
                     窗号=excluded.窗号, 命运='在缓冲', 入缓冲时=excluded.入缓冲时""",
                (r["事件编号"], r["运单键"], r["网点键"], r["事件时"], 窗号, "在缓冲", 此刻()),
            )
            conn.execute(
                "UPDATE 暂存缓冲 SET 命运=?, 窗号=? WHERE 事件编号=?",
                ("已裁", 窗号, r["事件编号"]),
            )
            conn.execute("UPDATE 观测点 SET 命运=? WHERE 事件编号=?", ("已入缓冲", r["事件编号"]))
        else:
            剔除.append(r["事件编号"])
            # 无论哪一形态，正式缓冲都不得留窗外点。
            conn.execute("DELETE FROM 入窗缓冲 WHERE 事件编号=?", (r["事件编号"],))
            if 形态 == "双写":
                去向 = "已归档"
                conn.execute(
                    "UPDATE 暂存缓冲 SET 命运=?, 窗号=? WHERE 事件编号=?",
                    ("已归档", 窗号, r["事件编号"]),
                )
            else:
                去向 = "已迟到"
                conn.execute(
                    "UPDATE 暂存缓冲 SET 命运=?, 窗号=? WHERE 事件编号=?",
                    ("已切流", 窗号, r["事件编号"]),
                )
                conn.execute(
                    """INSERT INTO 迟到匣(事件编号,运单键,原因,事件时,入匣时)
                       VALUES (?,?,?,?,?)""",
                    (r["事件编号"], r["运单键"], "再裁出窗", r["事件时"], 此刻()),
                )
            conn.execute("UPDATE 观测点 SET 命运=? WHERE 事件编号=?", (去向, r["事件编号"]))
    归档键 = ""
    if 形态 == "双写" and 剔除:
        归档键 = f"归档-{uuid.uuid4().hex[:10]}"
        for 编号 in 剔除:
            r = conn.execute("SELECT * FROM 暂存缓冲 WHERE 事件编号=?", (编号,)).fetchone()
            conn.execute(
                """INSERT OR IGNORE INTO 窗外归档(归档键,事件编号,运单键,网点键,事件时,入档时)
                   VALUES (?,?,?,?,?,?)""",
                (归档键, r["事件编号"], r["运单键"], r["网点键"], r["事件时"], 此刻()),
            )
    指纹 = _指纹(窗号, 入窗, 剔除)
    conn.execute(
        """INSERT INTO 再裁账(批次,阶段,形态,窗号,暂存数,入窗数,剔除数,归档键,指纹,valid,收口原因,时刻)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (批次, 阶段, 形态, 窗号, len(rows), len(入窗), len(剔除), 归档键, 指纹, 1, "", 此刻()),
    )
    return {
        "批次": 批次,
        "阶段": 阶段,
        "形态": 形态,
        "窗号": 窗号,
        "暂存数": len(rows),
        "入窗数": len(入窗),
        "剔除数": len(剔除),
        "归档键": 归档键,
        "指纹": 指纹,
        "入窗": list(入窗),
        "剔除": list(剔除),
        "valid": True,
    }


def 批末裁净(conn, 批次: str, 窗号: str, 窗起: str, 窗止: str, 形态: str) -> dict:
    return _裁一批(conn, "批次编号=?", (批次,), "批末", 批次, 窗号, 窗起, 窗止, 形态)


def 关窗前裁净(conn, 批次: str, 窗号: str, 窗起: str, 窗止: str, 形态: str) -> dict:
    """关窗前把该窗所有未裁暂存按事件时裁净（含上次批末未收口的残留）。"""
    return _裁一批(conn, "窗号=? OR 窗号=''", (窗号,), "关窗前", 批次, 窗号, 窗起, 窗止, 形态)


def 清残留(conn, 窗号: str, 窗起: str, 窗止: str, 形态: str) -> list[str]:
    """防御：现行缓冲里历史残留的窗外点按选定形态裁净，绝不成片。

    返回被清理的事件编号列表。
    """
    残留行 = []
    for r in conn.execute(
        "SELECT * FROM 入窗缓冲 WHERE 窗号=? AND 命运='在缓冲'", (窗号,)
    ).fetchall():
        try:
            ok = bool(窗起 and 窗止 and 时间.在窗内(r["事件时"], 窗起, 窗止))
        except Exception:
            ok = False
        if not ok:
            残留行.append(r)
    if not 残留行:
        return []
    编号表 = [r["事件编号"] for r in 残留行]
    conn.executemany("DELETE FROM 入窗缓冲 WHERE 事件编号=?", [(i,) for i in 编号表])
    if 形态 == "双写":
        归档键 = f"归档-补救-{uuid.uuid4().hex[:8]}"
        for r in 残留行:
            conn.execute(
                """INSERT OR IGNORE INTO 窗外归档(归档键,事件编号,运单键,网点键,事件时,入档时)
                   VALUES (?,?,?,?,?,?)""",
                (归档键, r["事件编号"], r["运单键"], r["网点键"], r["事件时"], 此刻()),
            )
        conn.executemany(
            "UPDATE 观测点 SET 命运=? WHERE 事件编号=?", [("已归档-补救", i) for i in 编号表]
        )
    else:
        for r in 残留行:
            conn.execute(
                """INSERT INTO 迟到匣(事件编号,运单键,原因,事件时,入匣时)
                   VALUES (?,?,?,?,?)""",
                (r["事件编号"], r["运单键"], "再裁残留出窗", r["事件时"], 此刻()),
            )
        conn.executemany(
            "UPDATE 观测点 SET 命运=? WHERE 事件编号=?", [("已迟到-补救", i) for i in 编号表]
        )
    return 编号表


def 收口(批次: str, 原因: str, 形态: str = "", 窗号: str = "", 阶段: str = "批末") -> None:
    """失败后另起事务写 valid=0 收口账，供只读接口区分半裁。"""
    conn = 连接()
    try:
        conn.execute(
            """INSERT INTO 再裁账(批次,阶段,形态,窗号,暂存数,入窗数,剔除数,归档键,指纹,valid,收口原因,时刻)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (批次, 阶段, 形态, 窗号, 0, 0, 0, "", "", 0, 原因, 此刻()),
        )
        conn.commit()
    finally:
        conn.close()


def 取指纹(conn, 窗号: str) -> str:
    row = conn.execute(
        "SELECT 指纹 FROM 再裁账 WHERE valid=1 AND 窗号=? ORDER BY 编号 DESC LIMIT 1",
        (窗号,),
    ).fetchone()
    return row["指纹"] if row and row["指纹"] else ""


def 只读() -> dict:
    规 = _窗规()
    st = 状态(规)
    窗号 = 规.get("当前窗号", "W-demo")
    窗起 = 规.get("窗起", "")
    窗止 = 规.get("窗止", "")
    conn = 连接()
    try:
        账 = []
        for r in conn.execute("SELECT * FROM 再裁账 ORDER BY 编号 DESC LIMIT 10").fetchall():
            d = dict(r)
            d["valid"] = bool(d["valid"])
            账.append(d)
        最新 = dict(账[0]) if 账 else None
        缓冲 = conn.execute("SELECT * FROM 入窗缓冲 WHERE 命运='在缓冲'").fetchall()
        窗内 = 0
        脏 = 0
        for r in 缓冲:
            try:
                ok = bool(窗起 and 窗止 and 时间.在窗内(r["事件时"], 窗起, 窗止))
            except Exception:
                ok = False
            if ok:
                窗内 += 1
            else:
                脏 += 1
        暂存待裁 = conn.execute(
            "SELECT COUNT(*) AS c FROM 暂存缓冲 WHERE 命运='暂存'"
        ).fetchone()["c"]
        归档笔数 = conn.execute("SELECT COUNT(*) AS c FROM 窗外归档").fetchone()["c"]
        归档键数 = conn.execute(
            "SELECT COUNT(DISTINCT 归档键) AS c FROM 窗外归档"
        ).fetchone()["c"]
    finally:
        conn.close()
    下一步 = (
        "已启用：批末与关窗前都会按事件时裁净；可点「跑窗贯验」复核"
        if st["启用"]
        else "未启用：当前走基线事件时入窗；可在窗规点「启用再裁」"
    )
    return {
        "策略": 策略名,
        "形态": st["形态"],
        "启用": st["启用"],
        "窗号": 窗号,
        "最新": 最新,
        "账": 账,
        "现行缓冲含窗外点": 脏 > 0,
        "脏行数": 脏,
        "窗内笔数": 窗内,
        "暂存待裁": int(暂存待裁),
        "归档笔数": int(归档笔数),
        "归档键数": int(归档键数),
        "下一步": 下一步,
    }


def 归档列表(键: str) -> list[dict]:
    conn = 连接()
    try:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM 窗外归档 WHERE 归档键=? ORDER BY 事件时", (键,)
            ).fetchall()
        ]
    finally:
        conn.close()
