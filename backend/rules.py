"""可疑质量旗（B）的出路规则。

解析仍由 parser.py 完成；这里只决定“一行该走哪条路”、策略取值和批次状态。
全部是纯函数，方便不连库时单独核对。
"""

SUSPECT_FLAG = "B"

# 两条出路，写进策略栏后当日不得更换。
SUSPECT_EXITS = {
    "quarantine": {
        "name": "只进隔离队列",
        "summary": "可疑旗不入库，只进隔离队列；复核后仍留在队列留档。",
    },
    "land_review": {
        "name": "落库待复核",
        "summary": "可疑旗允许落库，但详情行标待复核，不和实测行混作一种。",
    },
}
EXIT_CODES = tuple(SUSPECT_EXITS)
DEFAULT_SUSPECT_EXIT = "quarantine"

QUARANTINE_PENDING = "待隔离"
QUARANTINE_REVIEWED = "已复核"
QUARANTINE_REASON = "可疑旗，按策略只进隔离队列"
EXIT_DEVIATION = "旗出路偏了"
CONTESTED = "旗争用"
REVIEW_MARK = "待复核"

ROUTE_LAND = "land"
ROUTE_QUARANTINE = "quarantine"
ROUTE_REJECT = "reject"


class UnknownExit(ValueError):
    pass


def exit_name(code):
    item = SUSPECT_EXITS.get(code)
    return item["name"] if item else ""


def exit_choices():
    return [
        {"code": code, "name": item["name"], "summary": item["summary"]}
        for code, item in SUSPECT_EXITS.items()
    ]


def normalize_exit(value):
    value = (value or "").strip()
    if not value:
        return None
    if value not in SUSPECT_EXITS:
        raise UnknownExit(value)
    return value


def resolve_exit(requested, policy_code):
    """把请求里的出路对齐到当日策略。

    返回 (实际生效的出路, 是否偏离)。没有请求出路时沿用策略；
    请求的出路不认识抛 UnknownExit；和策略不同即偏离，生效的仍是策略。
    """
    requested = normalize_exit(requested)
    if policy_code not in SUSPECT_EXITS:
        policy_code = DEFAULT_SUSPECT_EXIT
        if requested:
            policy_code = requested
    return policy_code, bool(requested and requested != policy_code)


def route_line(flag, exit_code, deviation):
    """返回 (去向, 拒绝原因, 落库复核标记)。

    实测/插补照常落库；可疑旗按策略走隔离或落库待复核；
    请求出路偏离策略的可疑行不落库，原因记为“旗出路偏了”。
    观测时刻重复、已在隔离队列等判断在落库前单独做。
    """
    if flag != SUSPECT_FLAG:
        return ROUTE_LAND, None, None
    if deviation:
        return ROUTE_REJECT, EXIT_DEVIATION, None
    if exit_code == "quarantine":
        return ROUTE_QUARANTINE, QUARANTINE_REASON, None
    return ROUTE_LAND, None, REVIEW_MARK


def batch_status(accepted, rejected, quarantined, pending=0):
    """批次状态按计数推出，复核后队列腾空即从“待隔离”变成“已复核”。

    accepted 是落库行数，quarantined 是隔离行数（含已复核），
    rejected 是未接纳行数，pending 是仍在队列里的隔离行数。
    """
    if not (accepted or quarantined):
        return "未接纳"
    if pending:
        return "待隔离"
    if rejected:
        return "部分接纳"
    return "已归集" if accepted else "已复核"
