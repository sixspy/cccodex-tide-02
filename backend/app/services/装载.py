"""观测点装载 → 事件时入窗或迟到；启用再裁时改为处理时暂存、批末裁净。"""
from __future__ import annotations
import json
import uuid
from app.db import 连接
from app.seed import 此刻
from app.services import 校验, 目录, 时间, 再裁


def 装一批(体: dict) -> dict:
    批次 = 体.get("批次编号") or f"潮批-{uuid.uuid4().hex[:8]}"
    事件 = 体.get("事件") or []
    报告 = {"批次编号": 批次, "入流": 0, "入缓冲": 0, "迟到": 0, "驳回": 0, "明细": []}
    for e in 校验.校验批次(体):
        报告["明细"].append({"命运": "批次预检", "说明": e})
    规 = 目录.窗规()
    窗号 = 规.get("当前窗号", "W-demo")
    窗起 = 规.get("窗起", "2026-11-24T00:00:00Z")
    窗止 = 规.get("窗止", "2026-11-24T01:00:00Z")
    态 = 再裁.状态(规)
    conn = 连接()
    已提交 = False
    try:
        if 态["启用"]:
            # 暂存与批末裁净同事务；与关窗写锁串行，避免半裁/双写。
            conn.execute("BEGIN IMMEDIATE")
        for ev in 事件:
            行错 = 校验.校验事件(ev)
            if 行错:
                报告["驳回"] += 1
                报告["明细"].append({"命运": "驳回-校验", "说明": ";".join(行错)})
                continue
            编号 = ev["事件编号"]
            if conn.execute("SELECT 1 FROM 观测点 WHERE 事件编号=?", (编号,)).fetchone():
                报告["驳回"] += 1
                报告["明细"].append({"命运": "驳回-幂等", "说明": 编号})
                continue
            处理 = 此刻()
            conn.execute(
                """INSERT INTO 观测点(事件编号,运单键,网点键,事件时,处理时,命运,批次编号)
                   VALUES (?,?,?,?,?,?,?)""",
                (编号, ev["运单键"], ev["网点键"], ev["事件时"], 处理, "已装载", 批次),
            )
            报告["入流"] += 1
            if 态["启用"]:
                再裁.暂存(conn, ev, 处理, 批次)
                报告["暂存"] = 报告.get("暂存", 0) + 1
                报告["明细"].append({"命运": "暂存", "事件编号": 编号, "批次编号": 批次})
            elif 时间.在窗内(ev["事件时"], 窗起, 窗止):
                conn.execute(
                    """INSERT INTO 入窗缓冲(事件编号,运单键,网点键,事件时,窗号,命运,入缓冲时)
                       VALUES (?,?,?,?,?,?,?)""",
                    (编号, ev["运单键"], ev["网点键"], ev["事件时"], 窗号, "在缓冲", 处理),
                )
                conn.execute("UPDATE 观测点 SET 命运=? WHERE 事件编号=?", ("已入缓冲", 编号))
                报告["入缓冲"] += 1
                报告["明细"].append({"命运": "入缓冲", "事件编号": 编号, "窗号": 窗号})
            else:
                conn.execute(
                    """INSERT INTO 迟到匣(事件编号,运单键,原因,事件时,入匣时)
                       VALUES (?,?,?,?,?)""",
                    (编号, ev["运单键"], "事件时出窗", ev["事件时"], 处理),
                )
                conn.execute("UPDATE 观测点 SET 命运=? WHERE 事件编号=?", ("已迟到", 编号))
                报告["迟到"] += 1
                报告["明细"].append({"命运": "迟到", "事件编号": 编号})
        if 态["启用"]:
            账 = 再裁.批末裁净(conn, 批次, 窗号, 窗起, 窗止, 态["形态"])
            报告["再裁"] = 账
            报告["入缓冲"] = 账["入窗数"]
            剔除命运 = "归档" if 态["形态"] == "双写" else "迟到"
            报告["归档" if 态["形态"] == "双写" else "迟到"] = 账["剔除数"]
            for 编号 in 账["入窗"]:
                报告["明细"].append({"命运": "入缓冲", "事件编号": 编号, "窗号": 窗号})
            for 编号 in 账["剔除"]:
                报告["明细"].append({"命运": 剔除命运, "事件编号": 编号, "窗号": 窗号})
        conn.execute(
            """INSERT INTO 装载批次(编号,时刻,入流,入缓冲,迟到,报告) VALUES (?,?,?,?,?,?)""",
            (批次, 此刻(), 报告["入流"], 报告["入缓冲"], 报告["迟到"], json.dumps(报告, ensure_ascii=False)),
        )
        conn.commit()
        已提交 = True
    except Exception as 错:
        conn.rollback()
        if 态["启用"] and not 已提交:
            try:
                再裁.收口(批次, str(错), 态["形态"], 窗号, "批末")
            except Exception:
                pass
        raise
    finally:
        conn.close()
    return 报告
