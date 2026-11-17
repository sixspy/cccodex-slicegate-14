import hashlib
import secrets
import sqlite3
from datetime import date as date_cls, datetime as datetime_cls
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import db
from doctrine import (
    CLAIM_STATE_ACTIVE,
    CLAIM_STATE_DONE,
    DOCTRINE_CODE,
    DOCTRINE_LABEL,
    DOCTRINE_PINNED_ON,
    DOCTRINE_REJECTED_RULE,
    DOCTRINE_RULE,
    DOCTRINE_SHA,
    VERDICT_BASELINE,
    VERDICT_CONTENTION,
    VERDICT_DEVIATION,
    VERDICT_PASS,
    VERDICT_PENDING,
)
import handoff

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DB_PATH = DATA / "app.db"
FRONTEND = ROOT / "frontend"
SESSIONS: dict[str, str] = {}

PRODUCT = "日切放行"
SKELETON = "业务日分区切片登记后裁决完整度，完整才放行下游，并与消费水位对账"


def connect():
    return db.connect(DB_PATH)


def sha(p: str) -> str:
    return hashlib.sha256(p.encode("utf-8")).hexdigest()


def ensure_users(conn):
    if conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 0:
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("admin", sha("123456"), "管理员"))
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("viewer", sha("view123"), "只读"))


def seed(conn):
    if conn.execute("SELECT COUNT(*) c FROM slices").fetchone()["c"]:
        return
    rows = [
        ("SL-2026-11-01-east", "2026-11-01", "east", 980, 1000, "部分完整", "未放行"),
        ("SL-2026-11-01-west", "2026-11-01", "west", 1000, 1000, "完整", "已放行"),
        ("SL-2026-11-02-east", "2026-11-02", "east", 0, 1000, "待检", "未放行"),
    ]
    for r in rows:
        conn.execute(
            "INSERT INTO slices(slice_no,biz_date,partition_key,row_count,expected_count,"
            "completeness,release_status) VALUES (?,?,?,?,?,?,?)", r)
    conn.execute(
        "INSERT INTO releases(slice_id,consumer,status,note,created_at,doctrine_sha,check_id,operator)"
        " VALUES (2,'ads_order','成功','存量完整切片放行','2026-11-01T18:00:00','',0,'seed')")
    conn.execute(
        "INSERT INTO watermarks(consumer,biz_date,watermark,source_slice,prev_watermark,event,"
        "ref_release_id,ref_check_id,operator,doctrine_sha) VALUES "
        "('ads_order','2026-11-01','2026-11-01T18:00:00','SL-2026-11-01-west','','存量基线',1,0,'seed','')")


def init_db():
    with connect() as conn:
        conn.executescript(db.SCHEMA)
        db.migrate(conn)
        ensure_users(conn)
        seed(conn)
        handoff.ensure_policy(conn)


class LoginIn(BaseModel):
    username: str
    password: str


def require_user(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "未登录")
    u = SESSIONS.get(authorization.removeprefix("Bearer ").strip())
    if not u:
        raise HTTPException(401, "登录已失效")
    return u


app = FastAPI(title=PRODUCT)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])


@app.on_event("startup")
def _start():
    init_db()


def _doctrine_payload() -> dict:
    return {
        "code": DOCTRINE_CODE,
        "label": DOCTRINE_LABEL,
        "rule": DOCTRINE_RULE,
        "rejected_rule": DOCTRINE_REJECTED_RULE,
        "sha": DOCTRINE_SHA,
        "pinned_on": DOCTRINE_PINNED_ON,
    }


def _refuse(status_code: int, code: str, message: str, **extra):
    raise HTTPException(status_code, {"code": code, "message": message, **extra})


@app.get("/api/health")
def health():
    with connect() as c:
        n = c.execute("SELECT COUNT(*) c FROM slices").fetchone()["c"]
        pinned = handoff.policy_state(c)["ok"]
    return {"ok": True, "product": PRODUCT, "skeleton": SKELETON, "slices": n,
            "db": str(DB_PATH), "doctrine": _doctrine_payload(), "policy_pinned": pinned}


@app.post("/api/login")
def login(body: LoginIn):
    with connect() as c:
        row = c.execute("SELECT username, role FROM users WHERE username=? AND password_sha=?",
                        (body.username, sha(body.password))).fetchone()
    if not row:
        raise HTTPException(401, "账号或密码错误")
    tok = secrets.token_hex(16)
    SESSIONS[tok] = row["username"]
    return {"token": tok, "username": row["username"], "role": row["role"]}


# ---------------------------------------------------------------------------
# 登记与裁决
# ---------------------------------------------------------------------------

@app.get("/api/slices")
def list_slices(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        rows = c.execute("SELECT * FROM slices ORDER BY id").fetchall()
        items = []
        for r in rows:
            item = dict(r)
            chk = c.execute(
                "SELECT check_no,verdict,state,created_at FROM handoff_checks "
                "WHERE slice_id=? ORDER BY id DESC LIMIT 1", (r["id"],)).fetchone()
            wm = c.execute(
                "SELECT id,watermark,event,biz_date FROM watermarks "
                "WHERE source_slice=? ORDER BY id DESC LIMIT 1", (r["slice_no"],)).fetchone()
            item["handoff_check_no"] = chk["check_no"] if chk else ""
            item["handoff_verdict"] = chk["verdict"] if chk else VERDICT_PENDING
            item["watermark_written"] = bool(wm)
            item["watermark_value"] = wm["watermark"] if wm else ""
            item["watermark_event"] = wm["event"] if wm else ""
            items.append(item)
    return {"items": items, "doctrine": _doctrine_payload()}


class SliceIn(BaseModel):
    slice_no: str
    biz_date: str
    partition_key: str
    row_count: int
    expected_count: int


@app.post("/api/slices")
def add_slice(body: SliceIn, authorization: str | None = Header(default=None)):
    require_user(authorization)
    try:
        date_cls.fromisoformat(body.biz_date)
    except (TypeError, ValueError):
        raise HTTPException(422, "业务日格式须为 YYYY-MM-DD")
    comp = ("完整" if body.row_count >= body.expected_count and body.expected_count > 0
            else ("部分完整" if body.row_count > 0 else "待检"))
    with connect() as c:
        try:
            cur = c.execute(
                "INSERT INTO slices(slice_no,biz_date,partition_key,row_count,expected_count,"
                "completeness,release_status) VALUES (?,?,?,?,?,?,?)",
                (body.slice_no, body.biz_date, body.partition_key, body.row_count,
                 body.expected_count, comp, "未放行"))
            return {"id": cur.lastrowid, "completeness": comp}
        except sqlite3.IntegrityError:
            raise HTTPException(409, "切片号已存在")


@app.post("/api/slices/{sid}/evaluate")
def evaluate_slice(sid: int, authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        row = c.execute("SELECT * FROM slices WHERE id=?", (sid,)).fetchone()
        if not row:
            raise HTTPException(404, "切片不存在")
        comp = ("完整" if row["row_count"] >= row["expected_count"] and row["expected_count"] > 0
                else ("部分完整" if row["row_count"] > 0 else "待检"))
        c.execute("UPDATE slices SET completeness=? WHERE id=?", (comp, sid))
    return {"id": sid, "completeness": comp}


# ---------------------------------------------------------------------------
# 衔接目录 / 策略条 / 核验
# ---------------------------------------------------------------------------

@app.get("/api/handoff/policy")
def get_policy(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        state = handoff.policy_state(c)
    return {"policy": state, "doctrine": _doctrine_payload(), "pinned": state["ok"]}


@app.get("/api/handoff/evaluate")
def precheck(consumer: str, biz_date: str, authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        result = handoff.evaluate(c, consumer, biz_date)
        existing = c.execute(
            "SELECT id,watermark FROM watermarks WHERE consumer=? AND biz_date=? AND event=?",
            (consumer, biz_date, handoff.EVENT_ADVANCE)).fetchone()
        policy = handoff.policy_state(c)
    result["policy_pinned"] = policy["ok"]
    result["already_advanced"] = bool(existing)
    if existing:
        result["verdict"] = VERDICT_CONTENTION
        result["detail"] = f"同日已有水位推进 {existing['watermark']}，后手即衔接争用，不得写成成功。"
    return result


@app.get("/api/handoff/checks")
def list_checks(consumer: str | None = None, biz_date: str | None = None,
                verdict: str | None = None, limit: int = 200,
                authorization: str | None = Header(default=None)):
    require_user(authorization)
    sql = "SELECT * FROM handoff_checks WHERE 1=1"
    params: list[Any] = []
    if consumer:
        sql += " AND consumer=?"
        params.append(consumer)
    if biz_date:
        sql += " AND biz_date=?"
        params.append(biz_date)
    if verdict:
        sql += " AND verdict=?"
        params.append(verdict)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, min(limit, 500)))
    with connect() as c:
        rows = [dict(r) for r in c.execute(sql, params)]
    return {"items": rows, "doctrine": _doctrine_payload()}


@app.get("/api/handoff/catalog")
def catalog(authorization: str | None = Header(default=None)):
    """衔接目录：目录、策略条、水位、切片四份视图同库可核。"""
    require_user(authorization)
    with connect() as c:
        keys = set()
        for table, cols in [
            ("handoff_checks", "consumer,biz_date"),
            ("releases", "consumer,COALESCE((SELECT biz_date FROM slices s WHERE s.id=releases.slice_id),'')"),
            ("watermarks", "consumer,biz_date"),
        ]:
            for r in c.execute(f"SELECT DISTINCT {cols} FROM {table}"):
                keys.add((r[0], r[1]))
        keys = {(k, d) for k, d in keys if k and d}
        groups = []
        for consumer, biz_date in sorted(keys):
            slices = [dict(r) for r in c.execute(
                "SELECT * FROM slices WHERE biz_date=? ORDER BY partition_key", (biz_date,))]
            checks = [dict(r) for r in c.execute(
                "SELECT * FROM handoff_checks WHERE consumer=? AND biz_date=? ORDER BY id",
                (consumer, biz_date))]
            releases = [dict(r) for r in c.execute(
                "SELECT r.*, s.slice_no FROM releases r LEFT JOIN slices s ON s.id=r.slice_id "
                "WHERE r.consumer=? AND s.biz_date=? ORDER BY r.id", (consumer, biz_date))]
            watermarks = [dict(r) for r in c.execute(
                "SELECT * FROM watermarks WHERE consumer=? AND biz_date=? ORDER BY id",
                (consumer, biz_date))]
            advances = [w for w in watermarks if w["event"] == handoff.EVENT_ADVANCE]
            success = [r for r in releases if r["status"] == handoff.RELEASE_SUCCESS]
            pass_checks = [h for h in checks if h["verdict"] == VERDICT_PASS]
            problems = []
            if len(advances) > 1:
                problems.append("同日出现多条水位推进")
            for r in releases:
                # 存量放行发生在衔接闸上线前，按基线对账，不套今日硬闸规则。
                if r["status"] == "成功" and not r["doctrine_sha"]:
                    linked = [w for w in watermarks if w["ref_release_id"] == r["id"]]
                    if not linked:
                        problems.append(f"存量放行 {r['id']} 无对应水位")
                    elif not any(w["event"] in (handoff.EVENT_BASELINE, "存量基线")
                                 for w in linked):
                        problems.append(f"存量放行 {r['id']} 的水位未标基线")
            new_success = [r for r in success if r["doctrine_sha"]]
            if new_success and not pass_checks:
                problems.append("有成功放行但无通过的衔接检查")
            if advances and not new_success:
                problems.append("有水位推进但无成功放行")
            if new_success and not advances:
                problems.append("有成功放行但无水位推进")
            if pass_checks and not advances:
                problems.append("衔接检查通过但无水位推进")
            for w in advances:
                if not any(h["id"] == w["ref_check_id"] for h in pass_checks):
                    problems.append(f"水位 {w['id']} 未挂到通过的衔接检查")
            groups.append({
                "consumer": consumer,
                "biz_date": biz_date,
                "slices": slices,
                "checks": checks,
                "releases": releases,
                "watermarks": watermarks,
                "consistent": not problems,
                "problems": problems,
            })
        policy = handoff.policy_state(c)
        detail = handoff.watermark_detail(c)
    return {
        "doctrine": _doctrine_payload(),
        "policy": policy,
        "groups": groups,
        "watermarks": detail,
        "db": str(DB_PATH),
        "same_db": True,
    }


@app.get("/api/handoff/audit")
def audit(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        result = handoff.audit(c, DB_PATH)
    return result


# ---------------------------------------------------------------------------
# 放行闸：唯一入口，甲口径硬校验
# ---------------------------------------------------------------------------

class ReleaseIn(BaseModel):
    consumer: str = Field(min_length=1)
    # 以下字段只用于识别偏离口径的调用，正常放行一律不传。
    policy_code: str | None = None
    today_watermark: str | None = None
    force: bool = False


def _record_refusal(conn, slice_row, consumer, operator, *, verdict, detail, note,
                    state=CLAIM_STATE_DONE, result=None, missing_dates=None):
    check_id, check_no = handoff.add_check(
        conn, consumer=consumer, biz_date=slice_row["biz_date"], verdict=verdict,
        state=state, operator=operator, detail=detail, slice_row=slice_row,
        result=result, missing_dates=missing_dates)
    release_id = handoff.insert_release(
        conn, slice_row, consumer, handoff.RELEASE_REFUSED,
        f"{detail}｜{note}", operator, check_id)
    # 拒绝也要双向可核：检查 → 放行、检查 → 水位（此路径无水位）。
    handoff.finish_check(conn, check_id, verdict=verdict, state=state,
                         release_id=release_id)
    return check_id, check_no, release_id


def _is_contended(conn, consumer, biz_date):
    return conn.execute(
        "SELECT * FROM watermarks WHERE consumer=? AND biz_date=? AND event=?",
        (consumer, biz_date, handoff.EVENT_ADVANCE)).fetchone()


@app.post("/api/slices/{sid}/release")
def release(sid: int, body: ReleaseIn, authorization: str | None = Header(default=None)):
    operator = require_user(authorization)
    consumer = (body.consumer or "").strip()
    if not consumer:
        raise HTTPException(422, "下游不能为空")

    with connect() as c:
        slice_row = c.execute("SELECT * FROM slices WHERE id=?", (sid,)).fetchone()
        if not slice_row:
            raise HTTPException(404, "切片不存在")
        slice_row = dict(slice_row)
        policy = handoff.policy_state(c)

    # 口径偏离一律先判并留痕，不写水位；哪怕切片本身未完整也要记下偏离。
    deviation = None
    if policy["code"] and body.policy_code and body.policy_code != policy["code"]:
        deviation = (f"调用方要求口径 {body.policy_code}，与本日钉死口径 {policy['code']} 不符，"
                     "拒绝执行并按衔接口径偏了留痕。")
    elif body.force:
        deviation = "调用方要求强制放行，偏离衔接说明（绕开昨日现行水位核验），按衔接口径偏了拒绝。"
    elif body.today_watermark is not None:
        deviation = "调用方自带今日水位，可能覆盖昨日现行水位，按衔接口径偏了拒绝。"

    # 一个 BEGIN IMMEDIATE 事务包住「口径 → 完整度 → 争用 → 核验 → 放行 → 水位推进」：
    # SQLite 写锁把两名衔接值班串行化，后手必然看到前手结果，不会双重推进。
    # 拒绝记录先在同一事务内写完，出事务后再抛：留痕不能随回滚消失。
    refusal = None
    with connect() as c, db.tx(c):
        live_policy = handoff.policy_state(c)
        if not live_policy["ok"]:
            check_id, check_no, _ = _record_refusal(
                c, slice_row, consumer, operator, verdict=VERDICT_DEVIATION,
                detail="策略条口径与钉死口径不一致，放行闸关闭。", note="policy_pinned=False")
            refusal = (409, {
                "code": VERDICT_DEVIATION, "message": "策略条口径已被改写，放行拒绝。",
                "check_no": check_no, "doctrine": _doctrine_payload()})
        elif deviation:
            check_id, check_no, _ = _record_refusal(
                c, slice_row, consumer, operator, verdict=VERDICT_DEVIATION,
                detail=deviation, note="deviation_detected")
            refusal = (409, {
                "code": VERDICT_DEVIATION, "message": deviation,
                "check_no": check_no, "doctrine": _doctrine_payload()})
        elif slice_row["completeness"] != "完整":
            handoff.insert_release(
                c, slice_row, consumer, handoff.RELEASE_REFUSED, "完整度未达标",
                operator, 0)
            refusal = (409, {"code": "未完整", "message": "未完整禁止放行。"})
        else:
            existing = _is_contended(c, consumer, slice_row["biz_date"])
            already = c.execute("SELECT release_status FROM slices WHERE id=?",
                                (sid,)).fetchone()
            if existing or (already and already["release_status"] == "已放行"):
                detail = (f"衔接争用：{consumer} / {slice_row['biz_date']} 已由前手完成"
                          f"（水位 {existing['watermark'] if existing else '无'}），"
                          "后手不得静默写成成功。")
                check_id, check_no, _ = _record_refusal(
                    c, slice_row, consumer, operator, verdict=VERDICT_CONTENTION,
                    detail=detail, note="handoff_contention")
                refusal = (409, {"code": VERDICT_CONTENTION, "message": detail,
                                 "check_no": check_no, "doctrine": _doctrine_payload()})
            else:
                result = handoff.evaluate(c, consumer, slice_row["biz_date"])
                if result["verdict"] != VERDICT_PASS:
                    check_id, check_no, _ = _record_refusal(
                        c, slice_row, consumer, operator, verdict=result["verdict"],
                        detail=result["detail"], note="handoff_gate", result=result)
                    refusal = (409, {
                        "code": result["verdict"], "message": result["detail"],
                        "check_no": check_no, "missing_dates": result["missing_dates"],
                        "doctrine": _doctrine_payload()})
                else:
                    check_id, check_no = handoff.add_check(
                        c, consumer=consumer, biz_date=slice_row["biz_date"],
                        verdict=VERDICT_PENDING, state=CLAIM_STATE_ACTIVE,
                        operator=operator, detail="核验通过，写入放行与水位。",
                        slice_row=slice_row, result=result)
                    release_id = handoff.insert_release(
                        c, slice_row, consumer, handoff.RELEASE_SUCCESS,
                        "完整切片放行；昨日现行水位 " + result["prev_watermark"] +
                        f"（{result['prev_biz_date']}）已核。", operator, check_id)
                    wm_id = handoff.insert_advance_watermark(
                        c, consumer=consumer, biz_date=slice_row["biz_date"],
                        value=result["today_watermark"], source_slice=slice_row["slice_no"],
                        prev_watermark=result["prev_watermark"], release_id=release_id,
                        check_id=check_id, operator=operator)
                    handoff.finish_check(
                        c, check_id, verdict=VERDICT_PASS, state=CLAIM_STATE_DONE,
                        release_id=release_id, watermark_id=wm_id, detail=result["detail"],
                        result=result)
                    c.execute("UPDATE slices SET release_status='已放行' WHERE id=?", (sid,))

    if refusal:
        raise HTTPException(refusal[0], refusal[1])

    return {
        "ok": True,
        "check_no": check_no,
        "check_id": check_id,
        "release_id": release_id,
        "watermark_id": wm_id,
        "watermark": result["today_watermark"],
        "prev_watermark": result["prev_watermark"],
        "prev_biz_date": result["prev_biz_date"],
        "doctrine": _doctrine_payload(),
    }


# ---------------------------------------------------------------------------
# 消费水位（只读视图）与基线补登
# ---------------------------------------------------------------------------

@app.get("/api/releases")
def list_releases(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        rows = c.execute(
            "SELECT r.*, s.slice_no, s.biz_date FROM releases r "
            "JOIN slices s ON s.id=r.slice_id ORDER BY r.id DESC").fetchall()
    return {"items": [dict(r) for r in rows]}


@app.get("/api/watermarks")
def list_wm(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        rows = c.execute("SELECT * FROM watermarks ORDER BY id DESC").fetchall()
        detail = handoff.watermark_detail(c)
    return {"items": [dict(r) for r in rows], "detail": detail,
            "doctrine": _doctrine_payload()}


@app.get("/api/watermarks/detail")
def wm_detail(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"detail": handoff.watermark_detail(c), "doctrine": _doctrine_payload()}


class BaselineIn(BaseModel):
    consumer: str = Field(min_length=1)
    biz_date: str
    watermark: str
    note: str = ""


@app.post("/api/handoff/baseline")
def baseline(body: BaselineIn, authorization: str | None = Header(default=None)):
    """基线补登：唯一允许补齐历史水位缺口的口径内动作；只追加不覆盖。"""
    operator = require_user(authorization)
    consumer = body.consumer.strip()
    try:
        date_cls.fromisoformat(body.biz_date)
        wm_dt = datetime_cls.fromisoformat(body.watermark)
    except (TypeError, ValueError):
        raise HTTPException(422, "业务日或水位时间格式不合法")
    if not body.watermark.startswith(body.biz_date + "T"):
        raise HTTPException(409, "补登水位必须锚定业务日（形如 2026-11-02T09:00:00），"
                                 "禁止用其它日期的时间点冒充该业务日水位。")

    # 策略条被改写属于衔接口径偏离：先留痕并提交，再拒绝，不能被回滚吞掉。
    with connect() as c:
        if not handoff.policy_state(c)["ok"]:
            with db.tx(c):
                check_id, check_no = handoff.add_check(
                    c, consumer=consumer, biz_date=body.biz_date,
                    verdict=VERDICT_DEVIATION, state=CLAIM_STATE_DONE, operator=operator,
                    detail="策略条口径与钉死口径不一致，基线补登拒绝。",
                    result={"today_watermark": body.watermark, "missing_dates": []})
            _refuse(409, VERDICT_DEVIATION, "策略条口径已被改写，补登拒绝。",
                    check_no=check_no)

    with connect() as c, db.tx(c):
        taken = c.execute(
            "SELECT * FROM watermarks WHERE consumer=? AND biz_date=?",
            (consumer, body.biz_date)).fetchone()
        if taken:
            raise HTTPException(409, f"{consumer} 在 {body.biz_date} 已有水位，禁止覆盖。")
        prev = handoff.latest_before(c, consumer, body.biz_date)
        nxt = handoff.earliest_after(c, consumer, body.biz_date)
        prev_dt = handoff.parse_dt(prev["watermark"]) if prev else None
        next_dt = handoff.parse_dt(nxt["watermark"]) if nxt else None
        if prev_dt and wm_dt <= prev_dt:
            raise HTTPException(409, "补登水位必须晚于前一现行水位，禁止回拨。")
        if next_dt and wm_dt >= next_dt:
            raise HTTPException(409, "补登水位必须早于后一现行水位，禁止倒挂。")
        check_id, check_no = handoff.add_check(
            c, consumer=consumer, biz_date=body.biz_date, verdict=VERDICT_BASELINE,
            state=CLAIM_STATE_DONE, operator=operator,
            detail=f"基线补登：{consumer} / {body.biz_date} 水位 {body.watermark}",
            result={"prev_biz_date": prev["biz_date"] if prev else "",
                    "prev_watermark": prev["watermark"] if prev else "",
                    "prev_source_slice": prev["source_slice"] if prev else "",
                    "today_watermark": body.watermark, "missing_dates": []})
        wm_id = handoff.insert_baseline_watermark(
            c, consumer=consumer, biz_date=body.biz_date, value=body.watermark,
            prev_watermark=prev["watermark"] if prev else "", check_id=check_id,
            operator=operator)
        c.execute("UPDATE handoff_checks SET watermark_id=? WHERE id=?", (wm_id, check_id))
        c.execute(
            "INSERT INTO handoff_dispositions(check_id,check_no,action,note,operator,created_at)"
            " VALUES (?,?,?,?,?,?)",
            (check_id, check_no, "基线补登", body.note or "补齐历史水位", operator, handoff.now()))
    return {"ok": True, "check_no": check_no, "watermark_id": wm_id,
            "doctrine": _doctrine_payload()}


# ---------------------------------------------------------------------------
# 处置页
# ---------------------------------------------------------------------------

class DispositionIn(BaseModel):
    action: str
    note: str = ""


@app.get("/api/handoff/dispositions")
def list_dispositions(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM handoff_dispositions ORDER BY id DESC LIMIT 300")]
        checks = [dict(r) for r in c.execute(
            "SELECT * FROM handoff_checks WHERE verdict<>? ORDER BY id DESC LIMIT 200",
            (VERDICT_PASS,))]
        for chk in checks:
            chk["dispositions"] = [d for d in rows if d["check_id"] == chk["id"]]
            chk["release"] = dict(c.execute(
                "SELECT id,status,note,created_at FROM releases WHERE id=?",
                (chk["release_id"],)).fetchone() or {}) if chk["release_id"] else {}
            chk["watermark"] = dict(c.execute(
                "SELECT id,watermark,event FROM watermarks WHERE id=?",
                (chk["watermark_id"],)).fetchone() or {}) if chk["watermark_id"] else {}
    return {"items": checks, "dispositions": rows, "actions": handoff.DISPOSITION_ACTIONS,
            "doctrine": _doctrine_payload()}


@app.post("/api/handoff/checks/{check_id}/disposition")
def add_disposition(check_id: int, body: DispositionIn,
                    authorization: str | None = Header(default=None)):
    operator = require_user(authorization)
    if body.action not in handoff.DISPOSITION_ACTIONS:
        raise HTTPException(422, "处置动作不合法")
    with connect() as c, db.tx(c):
        chk = c.execute("SELECT * FROM handoff_checks WHERE id=?", (check_id,)).fetchone()
        if not chk:
            raise HTTPException(404, "衔接检查不存在")
        cur = c.execute(
            "INSERT INTO handoff_dispositions(check_id,check_no,action,note,operator,created_at)"
            " VALUES (?,?,?,?,?,?)",
            (check_id, chk["check_no"], body.action, body.note, operator, handoff.now()))
    return {"ok": True, "id": cur.lastrowid, "check_no": chk["check_no"]}


@app.get("/api/handoff/checks/{check_id}")
def get_check(check_id: int, authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        chk = c.execute("SELECT * FROM handoff_checks WHERE id=?", (check_id,)).fetchone()
        if not chk:
            raise HTTPException(404, "衔接检查不存在")
        item = dict(chk)
        item["release"] = dict(c.execute(
            "SELECT * FROM releases WHERE id=?", (chk["release_id"],)).fetchone() or {}) \
            if chk["release_id"] else {}
        item["watermark"] = dict(c.execute(
            "SELECT * FROM watermarks WHERE id=?", (chk["watermark_id"],)).fetchone() or {}) \
            if chk["watermark_id"] else {}
        item["slice"] = dict(c.execute(
            "SELECT * FROM slices WHERE id=?", (chk["slice_id"],)).fetchone() or {}) \
            if chk["slice_id"] else {}
        item["dispositions"] = [dict(r) for r in c.execute(
            "SELECT * FROM handoff_dispositions WHERE check_id=? ORDER BY id", (check_id,))]
    return item


@app.get("/")
def index():
    return FileResponse(FRONTEND / "index.html")


app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")
