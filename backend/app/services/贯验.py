"""窗贯验：缓冲/切片/迟到互证。启用再裁时追加裁净检查。"""
from __future__ import annotations
from app.db import 连接
from app.seed import 此刻
from app.services import 时间, 再裁


def 跑一次() -> dict:
    conn = 连接()
    try:
        缓冲 = conn.execute(
            "SELECT COUNT(*) AS c FROM 入窗缓冲 WHERE 命运='在缓冲'"
        ).fetchone()["c"]
        已物化缓冲 = conn.execute(
            "SELECT COUNT(*) AS c FROM 入窗缓冲 WHERE 命运='已物化'"
        ).fetchone()["c"]
        切片事件 = conn.execute(
            "SELECT COALESCE(SUM(事件数),0) AS c FROM 窗切片账"
        ).fetchone()["c"]
        迟到 = conn.execute("SELECT COUNT(*) AS c FROM 迟到匣").fetchone()["c"]
        规 = {r["键"]: r["值"] for r in conn.execute("SELECT 键,值 FROM 窗规").fetchall()}
        态 = 再裁.状态(规)
        错: list[str] = []
        # 基线口径：已物化缓冲事件数应等于切片事件数之和
        if int(已物化缓冲) != int(切片事件):
            错.append(f"撕裂：已物化缓冲={已物化缓冲} 切片事件={切片事件}")
        if 态["启用"]:
            窗起 = 规.get("窗起", "")
            窗止 = 规.get("窗止", "")
            脏 = 0
            for r in conn.execute("SELECT 事件时 FROM 入窗缓冲 WHERE 命运='在缓冲'").fetchall():
                try:
                    if not (窗起 and 窗止 and 时间.在窗内(r["事件时"], 窗起, 窗止)):
                        脏 += 1
                except Exception:
                    脏 += 1
            if 脏:
                错.append(f"再裁脏行：现行缓冲含窗外点 {脏} 行")
            账 = conn.execute(
                "SELECT 批次,valid,收口原因 FROM 再裁账 ORDER BY 编号 DESC LIMIT 1"
            ).fetchone()
            if 账 is None:
                错.append("再裁账缺失：启用后应有裁净账")
            elif 账["valid"] != 1:
                错.append(f"再裁账未收口：批次 {账['批次']} {账['收口原因']}")
        对齐 = 0 if 错 else 1
        说明 = "缓冲物化与切片事件数一致" if not 错 else "; ".join(错)
        时刻 = 此刻()
        conn.execute(
            """INSERT INTO 窗贯验(时刻,缓冲笔数,切片笔数,迟到笔数,是否对齐,说明)
               VALUES (?,?,?,?,?,?)""",
            (时刻, 缓冲, int(切片事件), 迟到, 对齐, 说明),
        )
        conn.commit()
        return {
            "时刻": 时刻,
            "在缓冲": 缓冲,
            "已物化缓冲": int(已物化缓冲),
            "切片事件": int(切片事件),
            "迟到": 迟到,
            "再裁启用": 态["启用"],
            "再裁形态": 态["形态"],
            "是否对齐": bool(对齐),
            "说明": 说明,
        }
    finally:
        conn.close()
