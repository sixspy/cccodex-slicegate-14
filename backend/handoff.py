"""跨日水位衔接闸：全仓唯一实现，放行侧与水位侧共用。

口径只来自 doctrine.py，任何一处不得另写判断。
甲口径：昨日现行水位不可核 -> 今日成功放行不落库、水位推进一并卡死。
水位账本只追加不覆盖，禁止静默吞掉昨日水位，禁止用今日水位覆盖昨日现行。
"""
import json
from datetime import date as date_cls, datetime, timedelta

from doctrine import (
    DOCTRINE_CODE,
    DOCTRINE_LABEL,
    DOCTRINE_REJECTED_RULE,
    DOCTRINE_RULE,
    DOCTRINE_SHA,
    VERDICT_CONTENTION,
    VERDICT_DEVIATION,
    VERDICT_GAP,
    VERDICT_PASS,
    VERDICT_ROLLBACK,
)

RELEASE_SUCCESS = "成功"
RELEASE_REFUSED = "拒绝"
EVENT_ADVANCE = "推进"
EVENT_BASELINE = "基线补登"
BASELINE_SOURCE = "人工基线补登"
DISPOSITION_ACTIONS = ("复核", "基线补登", "确认争用", "备注")


def now():
    # 微秒精度：同日多次推进时水位仍严格可区分。
    return datetime.now().isoformat(timespec="microseconds")


def watermark_for(biz_date):
    """水位锚定业务日：<业务日>T<当前时分秒.微秒>。

    存量水位（如 2026-11-01T18:00:00）就是业务日内的时间点；跨日推进必须
    沿用同一坐标系，才能和前一日水位直接比大小，也不会把 11-02 的水位写成 11-17。
    """
    clock = datetime.now().strftime("%H:%M:%S.%f")
    return f"{biz_date}T{clock}"


def parse_dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _rows(conn, consumer):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM watermarks WHERE consumer=? ORDER BY biz_date, id", (consumer,))]


def chain_gaps(rows, upto=None):
    """返回水位链断档日期。

    起点为该消费方最早水位日；终点取 max(最晚水位日, upto)，
    这样“今日迟迟没接上昨日”的缺口也会被列出，而不是只在已有两天之间找缝。
    """
    dates = sorted({r["biz_date"] for r in rows})
    if not dates:
        return []
    start = date_cls.fromisoformat(dates[0])
    end = date_cls.fromisoformat(dates[-1])
    if upto:
        end = max(end, date_cls.fromisoformat(upto))
    present = set(dates)
    gaps = []
    cursor = start
    while cursor <= end:
        if cursor.isoformat() not in present:
            gaps.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return gaps


def evaluate(conn, consumer, biz_date):
    """核昨日现行水位。只读，不写账本。"""
    try:
        day = date_cls.fromisoformat(biz_date)
    except (TypeError, ValueError):
        return {
            "consumer": consumer, "biz_date": biz_date, "prev_biz_date": "",
            "prev_watermark": "", "prev_source_slice": "", "missing_dates": [],
            "today_watermark": "", "verdict": VERDICT_GAP,
            "detail": "业务日格式不合法，昨日现行水位不可核。",
        }
    yesterday = (day - timedelta(days=1)).isoformat()
    rows = _rows(conn, consumer)
    prior = [r for r in rows if r["biz_date"] < biz_date]
    result = {
        "consumer": consumer,
        "biz_date": biz_date,
        "prev_biz_date": yesterday,
        "prev_watermark": "",
        "prev_source_slice": "",
        "missing_dates": [],
        "today_watermark": "",
        "verdict": VERDICT_GAP,
        "detail": "",
    }
    if not prior:
        result["missing_dates"] = [yesterday]
        result["detail"] = (
            f"昨日现行水位不可核：{consumer} 在 {yesterday} 及之前没有任何水位记录；"
            "今日成功放行不落库，水位推进一并卡死。"
        )
        return result

    dates = sorted({r["biz_date"] for r in prior})
    present = set(dates)
    missing = []
    cursor = date_cls.fromisoformat(dates[0])
    end = date_cls.fromisoformat(yesterday)
    while cursor <= end:
        if cursor.isoformat() not in present:
            missing.append(cursor.isoformat())
        cursor += timedelta(days=1)

    last = max(prior, key=lambda r: (r["biz_date"], r["id"]))
    result["prev_watermark"] = last["watermark"]
    result["prev_source_slice"] = last["source_slice"]
    result["prev_biz_date"] = last["biz_date"]

    if missing:
        result["missing_dates"] = missing
        result["detail"] = (
            "昨日现行水位不可核：水位链断档 " + "、".join(missing) +
            "；今日成功放行不落库，水位推进一并卡死。"
        )
        return result

    if last["biz_date"] != yesterday:
        result["missing_dates"] = [yesterday]
        result["detail"] = (
            f"昨日现行水位不可核：最近水位停在 {last['biz_date']}，缺 {yesterday}；"
            "今日成功放行不落库，水位推进一并卡死。"
        )
        return result

    candidate = watermark_for(biz_date)
    last_dt = parse_dt(last["watermark"])
    candidate_dt = parse_dt(candidate)
    if last_dt and candidate_dt and candidate_dt < last_dt:
        # 同日已有更早时刻的水位（正常单日只会有一条推进，这里兜底回拨）。
        result["verdict"] = VERDICT_ROLLBACK
        result["detail"] = (
            f"水位回拨：拟推进 {candidate} 早于现行 {last['watermark']}，"
            "拒绝推进，今日成功放行不落库。"
        )
        return result

    later = earliest_after(conn, consumer, biz_date)
    if later:
        # 水位锚定业务日，迟到切片的推进值会落回历史：一律按回拨拒绝。
        result["verdict"] = VERDICT_ROLLBACK
        result["detail"] = (
            f"迟到切片禁止把水位写回历史：{consumer} 已有 {later['biz_date']} 水位 "
            f"{later['watermark']}，本次 {biz_date} 推进会倒挂。"
            "今日成功放行不落库，先走复核或基线补登。"
        )
        return result

    result.update({
        "verdict": VERDICT_PASS,
        "today_watermark": candidate,
        "detail": (
            f"昨日现行水位已核：{yesterday} = {last['watermark']}"
            f"（来源 {last['source_slice']}），允许今日放行并推进水位。"
        ),
    })
    return result


def ensure_policy(conn):
    row = conn.execute("SELECT * FROM handoff_policy ORDER BY id DESC LIMIT 1").fetchone()
    if row is None:
        conn.execute(
            """INSERT INTO handoff_policy(code,label,rule,rejected_rule,sha,pinned_on,pinned_at,active)
               VALUES (?,?,?,?,?,?,?,1)""",
            (DOCTRINE_CODE, DOCTRINE_LABEL, DOCTRINE_RULE, DOCTRINE_REJECTED_RULE,
             DOCTRINE_SHA, date_cls.today().isoformat(), now()),
        )
        row = conn.execute("SELECT * FROM handoff_policy ORDER BY id DESC LIMIT 1").fetchone()
    return dict(row)


def policy_state(conn):
    row = conn.execute("SELECT * FROM handoff_policy ORDER BY id DESC LIMIT 1").fetchone()
    if row is None:
        return {
            "code": "", "label": "", "rule": "", "rejected_rule": "",
            "sha": "", "expected_sha": DOCTRINE_SHA, "pinned_on": "",
            "pinned_at": "", "active": 0, "pinned": False, "ok": False,
        }
    item = dict(row)
    ok = (
        item["code"] == DOCTRINE_CODE
        and item["label"] == DOCTRINE_LABEL
        and item["rule"] == DOCTRINE_RULE
        and item["sha"] == DOCTRINE_SHA
        and int(item["active"]) == 1
    )
    item["expected_sha"] = DOCTRINE_SHA
    item["pinned"] = ok
    item["ok"] = ok
    item["active"] = bool(item["active"])
    return item


def next_check_no(conn):
    nxt = conn.execute("SELECT COALESCE(MAX(id),0)+1 n FROM handoff_checks").fetchone()["n"]
    return f"HG-{date_cls.today().strftime('%Y%m%d')}-{int(nxt):05d}"


def add_check(conn, *, consumer, biz_date, verdict, state, operator, detail,
              slice_row=None, release_id=0, watermark_id=0, result=None,
              missing_dates=None):
    result = result or {}
    check_no = next_check_no(conn)
    cur = conn.execute(
        """INSERT INTO handoff_checks(
             check_no,consumer,biz_date,prev_biz_date,prev_watermark,prev_source_slice,
             today_watermark,verdict,state,slice_id,slice_no,release_id,watermark_id,
             detail,operator,doctrine_sha,missing_dates,created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            check_no, consumer, biz_date,
            result.get("prev_biz_date", ""),
            result.get("prev_watermark", ""),
            result.get("prev_source_slice", ""),
            result.get("today_watermark", ""),
            verdict, state,
            slice_row["id"] if slice_row else 0,
            slice_row["slice_no"] if slice_row else "",
            release_id, watermark_id, detail, operator, DOCTRINE_SHA,
            json.dumps(missing_dates or result.get("missing_dates") or [], ensure_ascii=False),
            now(),
        ),
    )
    return int(cur.lastrowid), check_no


def finish_check(conn, check_id, *, verdict, state, release_id=0, watermark_id=0,
                 detail=None, result=None):
    result = result or {}
    sets = ["verdict=?", "state=?", "release_id=?", "watermark_id=?"]
    params = [verdict, state, release_id, watermark_id]
    if detail is not None:
        sets.append("detail=?")
        params.append(detail)
    for key in ("prev_biz_date", "prev_watermark", "prev_source_slice", "today_watermark"):
        if key in result:
            sets.append(f"{key}=?")
            params.append(result[key])
    if "missing_dates" in result:
        sets.append("missing_dates=?")
        params.append(json.dumps(result["missing_dates"], ensure_ascii=False))
    params.append(check_id)
    conn.execute(f"UPDATE handoff_checks SET {', '.join(sets)} WHERE id=?", params)


def insert_release(conn, slice_row, consumer, status, note, operator, check_id=0):
    cur = conn.execute(
        """INSERT INTO releases(slice_id,consumer,status,note,created_at,doctrine_sha,check_id,operator)
           VALUES (?,?,?,?,?,?,?,?)""",
        (slice_row["id"], consumer, status, note, now(), DOCTRINE_SHA, check_id, operator),
    )
    return int(cur.lastrowid)


def insert_advance_watermark(conn, *, consumer, biz_date, value, source_slice,
                             prev_watermark, release_id, check_id, operator):
    cur = conn.execute(
        """INSERT INTO watermarks(consumer,biz_date,watermark,source_slice,prev_watermark,
             event,ref_release_id,ref_check_id,operator,doctrine_sha)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (consumer, biz_date, value, source_slice, prev_watermark,
         EVENT_ADVANCE, release_id, check_id, operator, DOCTRINE_SHA),
    )
    return int(cur.lastrowid)


def insert_baseline_watermark(conn, *, consumer, biz_date, value, prev_watermark,
                              check_id, operator):
    cur = conn.execute(
        """INSERT INTO watermarks(consumer,biz_date,watermark,source_slice,prev_watermark,
             event,ref_release_id,ref_check_id,operator,doctrine_sha)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (consumer, biz_date, value, BASELINE_SOURCE, prev_watermark,
         EVENT_BASELINE, 0, check_id, operator, DOCTRINE_SHA),
    )
    return int(cur.lastrowid)


def latest_before(conn, consumer, biz_date):
    row = conn.execute(
        """SELECT * FROM watermarks WHERE consumer=? AND biz_date<?
           ORDER BY biz_date DESC, id DESC LIMIT 1""",
        (consumer, biz_date),
    ).fetchone()
    return dict(row) if row else None


def earliest_after(conn, consumer, biz_date):
    row = conn.execute(
        """SELECT * FROM watermarks WHERE consumer=? AND biz_date>?
           ORDER BY biz_date ASC, id ASC LIMIT 1""",
        (consumer, biz_date),
    ).fetchone()
    return dict(row) if row else None


def _frontier(conn, consumer, rows):
    """水位链应到达的边界：账本、放行、衔接检查三者中最晚业务日。"""
    dates = [r["biz_date"] for r in rows]
    for sql in (
        "SELECT MAX(s.biz_date) d FROM releases r JOIN slices s ON s.id=r.slice_id "
        "WHERE r.consumer=?",
        "SELECT MAX(biz_date) d FROM handoff_checks WHERE consumer=?",
    ):
        row = conn.execute(sql, (consumer,)).fetchone()
        if row and row["d"]:
            dates.append(row["d"])
    return max(dates) if dates else ""


def watermark_detail(conn):
    """水位详情：每个消费方的现行水位、其前一日现行水位、连续性缺口告警。

    “昨日”以该消费方现行水位所在业务日为锚，而不是系统日历日：
    看板要回答的是“现行水位的前一日是否可核”，否则一切正常时也会显示缺日。
    缺口边界取账本/放行/衔接检查的最晚业务日，避免把没排期的未来日算成缺口。
    """
    consumers = [r["consumer"] for r in conn.execute(
        "SELECT DISTINCT consumer FROM watermarks ORDER BY consumer")]
    today = date_cls.today().isoformat()
    out = []
    for consumer in consumers:
        rows = _rows(conn, consumer)
        gaps = chain_gaps(rows, _frontier(conn, consumer, rows) or None)
        last = rows[-1] if rows else None
        prev = None
        if last:
            anchor = date_cls.fromisoformat(last["biz_date"])
            prev_date = (anchor - timedelta(days=1)).isoformat()
            exact = [r for r in rows if r["biz_date"] == prev_date]
            prev = exact[-1] if exact else None
        else:
            prev_date = ""
        prev_ok = bool(prev and not [g for g in gaps if g <= prev["biz_date"]])
        items = []
        for r in rows:
            rel = conn.execute("SELECT id,status,note FROM releases WHERE id=?",
                               (r["ref_release_id"],)).fetchone()
            chk = conn.execute("SELECT id,check_no,verdict FROM handoff_checks WHERE id=?",
                               (r["ref_check_id"],)).fetchone()
            items.append({
                **r,
                "release_status": rel["status"] if rel else "",
                "check_no": chk["check_no"] if chk else "",
                "check_verdict": chk["verdict"] if chk else "",
            })
        out.append({
            "consumer": consumer,
            "current_biz_date": last["biz_date"] if last else "",
            "current_watermark": last["watermark"] if last else "",
            "chain_start": rows[0]["biz_date"] if rows else "",
            "first_day": bool(rows and last and rows[0]["biz_date"] == last["biz_date"]),
            "yesterday_biz_date": prev_date,
            "yesterday_ok": prev_ok,
            "yesterday_watermark": prev["watermark"] if prev else "",
            "yesterday_source_slice": prev["source_slice"] if prev else "",
            "yesterday_event": prev["event"] if prev else "",
            "continuity": not gaps,
            "gaps": gaps,
            "gap_warning": (
                "" if not gaps else
                "水位链断档 " + "、".join(gaps) + "，不得写成连续无断。"
            ),
            "items": items,
        })
    return {"consumers": out, "calendar_today": date_cls.today().isoformat()}


def audit(conn, db_path):
    policy = policy_state(conn)
    problems = []

    triggers = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger'")}
    for name in ("watermarks_no_update", "watermarks_no_delete",
                 "handoff_policy_no_update", "handoff_policy_no_delete"):
        if name not in triggers:
            problems.append(f"缺少账本防覆盖触发器：{name}")

    if not policy["ok"]:
        problems.append("策略条口径与钉死口径不一致，放行必须拒绝")

    success_rows = conn.execute(
        """SELECT r.*, s.biz_date, s.slice_no FROM releases r
           LEFT JOIN slices s ON s.id=r.slice_id
           WHERE r.status='成功' AND r.doctrine_sha!='' ORDER BY r.id"""
    ).fetchall()
    for r in success_rows:
        chk = conn.execute("SELECT * FROM handoff_checks WHERE id=?", (r["check_id"],)).fetchone()
        if not chk:
            problems.append(f"放行 {r['id']} 成功但无衔接检查")
        elif chk["verdict"] != VERDICT_PASS:
            problems.append(f"放行 {r['id']} 成功但衔接检查 {chk['check_no']} 判定为 {chk['verdict']}")
        elif chk["consumer"] != r["consumer"] or chk["biz_date"] != r["biz_date"]:
            problems.append(f"放行 {r['id']} 与衔接检查 {chk['check_no']} 的下游/业务日不一致")
        wm = conn.execute(
            "SELECT * FROM watermarks WHERE ref_release_id=? AND event=?",
            (r["id"], EVENT_ADVANCE),
        ).fetchone()
        if not wm:
            problems.append(f"放行 {r['id']} 成功但水位账本无对应推进记录")
        else:
            if wm["consumer"] != r["consumer"] or wm["biz_date"] != r["biz_date"]:
                problems.append(f"放行 {r['id']} 与水位 {wm['id']} 下游/业务日错位")
            if wm["doctrine_sha"] != r["doctrine_sha"]:
                problems.append(f"放行 {r['id']} 与水位 {wm['id']} 口径哈希不一致")

    pass_checks = conn.execute(
        "SELECT * FROM handoff_checks WHERE verdict=? AND doctrine_sha=?",
        (VERDICT_PASS, DOCTRINE_SHA),
    ).fetchall()
    for chk in pass_checks:
        wm = conn.execute(
            "SELECT * FROM watermarks WHERE ref_check_id=? AND event=?",
            (chk["id"], EVENT_ADVANCE),
        ).fetchone()
        if not wm:
            problems.append(f"衔接检查 {chk['check_no']} 通过但账本无对应水位推进")
        rel = conn.execute("SELECT * FROM releases WHERE id=?", (chk["release_id"],)).fetchone()
        if not rel or rel["status"] != RELEASE_SUCCESS:
            problems.append(f"衔接检查 {chk['check_no']} 通过但无成功放行")

    blocked = conn.execute(
        "SELECT * FROM handoff_checks WHERE verdict IN (?,?,?)",
        (VERDICT_GAP, VERDICT_CONTENTION, VERDICT_DEVIATION),
    ).fetchall()
    for chk in blocked:
        n = conn.execute("SELECT COUNT(*) c FROM watermarks WHERE ref_check_id=?",
                         (chk["id"],)).fetchone()["c"]
        if n:
            problems.append(f"衔接检查 {chk['check_no']} 判定为 {chk['verdict']} 却写入了水位")

    # 全库跨日衔接校验：每条“推进”水位都必须接得上前一日的现行水位。
    advances = conn.execute(
        "SELECT * FROM watermarks WHERE event=? ORDER BY consumer, biz_date, id",
        (EVENT_ADVANCE,)).fetchall()
    for wm in advances:
        prev_day = (date_cls.fromisoformat(wm["biz_date"]) - timedelta(days=1)).isoformat()
        prior = conn.execute(
            "SELECT * FROM watermarks WHERE consumer=? AND biz_date<=? "
            "ORDER BY biz_date DESC, id DESC LIMIT 1",
            (wm["consumer"], prev_day)).fetchone()
        if not prior or prior["biz_date"] != prev_day:
            problems.append(
                f"水位 {wm['id']}（{wm['consumer']} / {wm['biz_date']}）推进时"
                f"昨日 {prev_day} 现行水位不可核")
            continue
        if prior["watermark"] != wm["prev_watermark"]:
            problems.append(
                f"水位 {wm['id']} 记录的承接值 {wm['prev_watermark']} "
                f"与昨日现行水位 {prior['watermark']} 不一致")
        if parse_dt(wm["watermark"]) and parse_dt(prior["watermark"]) \
                and parse_dt(wm["watermark"]) <= parse_dt(prior["watermark"]):
            problems.append(f"水位 {wm['id']} 未严格晚于昨日现行水位，疑似覆盖或倒挂")

    # 存量放行（衔接闸上线前）按基线对账，不套今日硬闸；但要能核到对应水位。
    stock = conn.execute(
        """SELECT r.*, s.biz_date FROM releases r LEFT JOIN slices s ON s.id=r.slice_id
           WHERE r.status='成功' AND r.doctrine_sha='' ORDER BY r.id"""
    ).fetchall()
    for r in stock:
        linked = conn.execute(
            "SELECT * FROM watermarks WHERE ref_release_id=?", (r["id"],)).fetchall()
        if not linked:
            problems.append(f"存量放行 {r['id']} 无对应水位，跨日衔接不可核")
        elif not any(w["event"] in (EVENT_BASELINE, "存量基线") for w in linked):
            problems.append(f"存量放行 {r['id']} 的水位未标基线，疑似冒充今日推进")

    counts = {
        "slices": conn.execute("SELECT COUNT(*) c FROM slices").fetchone()["c"],
        "releases": conn.execute("SELECT COUNT(*) c FROM releases").fetchone()["c"],
        "watermarks": conn.execute("SELECT COUNT(*) c FROM watermarks").fetchone()["c"],
        "handoffs": conn.execute("SELECT COUNT(*) c FROM handoff_checks").fetchone()["c"],
        "gaps": conn.execute("SELECT COUNT(*) c FROM handoff_checks WHERE verdict=?",
                             (VERDICT_GAP,)).fetchone()["c"],
        "contentions": conn.execute("SELECT COUNT(*) c FROM handoff_checks WHERE verdict=?",
                                    (VERDICT_CONTENTION,)).fetchone()["c"],
        "deviations": conn.execute("SELECT COUNT(*) c FROM handoff_checks WHERE verdict=?",
                                   (VERDICT_DEVIATION,)).fetchone()["c"],
        "baselines": conn.execute("SELECT COUNT(*) c FROM watermarks WHERE event=?",
                                  (EVENT_BASELINE,)).fetchone()["c"],
    }
    return {
        "ok": not problems,
        "problems": problems,
        "policy": policy,
        "counts": counts,
        "db": str(db_path),
        "same_db": True,
        "doctrine_sha": DOCTRINE_SHA,
    }
