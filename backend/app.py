from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from auth import authenticate, issue_token, read_user
from db import (
    PolicyLocked,
    _insert_batch,
    connect,
    flag_summary,
    init_db,
    landed_for_batch,
    list_quarantine,
    policy_state,
    review_quarantine,
    write_policy,
)
from parser import FLAGS
from rules import (
    QUARANTINE_REVIEWED,
    UnknownExit,
    exit_choices,
    exit_name,
    normalize_exit,
)


class LoginBody(BaseModel):
    username: str
    password: str


class BatchBody(BaseModel):
    title: str = Field(min_length=1, max_length=80)
    raw_text: str = Field(min_length=1)
    suspect_exit: str | None = None


class PolicyBody(BaseModel):
    exit_code: str


def _stamp(value):
    return value.isoformat(sep=" ", timespec="minutes") if value else None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(lifespan=lifespan)


def _user_or_401(authorization: str | None):
    user = read_user(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    return user


def _writer_or_403(authorization: str | None):
    user = _user_or_401(authorization)
    if user["role"] != "writer":
        raise HTTPException(status_code=403, detail="档案员不能动归集和旗设置")
    return user


def _stamp_policy(state):
    state["exit_name"] = exit_name(state["exit_code"])
    state["options"] = exit_choices()
    return state


@app.get("/api/health")
def health():
    with connect() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok", "service": "tide-telegram-intake"}


@app.post("/api/login")
def login(body: LoginBody):
    user = authenticate(body.username.strip(), body.password)
    if not user:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    return {
        "access_token": issue_token(user),
        "username": user["username"],
        "role": user["role"],
        "title": user["title"],
    }


@app.get("/api/batches")
def list_batches(authorization: str | None = Header(default=None)):
    _user_or_401(authorization)
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT b.id, b.title, b.status, b.accepted_count, b.rejected_count,
                   b.quarantined_count, b.suspect_exit, b.suspect_exit_deviation,
                   b.created_by, b.created_at,
                   (SELECT COUNT(*) FROM quarantine_rows q
                     WHERE q.batch_id = b.id AND q.status = '待隔离') AS pending_count,
                   (SELECT COUNT(*) FROM quarantine_rows q
                     WHERE q.batch_id = b.id AND q.status = '已复核') AS reviewed_count,
                   (SELECT COUNT(*) FROM landed_rows l
                     WHERE l.batch_id = b.id AND l.review_state = '待复核') AS review_count,
                   (SELECT COUNT(*) FROM landed_rows l
                     WHERE l.batch_id = b.id AND l.quality_flag = 'B') AS suspect_landed_count
            FROM intake_batches b
            ORDER BY b.id DESC
            """
        ).fetchall()
        policy = policy_state(conn)
    for row in rows:
        row["created_at"] = _stamp(row["created_at"])
        row["policy_name"] = exit_name(policy["exit_code"])
        row["suspect_exit_name"] = exit_name(row["suspect_exit"] or policy["exit_code"])
        row["suspect_landed"] = (row["suspect_landed_count"] or 0) > 0
    return rows


@app.get("/api/batches/{batch_id}")
def batch_detail(batch_id: int, authorization: str | None = Header(default=None)):
    _user_or_401(authorization)
    with connect() as conn:
        batch = conn.execute(
            """
            SELECT id, title, raw_text, status, accepted_count, rejected_count,
                   quarantined_count, suspect_exit, suspect_exit_deviation, created_by, created_at
            FROM intake_batches WHERE id = %s
            """,
            (batch_id,),
        ).fetchone()
        if not batch:
            raise HTTPException(status_code=404, detail="没有这一批报文")
        landed = landed_for_batch(conn, batch_id)
        rejected = conn.execute(
            "SELECT line_no, raw_line, reason FROM reject_rows WHERE batch_id = %s ORDER BY line_no",
            (batch_id,),
        ).fetchall()
        quarantine = conn.execute(
            """
            SELECT id, line_no, station_code, observed_at, tide_cm, quality_flag,
                   raw_line, reason, status, claimed_by, claimed_at, reviewed_by, reviewed_at
            FROM quarantine_rows WHERE batch_id = %s ORDER BY line_no
            """,
            (batch_id,),
        ).fetchall()
        summary = flag_summary(conn, batch_id)
        policy = policy_state(conn)
    batch["created_at"] = _stamp(batch["created_at"])
    batch["suspect_exit_name"] = exit_name(batch["suspect_exit"] or policy["exit_code"])
    for row in quarantine:
        row["observed_at"] = _stamp(row["observed_at"])
        row["claimed_at"] = _stamp(row["claimed_at"])
        row["reviewed_at"] = _stamp(row["reviewed_at"])
        row["flag_label"] = FLAGS.get(row["quality_flag"], row["quality_flag"])
    return {
        "batch": batch,
        "landed": landed,
        "rejected": rejected,
        "quarantine": quarantine,
        "flag_summary": summary,
        "policy": _stamp_policy(policy),
    }


@app.post("/api/batches")
def create_batch(body: BatchBody, authorization: str | None = Header(default=None)):
    user = _writer_or_403(authorization)
    title = body.title.strip()
    raw_text = body.raw_text.strip()
    if not title or not raw_text:
        raise HTTPException(status_code=400, detail="标题和报文都不能空")
    try:
        requested = normalize_exit(body.suspect_exit)
    except UnknownExit:
        raise HTTPException(status_code=400, detail="旗出路不认识")
    with connect() as conn:
        batch_id = _insert_batch(conn, title, raw_text, user["username"], requested)
        conn.commit()
    return batch_detail(batch_id, authorization)


@app.get("/api/flag-policy")
def get_flag_policy(authorization: str | None = Header(default=None)):
    _user_or_401(authorization)
    with connect() as conn:
        state = policy_state(conn)
    return _stamp_policy(state)


@app.put("/api/flag-policy")
def put_flag_policy(body: PolicyBody, authorization: str | None = Header(default=None)):
    user = _writer_or_403(authorization)
    try:
        code = normalize_exit(body.exit_code)
    except UnknownExit:
        raise HTTPException(status_code=400, detail="旗出路不认识")
    if not code:
        raise HTTPException(status_code=400, detail="旗出路不能空")
    with connect() as conn:
        try:
            state = write_policy(conn, code, user["username"])
        except PolicyLocked as exc:
            conn.rollback()
            raise HTTPException(status_code=409, detail=str(exc))
        conn.commit()
    return _stamp_policy(state)


@app.get("/api/quarantine")
def quarantine_list(status: str | None = None, authorization: str | None = Header(default=None)):
    _user_or_401(authorization)
    with connect() as conn:
        rows = list_quarantine(conn, status)
    for row in rows:
        row["observed_at"] = _stamp(row["observed_at"])
        row["claimed_at"] = _stamp(row["claimed_at"])
        row["reviewed_at"] = _stamp(row["reviewed_at"])
        row["flag_label"] = FLAGS.get(row["quality_flag"], row["quality_flag"])
    return rows


@app.post("/api/quarantine/{row_id}/review")
def quarantine_review(row_id: int, authorization: str | None = Header(default=None)):
    user = _writer_or_403(authorization)
    with connect() as conn:
        result, reason = review_quarantine(conn, row_id, user["username"])
        conn.commit()
    if result == "missing":
        raise HTTPException(status_code=404, detail="隔离队列里没有这一行")
    if result == "already":
        raise HTTPException(status_code=409, detail="这一行已经复核过了")
    return {"row_id": row_id, "status": QUARANTINE_REVIEWED, "reason": reason}


if __name__ == "__main__":
    import os

    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
