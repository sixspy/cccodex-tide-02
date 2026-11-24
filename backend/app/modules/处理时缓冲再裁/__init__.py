"""已实现：处理时缓冲再裁

契约：先按处理时暂存，关窗前再按事件时裁进正式缓冲。
实现：`app.services.再裁`（暂存/裁净/双写归档/收口）+ 装载批末裁净 + 关窗写锁裁净。

两种形态（窗规 `再裁形态`）：
- 切流：窗外点从现行缓冲剔除并钉裁指纹；现行缓冲只留窗内集。
- 双写：窗外点进只读归档（`窗外归档`），现行缓冲改读窗内集；两轨读引用不同。

硬约束：
- 暂存、裁剪、物化同事务（关窗 BEGIN IMMEDIATE），同成败。
- 关窗与补装并发靠写锁串行，不半裁、不双写。
- 中途失败整批回滚并写 valid=0 收口账，现行缓冲不留窗外点。
- 批末后现行缓冲是否仍含窗外点：`GET /api/再裁` 的 `现行缓冲含窗外点/脏行数`。
"""

NAME = "处理时缓冲再裁"
CONTRACT = "先按处理时暂存，关窗前再按事件时裁进正式缓冲"
已实现 = True

HOOKS = (
    "before_load",
    "after_load",
    "decide_window",
    "before_close",
    "after_close",
    "extra_verify_checks",
)


def describe() -> dict:
    return {
        "空桩": NAME,
        "状态": "已实现",
        "契约": CONTRACT,
        "实现": "app.services.再裁",
        "形态": ["切流", "双写"],
        "只读接口": "/api/再裁",
        "钩子": list(HOOKS),
        "默认启用": False,
        "说明": "窗规 `缓冲再裁=开` 时启用；默认走基线事件时入窗",
    }


def decide_window(event: dict, rules: dict) -> str:
    """启用再裁时一律先“暂存”，事件时判定推迟到批末/关窗前。"""
    if str((rules or {}).get("缓冲再裁", "关")) == "开":
        return "暂存"
    return "入缓冲"
