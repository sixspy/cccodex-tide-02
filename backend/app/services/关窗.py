"""关窗：缓冲按运单物化为窗切片。启用再裁时关窗前先按事件时裁净。"""
from __future__ import annotations
import json
import uuid
from collections import defaultdict
from app.db import 连接
from app.seed import 此刻
from app.services import 目录, 时间, 再裁


def 关当前窗() -> dict:
    规 = 目录.窗规()
    窗号 = 规.get("当前窗号", "W-demo")
    窗起 = 规.get("窗起", "")
    窗止 = 规.get("窗止", "")
    态 = 再裁.状态(规)
    批 = f"关窗-{uuid.uuid4().hex[:8]}"
    conn = 连接()
    try:
        if 态["启用"]:
            # 写锁：与补装串行。暂存/裁剪/物化同事务。
            conn.execute("BEGIN IMMEDIATE")
            裁账 = 再裁.关窗前裁净(conn, 批, 窗号, 窗起, 窗止, 态["形态"])
        else:
            conn.execute("BEGIN IMMEDIATE")
            裁账 = None
        rows = conn.execute(
            "SELECT * FROM 入窗缓冲 WHERE 窗号=? AND 命运='在缓冲'", (窗号,)
        ).fetchall()
        if 态["启用"]:
            # 切片只读窗内集：历史残留脏行按选定形态裁净，绝不成片。
            再裁.清残留(conn, 窗号, 窗起, 窗止, 态["形态"])
            rows = [
                r for r in rows
                if 窗起 and 窗止 and 时间.在窗内(r["事件时"], 窗起, 窗止)
            ]
        指纹 = 再裁.取指纹(conn, 窗号) if 态["启用"] else ""
        by_waybill: dict[str, list] = defaultdict(list)
        for r in rows:
            by_waybill[r["运单键"]].append(r)
        成片 = 0
        时刻 = 此刻()
        for 运单, evs in by_waybill.items():
            切片 = f"片-{窗号}-{运单}-{uuid.uuid4().hex[:6]}"
            conn.execute(
                """INSERT INTO 窗切片账(切片编号,窗号,运单键,事件数,窗起,窗止,物化时,命运,裁指纹)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (切片, 窗号, 运单, len(evs), 窗起, 窗止, 时刻, "已成片", 指纹),
            )
            for e in evs:
                conn.execute(
                    "UPDATE 入窗缓冲 SET 命运=? WHERE 事件编号=?",
                    ("已物化", e["事件编号"]),
                )
                conn.execute(
                    "UPDATE 观测点 SET 命运=? WHERE 事件编号=?",
                    ("已成片", e["事件编号"]),
                )
            成片 += 1
        报告 = {"批次编号": 批, "窗号": 窗号, "成片数": 成片, "缓冲消耗": len(rows)}
        if 裁账 is not None:
            报告["再裁"] = 裁账
            报告["裁指纹"] = 指纹
        conn.execute(
            """INSERT INTO 关窗批次(编号,窗号,时刻,成片数,报告) VALUES (?,?,?,?,?)""",
            (批, 窗号, 时刻, 成片, json.dumps(报告, ensure_ascii=False)),
        )
        conn.commit()
        return 报告
    except Exception as 错:
        conn.rollback()
        if 态["启用"]:
            try:
                再裁.收口(批, str(错), 态["形态"], 窗号, "关窗前")
            except Exception:
                pass
        raise
    finally:
        conn.close()
