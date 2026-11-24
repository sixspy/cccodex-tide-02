"""处理时缓冲再裁：批末裁净 / 双写归档 / 中途收口 / 并发无重写。

口径：
- 启用再裁后，装载先按处理时塞暂存，批末与关窗前按事件时裁净。
- 切流：窗外点从现行缓冲剔除并钉裁指纹，现行缓冲无窗外点。
- 双写：窗外点另进只读归档；现行缓冲无窗外点，归档有。
- 关窗中途失败整批回滚并写 valid=0 收口账，不得半裁。
- 关窗与补装并发靠写锁串行，不得双写/半裁。
- 未启用时基线 happy path 不变。
"""
from __future__ import annotations

import os
import tempfile
import threading
from pathlib import Path

import pytest

os.environ.setdefault("数据目录", tempfile.mkdtemp())
os.environ.setdefault("夹具目录", str(Path(__file__).resolve().parents[2] / "fixtures"))

from app import seed
from app.db import 连接
from app.services import 装载, 关窗, 贯验, 目录, 夹具, 再裁, 时间

窗起 = "2026-11-24T00:00:00Z"
窗止 = "2026-11-24T01:00:00Z"

_表 = (
    "窗规", "网点源", "观测点", "入窗缓冲", "暂存缓冲", "再裁账", "窗外归档",
    "窗切片账", "迟到匣", "窗贯验", "装载批次", "关窗批次",
)


def _清库() -> None:
    conn = 连接()
    try:
        for 表 in _表:
            conn.execute(f"DELETE FROM {表}")
        conn.commit()
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def _净库():
    seed.初始化库()
    _清库()
    seed.初始化库()
    yield
    _清库()


def _开(形态: str) -> None:
    目录.改窗规({"缓冲再裁": "开", "再裁形态": 形态})


def _混批(批="夹具-混出窗") -> dict:
    return 夹具.读取("混入出窗样例")


def _现行事件时() -> list[str]:
    return [r["事件时"] for r in 目录.缓冲列表() if r["命运"] == "在缓冲"]


def test_切流批末裁净与只读账():
    _开("切流")
    报告 = 装载.装一批(_混批())

    assert 报告["暂存"] == 4
    assert 报告["入缓冲"] == 2
    assert 报告["迟到"] == 2

    在缓冲 = [r for r in 目录.缓冲列表() if r["命运"] == "在缓冲"]
    assert len(在缓冲) == 2
    assert all(时间.在窗内(r["事件时"], 窗起, 窗止) for r in 在缓冲)

    只读 = 再裁.只读()
    最新 = 只读["最新"]
    assert 最新 is not None
    assert {"批次", "剔除数", "归档键", "valid"}.issubset(最新)
    assert 最新["批次"] == 报告["再裁"]["批次"]
    assert 最新["剔除数"] == 2
    assert 最新["valid"] is True
    assert 最新["指纹"]
    assert 只读["形态"] == "切流"
    assert 只读["现行缓冲含窗外点"] is False
    assert 只读["脏行数"] == 0
    assert 只读["窗内笔数"] == 2
    assert 只读["暂存待裁"] == 0
    # 切流只钉指纹，不物化只读归档
    assert 再裁.归档列表("") == []


def test_双写归档且现行缓冲无窗外点():
    _开("双写")
    报告 = 装载.装一批(_混批())

    assert 报告["入缓冲"] == 2
    assert 报告["归档"] == 2
    在缓冲 = [r for r in 目录.缓冲列表() if r["命运"] == "在缓冲"]
    assert len(在缓冲) == 2
    assert all(时间.在窗内(r["事件时"], 窗起, 窗止) for r in 在缓冲)

    键 = 报告["再裁"]["归档键"]
    assert 键
    归档 = 再裁.归档列表(键)
    assert len(归档) == 2
    assert {r["事件编号"] for r in 归档} == {"E-mix-3", "E-mix-4"}
    assert not ({r["事件编号"] for r in 归档} & {r["事件编号"] for r in 在缓冲})

    只读 = 再裁.只读()
    assert 只读["现行缓冲含窗外点"] is False
    assert 只读["归档笔数"] == 2
    assert 只读["归档键数"] == 1
    assert 只读["最新"]["valid"] is True


def test_窗内成片在再裁下仍成片且贯验对齐():
    _开("切流")
    报告 = 装载.装一批(夹具.读取("窗内成片样例"))
    assert 报告["入缓冲"] == 3
    关 = 关窗.关当前窗()
    assert 关["成片数"] == 2
    assert len(关["再裁"]) > 0
    assert 关["裁指纹"]
    验 = 贯验.跑一次()
    assert 验["是否对齐"] is True
    assert 验["再裁启用"] is True


def test_关窗前补装出窗点被裁净_切片只读窗内():
    _开("切流")
    装载.装一批({"事件": [
        {"事件编号": "g-1", "运单键": "运单-G", "网点键": "网点-东站", "事件时": "2026-11-24T00:30:00Z"},
    ]})
    # 绕过批末手工塞窗外点，模拟关窗前的历史残留。
    _塞脏行("g-脏", "2026-11-24T02:30:00Z")

    关 = 关窗.关当前窗()
    assert 关["成片数"] == 1
    assert 关["缓冲消耗"] == 1
    assert all(r["事件编号"] != "g-脏" for r in 目录.缓冲列表() if r["命运"] == "在缓冲")
    assert 贯验.跑一次()["是否对齐"] is True


def test_关窗中途打断不半裁现行():
    _开("切流")
    装载.装一批(_混批())

    conn = 连接()
    try:
        conn.execute(
            """CREATE TRIGGER 炸切片 BEFORE INSERT ON 窗切片账
               BEGIN SELECT RAISE(ABORT, '模拟中途失败'); END;"""
        )
        conn.commit()
    finally:
        conn.close()
    try:
        with pytest.raises(Exception):
            关窗.关当前窗()
    finally:
        conn = 连接()
        try:
            conn.execute("DROP TRIGGER IF EXISTS 炸切片")
            conn.commit()
        finally:
            conn.close()

    # 不半裁：切片零、现行窗内点仍在、可区分未收口
    只读 = 再裁.只读()
    assert 只读["最新"]["valid"] is False
    assert 只读["最新"]["收口原因"]
    assert 目录.切片列表() == []
    在缓冲 = [r for r in 目录.缓冲列表() if r["命运"] == "在缓冲"]
    assert len(在缓冲) == 2
    assert 只读["现行缓冲含窗外点"] is False

    # 收口后再关窗可整批成功
    关 = 关窗.关当前窗()
    assert 关["成片数"] == 1
    assert 贯验.跑一次()["是否对齐"] is True


def test_关窗与补装并发不双写不半裁():
    _开("切流")
    装载.装一批({"事件": [
        {"事件编号": "c-1", "运单键": "运单-C", "网点键": "网点-东站", "事件时": "2026-11-24T00:05:00Z"},
        {"事件编号": "c-2", "运单键": "运单-C", "网点键": "网点-西港", "事件时": "2026-11-24T00:55:00Z"},
    ]})
    结果: list = []
    错: list = []

    def 关():
        try:
            结果.append(("关", 关窗.关当前窗()))
        except Exception as e:  # pragma: no cover - 允许锁等待后失败
            错.append(("关", e))

    def 补():
        try:
            结果.append(("补", 装载.装一批({"事件": [
                {"事件编号": "c-3", "运单键": "运单-D", "网点键": "网点-北仓", "事件时": "2026-11-24T00:35:00Z"},
                {"事件编号": "c-4", "运单键": "运单-D", "网点键": "网点-东站", "事件时": "2026-11-24T03:35:00Z"},
            ]})))
        except Exception as e:  # pragma: no cover
            错.append(("补", e))

    t1 = threading.Thread(target=关)
    t2 = threading.Thread(target=补)
    t1.start(); t2.start()
    t1.join(); t2.join()

    # 允许锁冲突按异常暴露，但一旦成功就必须账实一致。
    只读 = 再裁.只读()
    assert 只读["现行缓冲含窗外点"] is False
    在缓冲 = [r for r in 目录.缓冲列表() if r["命运"] == "在缓冲"]
    assert all(时间.在窗内(r["事件时"], 窗起, 窗止) for r in 在缓冲)
    conn = 连接()
    try:
        重复 = conn.execute(
            "SELECT 事件编号, COUNT(*) AS c FROM 入窗缓冲 WHERE 命运='已物化' GROUP BY 事件编号 HAVING c>1"
        ).fetchall()
        重片 = conn.execute(
            "SELECT 运单键, 裁指纹, COUNT(*) AS c FROM 窗切片账 GROUP BY 运单键, 裁指纹 HAVING c>1"
        ).fetchall()
        assert not 重复
        assert not 重片
    finally:
        conn.close()
    # 并发方若因写锁失败，必须留下收口账，不得静默半裁。
    if 错:
        assert any(not a["valid"] for a in 只读["账"])
    else:
        assert len(结果) == 2


def _塞脏行(编号: str, 事件时: str) -> None:
    conn = 连接()
    try:
        conn.execute(
            """INSERT INTO 观测点(事件编号,运单键,网点键,事件时,处理时,命运,批次编号)
               VALUES (?,?,?,?,?,?,?)""",
            (编号, "运单-G", "网点-东站", 事件时, "x", "已装载", "手工"),
        )
        conn.execute(
            """INSERT INTO 入窗缓冲(事件编号,运单键,网点键,事件时,窗号,命运,入缓冲时)
               VALUES (?,?,?,?,?,?,?)""",
            (编号, "运单-G", "网点-东站", 事件时, "W-demo", "在缓冲", "x"),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.mark.parametrize("形态", ["切流", "双写"])
def test_关窗前残留脏行按形态裁净(形态):
    _开(形态)
    装载.装一批({"事件": [
        {"事件编号": "g-1", "运单键": "运单-G", "网点键": "网点-东站", "事件时": "2026-11-24T00:30:00Z"},
    ]})
    _塞脏行("g-脏", "2026-11-24T02:30:00Z")

    关 = 关窗.关当前窗()
    assert 关["成片数"] == 1
    assert 关["缓冲消耗"] == 1
    assert all(r["事件编号"] != "g-脏" for r in 目录.缓冲列表() if r["命运"] == "在缓冲")
    assert 贯验.跑一次()["是否对齐"] is True
    只读 = 再裁.只读()
    assert 只读["现行缓冲含窗外点"] is False
    if 形态 == "双写":
        assert 只读["归档笔数"] >= 1
    else:
        assert any(r["事件编号"] == "g-脏" for r in 目录.迟到列表())


def test_未启用时基线不变():
    装载.装一批(夹具.读取("窗内成片样例"))
    关 = 关窗.关当前窗()
    assert 关["成片数"] == 2
    assert "再裁" not in 关
    assert 再裁.只读()["启用"] is False
