import os
import time
from datetime import date

import psycopg
from psycopg.rows import dict_row

from parser import parse_telegram_line, FLAGS
from rules import (
    CONTESTED,
    DEFAULT_SUSPECT_EXIT,
    EXIT_DEVIATION,
    QUARANTINE_PENDING,
    QUARANTINE_REVIEWED,
    REVIEW_MARK,
    ROUTE_LAND,
    ROUTE_QUARANTINE,
    ROUTE_REJECT,
    SUSPECT_EXITS,
    SUSPECT_FLAG,
    batch_status,
    route_line,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS intake_batches (
    id BIGSERIAL PRIMARY KEY,
    title TEXT NOT NULL,
    raw_text TEXT NOT NULL,
    status TEXT NOT NULL,
    accepted_count INTEGER NOT NULL,
    rejected_count INTEGER NOT NULL,
    quarantined_count INTEGER NOT NULL DEFAULT 0,
    suspect_exit TEXT,
    suspect_exit_deviation BOOLEAN NOT NULL DEFAULT FALSE,
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS landed_rows (
    id BIGSERIAL PRIMARY KEY,
    batch_id BIGINT NOT NULL REFERENCES intake_batches(id),
    line_no INTEGER NOT NULL,
    station_code TEXT NOT NULL,
    observed_at TIMESTAMP NOT NULL,
    tide_cm INTEGER NOT NULL,
    quality_flag TEXT NOT NULL,
    review_state TEXT,
    UNIQUE (station_code, observed_at)
);

CREATE TABLE IF NOT EXISTS reject_rows (
    id BIGSERIAL PRIMARY KEY,
    batch_id BIGINT NOT NULL REFERENCES intake_batches(id),
    line_no INTEGER NOT NULL,
    raw_line TEXT NOT NULL,
    reason TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS flag_policy (
    policy_day DATE PRIMARY KEY,
    exit_code TEXT NOT NULL,
    set_by TEXT NOT NULL,
    set_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS quarantine_rows (
    id BIGSERIAL PRIMARY KEY,
    batch_id BIGINT NOT NULL REFERENCES intake_batches(id),
    line_no INTEGER NOT NULL,
    station_code TEXT NOT NULL,
    observed_at TIMESTAMP NOT NULL,
    tide_cm INTEGER NOT NULL,
    quality_flag TEXT NOT NULL,
    raw_line TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL,
    claimed_by TEXT NOT NULL,
    claimed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reviewed_by TEXT,
    reviewed_at TIMESTAMPTZ,
    UNIQUE (station_code, observed_at)
);

CREATE INDEX IF NOT EXISTS quarantine_status_idx ON quarantine_rows (status);
"""

# 建表语句不会给已有表补列，老库靠这一段升级。
MIGRATIONS = """
ALTER TABLE landed_rows ADD COLUMN IF NOT EXISTS review_state TEXT;
ALTER TABLE intake_batches ADD COLUMN IF NOT EXISTS quarantined_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE intake_batches ADD COLUMN IF NOT EXISTS suspect_exit TEXT;
ALTER TABLE intake_batches ADD COLUMN IF NOT EXISTS suspect_exit_deviation BOOLEAN NOT NULL DEFAULT FALSE;
"""

SEED_CLEAN = """吴淞口 11-12 早班
WG01|202611120830|186|A
WG01|202611120900|191|A
BG02|202611120830|142|M
"""

SEED_BROKEN = """外高桥 11-12 错报
LG04|202611121000|205|A
WG01|202611120830|186|A
WG0X|202611121000|10|A
"""


class PolicyLocked(Exception):
    """旗设置动不了，带一句能直接给人看的中文。"""


def connect():
    return psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row)


def init_db():
    last_error = None
    for _ in range(30):
        try:
            with connect() as conn:
                conn.execute(SCHEMA)
                conn.execute(MIGRATIONS)
                count = conn.execute("SELECT COUNT(*) AS n FROM intake_batches").fetchone()["n"]
                if count == 0:
                    _seed(conn)
                conn.commit()
            return
        except psycopg.OperationalError as exc:
            last_error = exc
            time.sleep(1)
    raise last_error


def today():
    return date.today()


def policy_row(conn, day=None):
    return conn.execute(
        "SELECT policy_day, exit_code, set_by, set_at FROM flag_policy WHERE policy_day = %s",
        (day or today(),),
    ).fetchone()


def latest_policy_row(conn):
    """最近一次写进策略栏的出路，用来顺延。"""
    return conn.execute(
        "SELECT policy_day, exit_code, set_by, set_at FROM flag_policy ORDER BY policy_day DESC LIMIT 1"
    ).fetchone()


def pending_quarantine_count(conn):
    return conn.execute(
        "SELECT COUNT(*) AS n FROM quarantine_rows WHERE status = %s",
        (QUARANTINE_PENDING,),
    ).fetchone()["n"]


def effective_exit(conn):
    """当日生效的出路：今日策略 → 最近一次策略（顺延）→ 默认。"""
    row = policy_row(conn)
    if row:
        return row["exit_code"]
    carried = latest_policy_row(conn)
    if carried:
        return carried["exit_code"]
    return DEFAULT_SUSPECT_EXIT


def policy_state(conn):
    row = policy_row(conn)
    pending = pending_quarantine_count(conn)
    lock_reason = None
    if pending:
        lock_reason = "隔离队列尚未腾空，不许改旗设置"
    elif row:
        lock_reason = "本日已写入旗出路，不得更换"
    return {
        "day": (row["policy_day"] if row else today()).isoformat(),
        "exit_code": effective_exit(conn),
        "is_set": row is not None,
        "set_by": row["set_by"] if row else None,
        "lock_reason": lock_reason,
        "pending_count": pending,
    }


def _policy_lock(conn, shared=False):
    """旗设置与归集批次共用一把策略锁。

    归集批次持共享锁：彼此仍可并行，但只要还有批次开着，改策略就得等它提交，
    看得到它可能写进队列的隔离行，不会在“队列未腾空”上抢跑。
    改策略持排他锁：和读当日出路、判偏离互斥，同一批不会按过期出路分流。
    """
    if shared:
        conn.execute("SELECT pg_advisory_xact_lock_shared(hashtext(%s))", ("flag-policy",))
    else:
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("flag-policy",))


def write_policy(conn, exit_code: str, set_by: str):
    if exit_code not in SUSPECT_EXITS:
        raise ValueError("旗出路不认识")
    _policy_lock(conn)
    row = policy_row(conn)
    if row and row["exit_code"] != exit_code:
        raise PolicyLocked("本日不得更换旗出路")
    if pending_quarantine_count(conn):
        raise PolicyLocked("隔离队列尚未腾空，不许改旗设置")
    if not row:
        conn.execute(
            """
            INSERT INTO flag_policy (policy_day, exit_code, set_by)
            VALUES (%s, %s, %s)
            ON CONFLICT (policy_day) DO NOTHING
            """,
            (today(), exit_code, set_by),
        )
        row = policy_row(conn)
        if row and row["exit_code"] != exit_code:
            # 另一笔归集抢先写了今日策略。
            raise PolicyLocked("本日不得更换旗出路")
    return policy_state(conn)


def review_quarantine(conn, row_id: int, reviewed_by: str):
    result = conn.execute(
        """
        UPDATE quarantine_rows
        SET status = %s, reviewed_by = %s, reviewed_at = NOW()
        WHERE id = %s AND status = %s
        RETURNING id, reason, batch_id
        """,
        (QUARANTINE_REVIEWED, reviewed_by, row_id, QUARANTINE_PENDING),
    ).fetchone()
    if result:
        # 这一批可能已经全部腾空队列，状态得跟着变，不然列表还在喊“待隔离”。
        refresh_batch_status(conn, result["batch_id"])
        return "reviewed", result["reason"]
    exists = conn.execute(
        "SELECT status, reason FROM quarantine_rows WHERE id = %s", (row_id,)
    ).fetchone()
    if not exists:
        return "missing", None
    return "already", exists["reason"]


def list_quarantine(conn, status=None):
    sql = """
        SELECT q.id, q.batch_id, b.title AS batch_title, q.line_no, q.station_code,
               q.observed_at, q.tide_cm, q.quality_flag, q.raw_line, q.reason,
               q.status, q.claimed_by, q.claimed_at, q.reviewed_by, q.reviewed_at
        FROM quarantine_rows q
        JOIN intake_batches b ON b.id = q.batch_id
    """
    params = ()
    if status:
        sql += " WHERE q.status = %s"
        params = (status,)
    sql += " ORDER BY q.id"
    return conn.execute(sql, params).fetchall()


def _lock_slots(conn, keys):
    """按同一个全序把所有键一次加齐，两条批次不会反向加锁而死锁。"""
    for station_code, observed_at in sorted(keys, key=lambda k: (k[0], k[1].isoformat())):
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            (f"{station_code}|{observed_at.isoformat()}",),
        )


def _resolve_batch_exit(conn):
    """这一批可疑行按哪条出路走。

    归集请求只能被校验，不能替人改写当天的旗设置；
    生效出路一律取策略栏（今日 → 最近一次 → 默认）。
    是否有行进未接纳记“旗出路偏了”，由逐行判定后汇总，不在这里猜。
    """
    return effective_exit(conn)


def _insert_batch(conn, title: str, raw_text: str, created_by: str, suspect_exit: str | None = None):
    batch = conn.execute(
        """
        INSERT INTO intake_batches
            (title, raw_text, status, accepted_count, rejected_count, quarantined_count, created_by)
        VALUES (%s, %s, '处理中', 0, 0, 0, %s)
        RETURNING id
        """,
        (title, raw_text, created_by),
    ).fetchone()
    accepted = 0
    rejected = 0
    quarantined = 0
    # 持共享策略锁读完当日出路，改策略的人要等这批提交，反过来这批也读不到半截策略。
    _policy_lock(conn, shared=True)
    effective = _resolve_batch_exit(conn)
    # 请求里的出路和当日策略不一致才算偏离；只有出现被拦下的可疑行才会记进批次。
    deviation = bool(suspect_exit and suspect_exit != effective)

    def reject(line_no, raw_line, reason):
        nonlocal rejected
        conn.execute(
            "INSERT INTO reject_rows (batch_id, line_no, raw_line, reason) VALUES (%s, %s, %s, %s)",
            (batch["id"], line_no, raw_line.strip(), reason),
        )
        rejected += 1

    # 第一趟只解析，把要落库/进队列的行收齐；不碰表，便于统一加锁。
    pending = []
    index_of_key = {}
    deviation_hit = False
    for line_no, raw_line in enumerate(raw_text.splitlines(), start=1):
        if not raw_line.strip():
            continue
        parsed, reason = parse_telegram_line(raw_line)
        if reason:
            reject(line_no, raw_line, reason)
            continue
        key = (parsed["station_code"], parsed["observed_at"])
        route, note, review = route_line(parsed["quality_flag"], effective, deviation)
        if route == ROUTE_REJECT:
            if note == EXIT_DEVIATION:
                deviation_hit = True
            reject(line_no, raw_line, note)
            continue
        item = (line_no, raw_line, parsed, key, route, note, review)
        if key in index_of_key:
            # 同一批里同键重报：实测（A/M）优先于可疑（B），同旗之间先到先得。
            # 否则一行可疑旗就能把同一时刻的实测值挤进未接纳。
            kept = pending[index_of_key[key]]
            if kept[2]["quality_flag"] == SUSPECT_FLAG and parsed["quality_flag"] != SUSPECT_FLAG:
                reject(kept[0], kept[1], "同一批里观测时刻重复")
                pending[index_of_key[key]] = item
            else:
                reject(line_no, raw_line, "同一批里观测时刻重复")
            continue
        index_of_key[key] = len(pending)
        pending.append(item)

    # 第二趟：把整批的键按全序一次加齐，再逐行落库，跨批并发才不会被锁顺序坑到。
    _lock_slots(conn, [item[3] for item in pending])

    for line_no, raw_line, parsed, key, route, note, review in pending:
        if route == ROUTE_LAND:
            # 隔离队列占着这个站这个时刻：待隔离在复核前挡落库；
            # 已复核的行按留档继续占位，理由要如实说清楚，别混成一句。
            held = conn.execute(
                "SELECT id, status FROM quarantine_rows WHERE station_code = %s AND observed_at = %s",
                key,
            ).fetchone()
            if held:
                if held["status"] == QUARANTINE_PENDING:
                    reject(line_no, raw_line, "这个站这个时刻还在隔离队列待复核")
                else:
                    reject(line_no, raw_line, "这个站这个时刻已复核留档在隔离队列")
                continue
            # 唯一键在库里定胜负，并发重复提交也只有一行落得下。
            inserted = conn.execute(
                """
                INSERT INTO landed_rows
                    (batch_id, line_no, station_code, observed_at, tide_cm, quality_flag, review_state)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (station_code, observed_at) DO NOTHING
                RETURNING id
                """,
                (
                    batch["id"],
                    line_no,
                    parsed["station_code"],
                    parsed["observed_at"],
                    parsed["tide_cm"],
                    parsed["quality_flag"],
                    review,
                ),
            ).fetchone()
            if not inserted:
                reject(line_no, raw_line, "这个站这个时刻已经落过库")
                continue
            accepted += 1
            continue

        # ROUTE_QUARANTINE：只入隔离队列。先认领的一次留下，另一次记旗争用。
        if route != ROUTE_QUARANTINE:
            raise RuntimeError(f"未知去向 {route}")
        landed = conn.execute(
            "SELECT id FROM landed_rows WHERE station_code = %s AND observed_at = %s",
            key,
        ).fetchone()
        if landed:
            reject(line_no, raw_line, "这个站这个时刻已经落过库")
            continue
        claimed = conn.execute(
            """
            INSERT INTO quarantine_rows
                (batch_id, line_no, station_code, observed_at, tide_cm, quality_flag,
                 raw_line, reason, status, claimed_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (station_code, observed_at) DO NOTHING
            RETURNING id
            """,
            (
                batch["id"],
                line_no,
                parsed["station_code"],
                parsed["observed_at"],
                parsed["tide_cm"],
                parsed["quality_flag"],
                raw_line.strip(),
                note,
                QUARANTINE_PENDING,
                created_by,
            ),
        ).fetchone()
        if not claimed:
            reject(line_no, raw_line, CONTESTED)
            continue
        quarantined += 1

    status = batch_status(accepted, rejected, quarantined, pending=quarantined)
    conn.execute(
        """
        UPDATE intake_batches
        SET status = %s, accepted_count = %s, rejected_count = %s,
            quarantined_count = %s, suspect_exit = %s, suspect_exit_deviation = %s
        WHERE id = %s
        """,
        (status, accepted, rejected, quarantined, effective, deviation_hit, batch["id"]),
    )
    return batch["id"]


def refresh_batch_status(conn, batch_id):
    """复核隔离行后重算批次状态，列表上不再挂着过期的“待隔离”。"""
    accepted = conn.execute(
        "SELECT COUNT(*) AS n FROM landed_rows WHERE batch_id = %s", (batch_id,)
    ).fetchone()["n"]
    rejected = conn.execute(
        "SELECT COUNT(*) AS n FROM reject_rows WHERE batch_id = %s", (batch_id,)
    ).fetchone()["n"]
    held = conn.execute(
        """
        SELECT COUNT(*) AS n,
               SUM(CASE WHEN status = %s THEN 1 ELSE 0 END) AS pending_n
        FROM quarantine_rows WHERE batch_id = %s
        """,
        (QUARANTINE_PENDING, batch_id),
    ).fetchone()
    status = batch_status(accepted, rejected, held["n"], pending=held["pending_n"] or 0)
    conn.execute("UPDATE intake_batches SET status = %s WHERE id = %s", (status, batch_id))


def landed_for_batch(conn, batch_id):
    rows = conn.execute(
        """
        SELECT line_no, station_code, observed_at, tide_cm, quality_flag, review_state
        FROM landed_rows WHERE batch_id = %s ORDER BY line_no
        """,
        (batch_id,),
    ).fetchall()
    for row in rows:
        row["flag_label"] = FLAGS.get(row["quality_flag"], row["quality_flag"])
        row["review_label"] = row["review_state"] or ""
    return rows


def flag_summary(conn, batch_id=None):
    """按站按旗汇总，可疑旗单独计数，并标出它到底有没有入库。"""
    sql = """
        SELECT station_code, quality_flag,
               COUNT(*) AS n,
               SUM(CASE WHEN review_state = %s THEN 1 ELSE 0 END) AS review_n
        FROM landed_rows
    """
    params = [REVIEW_MARK]
    if batch_id is not None:
        sql += " WHERE batch_id = %s"
    sql += " GROUP BY station_code, quality_flag ORDER BY station_code, quality_flag"
    if batch_id is not None:
        params.append(batch_id)
    rows = conn.execute(sql, tuple(params)).fetchall()

    placeholders = ""
    params = []
    if batch_id is not None:
        placeholders = " AND batch_id = %s"
        params.append(batch_id)
    held = conn.execute(
        f"""
        SELECT station_code, COUNT(*) AS n,
               SUM(CASE WHEN status = %s THEN 1 ELSE 0 END) AS pending_n,
               SUM(CASE WHEN status = %s THEN 1 ELSE 0 END) AS reviewed_n
        FROM quarantine_rows WHERE TRUE {placeholders}
        GROUP BY station_code ORDER BY station_code
        """,
        (QUARANTINE_PENDING, QUARANTINE_REVIEWED, *params),
    ).fetchall()

    by_station = {}
    for row in rows:
        key = row["station_code"]
        entry = by_station.setdefault(
            key,
            {
                "station_code": key,
                "landed": 0,
                "review": 0,
                "quarantined": 0,
                "quarantine_pending": 0,
                "quarantine_reviewed": 0,
                "flags": {},
            },
        )
        entry["landed"] += row["n"]
        entry["review"] += row["review_n"] or 0
        flag = entry["flags"].setdefault(
            row["quality_flag"],
            {"quality_flag": row["quality_flag"], "flag_label": FLAGS.get(row["quality_flag"], row["quality_flag"]),
             "landed": 0, "review": 0},
        )
        flag["landed"] += row["n"]
        flag["review"] += row["review_n"] or 0
    for row in held:
        entry = by_station.setdefault(
            row["station_code"],
            {
                "station_code": row["station_code"],
                "landed": 0,
                "review": 0,
                "quarantined": 0,
                "flags": {},
            },
        )
        entry["quarantined"] = row["n"]
        entry["quarantine_pending"] = row["pending_n"] or 0
        entry["quarantine_reviewed"] = row["reviewed_n"] or 0

    for entry in by_station.values():
        entry["flags"] = list(entry["flags"].values())
        if entry.get("quarantine_pending"):
            entry["suspect_state"] = QUARANTINE_PENDING
        elif entry["quarantined"]:
            entry["suspect_state"] = QUARANTINE_REVIEWED
        elif entry["review"]:
            entry["suspect_state"] = REVIEW_MARK
        else:
            entry["suspect_state"] = ""
        # 可疑旗有没有入库：看有没有标待复核的落库行。
        entry["suspect_landed"] = entry["review"] > 0
    return sorted(by_station.values(), key=lambda item: item["station_code"])


def _seed(conn):
    title, _, body = SEED_CLEAN.partition("\n")
    _insert_batch(conn, title, body.strip(), "admin")
    title, _, body = SEED_BROKEN.partition("\n")
    _insert_batch(conn, title, body.strip(), "admin")
