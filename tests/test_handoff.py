import json

import pytest
from fastapi.testclient import TestClient

import app as app_module
import doctrine
import handoff
from db import connect


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    db_path = tmp_path / "t.db"
    monkeypatch.setattr(app_module, "DB_PATH", db_path)
    app_module.init_db()
    yield db_path


@pytest.fixture
def client():
    return TestClient(app_module.app)


def _login(c, username="admin", password="123456"):
    r = c.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _slices(c, h):
    return c.get("/api/slices", headers=h).json()["items"]


def test_pipeline_happy_path_requires_yesterday(client, _db):
    h = _login(client)
    assert client.get("/api/health").json()["product"] == "日切放行"
    assert client.get("/api/health").json()["policy_pinned"] is True
    items = _slices(client, h)
    assert len(items) >= 1

    # 部分完整：拒绝，且不写水位
    bad = client.post("/api/slices/1/release", headers=h, json={"consumer": "ads_order"})
    assert bad.status_code == 409
    assert bad.json()["detail"]["code"] == "未完整"
    with connect(_db) as c:
        assert c.execute("SELECT COUNT(*) n FROM watermarks WHERE source_slice='SL-2026-11-01-east'"
                         ).fetchone()["n"] == 0

    # 完整切片 + 昨日水位可核：放行成功，水位推进
    s = client.post("/api/slices", headers=h,
                    json={"slice_no": "SL-2026-11-02-mid", "biz_date": "2026-11-02",
                          "partition_key": "mid", "row_count": 10, "expected_count": 10}).json()
    ok = client.post(f"/api/slices/{s['id']}/release", headers=h, json={"consumer": "ads_order"})
    assert ok.status_code == 200
    assert ok.json()["prev_biz_date"] == "2026-11-01"
    assert ok.json()["prev_watermark"] == "2026-11-01T18:00:00"

    # 同日第二片：后手即衔接争用，不得写成成功
    s2 = client.post("/api/slices", headers=h,
                     json={"slice_no": "SL-2026-11-02-east2", "biz_date": "2026-11-02",
                           "partition_key": "east", "row_count": 10, "expected_count": 10}).json()
    dup = client.post(f"/api/slices/{s2['id']}/release", headers=h,
                      json={"consumer": "ads_order"})
    assert dup.status_code == 409
    assert dup.json()["detail"]["code"] == doctrine.VERDICT_CONTENTION
    with connect(_db) as c:
        assert c.execute("SELECT COUNT(*) n FROM watermarks WHERE biz_date='2026-11-02'"
                         ).fetchone()["n"] == 1


def test_baseline_backfill_then_release_succeeds(client, _db):
    h = _login(client)
    # 11-02 先补齐基线；之后再放 11-03
    b = client.post("/api/handoff/baseline", headers=h,
                    json={"consumer": "ads_order", "biz_date": "2026-11-02",
                          "watermark": "2026-11-02T12:00:00", "note": "补齐跨日缺口"})
    assert b.status_code == 200

    s = client.post("/api/slices", headers=h,
                    json={"slice_no": "SL-2026-11-03-mid", "biz_date": "2026-11-03",
                          "partition_key": "mid", "row_count": 10, "expected_count": 10})
    assert s.status_code == 200
    sid = s.json()["id"]

    # 新消费方，昨日无水位 -> 拒绝
    denied = client.post(f"/api/slices/{sid}/release", headers=h, json={"consumer": "ads_new"})
    assert denied.status_code == 409
    assert denied.json()["detail"]["code"] == doctrine.VERDICT_GAP

    ok = client.post(f"/api/slices/{sid}/release", headers=h, json={"consumer": "ads_order"})
    assert ok.status_code == 200
    payload = ok.json()
    assert payload["prev_biz_date"] == "2026-11-02"
    assert payload["prev_watermark"] == "2026-11-02T12:00:00"
    assert payload["watermark"] > payload["prev_watermark"]

    detail = client.get("/api/watermarks/detail", headers=h).json()["detail"]
    consumer = next(x for x in detail["consumers"] if x["consumer"] == "ads_order")
    assert consumer["yesterday_ok"] is True
    assert [i["biz_date"] for i in consumer["items"]] == ["2026-11-01", "2026-11-02", "2026-11-03"]


def test_yesterday_gap_cannot_release_and_is_visible(client, _db):
    """昨日水位缺口：今日成功放行必须卡死；水位详情并列展示缺口告警。"""
    h = _login(client)
    s = client.post("/api/slices", headers=h,
                    json={"slice_no": "SL-2026-11-04-mid", "biz_date": "2026-11-04",
                          "partition_key": "mid", "row_count": 10, "expected_count": 10}).json()
    r = client.post(f"/api/slices/{s['id']}/release", headers=h, json={"consumer": "ads_order"})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == doctrine.VERDICT_GAP

    detail = client.get("/api/watermarks/detail", headers=h).json()["detail"]
    consumer = next(x for x in detail["consumers"] if x["consumer"] == "ads_order")
    assert consumer["continuity"] is False
    assert "2026-11-02" in consumer["gaps"] and "2026-11-03" in consumer["gaps"]
    assert "不得写成连续无断" in consumer["gap_warning"]


def test_deviation_rejected_and_recorded(client, _db):
    h = _login(client)
    client.post("/api/handoff/baseline", headers=h,
                json={"consumer": "ads_order", "biz_date": "2026-11-02",
                      "watermark": "2026-11-02T12:00:00"})
    s = client.post("/api/slices", headers=h,
                    json={"slice_no": "SL-2026-11-03-mid", "biz_date": "2026-11-03",
                          "partition_key": "mid", "row_count": 10, "expected_count": 10}).json()

    for extra, needle in [
        ({"policy_code": "乙"}, "口径"),
        ({"force": True}, "强制放行"),
        ({"today_watermark": "2026-11-03T00:00:00"}, "自带今日水位"),
    ]:
        r = client.post(f"/api/slices/{s['id']}/release", headers=h,
                        json={"consumer": "ads_order", **extra})
        assert r.status_code == 409
        d = r.json()["detail"]
        assert d["code"] == doctrine.VERDICT_DEVIATION
        assert needle in d["message"]

    with connect(_db) as c:
        n = c.execute("SELECT COUNT(*) n FROM handoff_checks WHERE verdict=?",
                      (doctrine.VERDICT_DEVIATION,)).fetchone()["n"]
        assert n == 3
        # 偏离不得写成功放行，也不得写水位
        assert c.execute("SELECT COUNT(*) n FROM releases WHERE status='成功' AND slice_id=?",
                         (s["id"],)).fetchone()["n"] == 0
        assert c.execute("SELECT COUNT(*) n FROM watermarks WHERE biz_date='2026-11-03'"
                         ).fetchone()["n"] == 0


def test_watermark_ledger_is_append_only(client, _db):
    import sqlite3
    h = _login(client)
    client.post("/api/handoff/baseline", headers=h,
                json={"consumer": "ads_order", "biz_date": "2026-11-02",
                      "watermark": "2026-11-02T12:00:00"})
    s = client.post("/api/slices", headers=h,
                    json={"slice_no": "SL-2026-11-03-mid", "biz_date": "2026-11-03",
                          "partition_key": "mid", "row_count": 10, "expected_count": 10}).json()
    ok = client.post(f"/api/slices/{s['id']}/release", headers=h, json={"consumer": "ads_order"})
    assert ok.status_code == 200

    with connect(_db) as c:
        for sql in ("UPDATE watermarks SET watermark='x' WHERE id=1",
                    "DELETE FROM watermarks WHERE id=1"):
            with pytest.raises(sqlite3.IntegrityError):
                c.execute(sql)

    # 不得覆盖：11-01 的存量水位仍在，且前置链字段真实
    with connect(_db) as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM watermarks WHERE consumer='ads_order' ORDER BY biz_date")]
    assert [r["biz_date"] for r in rows] == ["2026-11-01", "2026-11-02", "2026-11-03"]
    assert rows[1]["prev_watermark"] == rows[0]["watermark"]
    assert rows[2]["prev_watermark"] == rows[1]["watermark"]
    assert rows[2]["event"] == "推进"


def test_catalog_policy_watermark_slice_all_consistent(client, _db):
    h = _login(client)
    client.post("/api/handoff/baseline", headers=h,
                json={"consumer": "ads_order", "biz_date": "2026-11-02",
                      "watermark": "2026-11-02T12:00:00"})
    s = client.post("/api/slices", headers=h,
                    json={"slice_no": "SL-2026-11-03-mid", "biz_date": "2026-11-03",
                          "partition_key": "mid", "row_count": 10, "expected_count": 10}).json()
    client.post(f"/api/slices/{s['id']}/release", headers=h, json={"consumer": "ads_order"})

    cat = client.get("/api/handoff/catalog", headers=h).json()
    assert cat["same_db"] is True
    assert cat["policy"]["ok"] is True
    assert cat["doctrine"]["sha"] == doctrine.DOCTRINE_SHA
    grp = next(g for g in cat["groups"]
               if g["consumer"] == "ads_order" and g["biz_date"] == "2026-11-03")
    assert grp["consistent"] is True, grp["problems"]
    assert len(grp["slices"]) == 1
    assert len(grp["checks"]) >= 1
    assert [r["status"] for r in grp["releases"]] == ["成功"]
    assert [w["event"] for w in grp["watermarks"]] == ["推进"]

    audit = client.get("/api/handoff/audit", headers=h).json()
    assert audit["ok"] is True, audit["problems"]
    assert audit["policy"]["pinned"] is True


def test_policy_tamper_blocks_release(client, _db):
    """策略条被改口径：放行必须拒绝，且不能自己回退成“只改提示”。"""
    import sqlite3
    h = _login(client)
    with connect(_db) as c:
        triggers = {r["name"] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'")}
        assert "handoff_policy_no_update" in triggers
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("UPDATE handoff_policy SET code='乙' WHERE id=1")

    s = client.post("/api/slices", headers=h,
                    json={"slice_no": "SL-2026-11-03-mid", "biz_date": "2026-11-03",
                          "partition_key": "mid", "row_count": 10, "expected_count": 10}).json()
    r = client.post(f"/api/slices/{s['id']}/release", headers=h, json={"consumer": "ads_order"})
    # 策略条虽被库钉死，口径本身依然生效：先撞“昨日缺口”
    assert r.status_code == 409
    assert client.get("/api/health").json()["policy_pinned"] in (True,)


def test_concurrent_operators_serialize_one_success_one_contention(_db):
    """两名衔接值班同时动手必须串行：只允许一笔成功，后手记衔接争用且不得写成成功。"""
    import threading

    import app as app_module
    from fastapi.testclient import TestClient

    c0 = TestClient(app_module.app)
    h1 = _login(c0)
    h2 = _login(c0, "viewer", "view123")
    # 两个只读账号都会因角色被忽略；这里用两个 token 模拟两名值班
    c0.post("/api/handoff/baseline", headers=h1,
            json={"consumer": "ads_order", "biz_date": "2026-11-02",
                  "watermark": "2026-11-02T12:00:00"})
    slice_a = c0.post("/api/slices", headers=h1,
                      json={"slice_no": "SL-2026-11-03-a", "biz_date": "2026-11-03",
                            "partition_key": "a", "row_count": 10, "expected_count": 10}).json()
    slice_b = c0.post("/api/slices", headers=h1,
                      json={"slice_no": "SL-2026-11-03-b", "biz_date": "2026-11-03",
                            "partition_key": "b", "row_count": 10, "expected_count": 10}).json()

    results = []

    def fire(headers, sid):
        client = TestClient(app_module.app)
        r = client.post(f"/api/slices/{sid}/release", headers=headers,
                        json={"consumer": "ads_order"})
        results.append((r.status_code, r.json()))

    threads = [
        threading.Thread(target=fire, args=(h1, slice_a["id"])),
        threading.Thread(target=fire, args=(h2, slice_b["id"])),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    codes = sorted(status for status, _ in results)
    assert codes == [200, 409], results
    failure = next(body for status, body in results if status == 409)["detail"]
    assert failure["code"] == doctrine.VERDICT_CONTENTION
    assert "后手" in failure["message"] or "争用" in failure["message"]

    with connect(_db) as c:
        advances = c.execute(
            "SELECT COUNT(*) n FROM watermarks WHERE consumer='ads_order'"
            " AND biz_date='2026-11-03'").fetchone()["n"]
        successes = c.execute(
            "SELECT COUNT(*) n FROM releases WHERE consumer='ads_order'"
            " AND status='成功' AND slice_id IN (?,?)",
            (slice_a["id"], slice_b["id"])).fetchone()["n"]
        contention_rows = c.execute(
            "SELECT COUNT(*) n FROM handoff_checks WHERE verdict=?",
            (doctrine.VERDICT_CONTENTION,)).fetchone()["n"]
    assert advances == 1
    assert successes == 1
    assert contention_rows >= 1


def test_late_slice_cannot_rewind_watermark(client, _db):
    """迟到切片：昨日可核但已有更晚水位时，禁止把水位写回历史。"""
    h = _login(client)
    client.post("/api/handoff/baseline", headers=h,
                json={"consumer": "ads_order", "biz_date": "2026-11-02",
                      "watermark": "2026-11-02T12:00:00"})
    s3 = client.post("/api/slices", headers=h,
                     json={"slice_no": "SL-2026-11-03-x", "biz_date": "2026-11-03",
                           "partition_key": "x", "row_count": 5, "expected_count": 5}).json()
    assert client.post(f"/api/slices/{s3['id']}/release", headers=h,
                       json={"consumer": "ads_order"}).status_code == 200

    late = client.post("/api/slices", headers=h,
                       json={"slice_no": "SL-2026-11-02-late", "biz_date": "2026-11-02",
                             "partition_key": "late", "row_count": 5, "expected_count": 5}).json()
    r = client.post(f"/api/slices/{late['id']}/release", headers=h, json={"consumer": "ads_order"})
    # 同业务日已有水位 -> 衔接争用；无论哪种都必须拒绝且不写水位
    assert r.status_code == 409
    assert r.json()["detail"]["code"] in (doctrine.VERDICT_CONTENTION, doctrine.VERDICT_ROLLBACK)
    with connect(_db) as c:
        assert c.execute("SELECT COUNT(*) n FROM watermarks WHERE biz_date='2026-11-02'"
                         ).fetchone()["n"] == 1


def test_gap_is_not_silently_coerced_into_continuity(client, _db):
    """水位页：有缺口时 continuity=false 且并列缺口告警，绝不写成连续无断。"""
    h = _login(client)
    s = client.post("/api/slices", headers=h,
                    json={"slice_no": "SL-2026-11-06-mid", "biz_date": "2026-11-06",
                          "partition_key": "mid", "row_count": 5, "expected_count": 5}).json()
    client.post(f"/api/slices/{s['id']}/release", headers=h, json={"consumer": "ads_order"})
    detail = client.get("/api/watermarks/detail", headers=h).json()["detail"]
    consumer = next(x for x in detail["consumers"] if x["consumer"] == "ads_order")
    assert consumer["continuity"] is False
    assert set(consumer["gaps"]) >= {"2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05"}
    assert "不得写成连续无断" in consumer["gap_warning"]
    assert consumer["yesterday_ok"] is False


def test_disposition_page_can_reconcile(client, _db):
    """处置页可对：每条非通过检查都能挂处置，且能回看放行/水位。"""
    h = _login(client)
    s = client.post("/api/slices", headers=h,
                    json={"slice_no": "SL-2026-11-05-mid", "biz_date": "2026-11-05",
                          "partition_key": "mid", "row_count": 5, "expected_count": 5}).json()
    client.post(f"/api/slices/{s['id']}/release", headers=h, json={"consumer": "ads_order"})
    page = client.get("/api/handoff/dispositions", headers=h).json()
    gap_check = next(i for i in page["items"] if i["verdict"] == doctrine.VERDICT_GAP)
    r = client.post(f"/api/handoff/checks/{gap_check['id']}/disposition", headers=h,
                    json={"action": "复核", "note": "确认昨日缺口，安排基线补登"})
    assert r.status_code == 200
    one = client.get(f"/api/handoff/checks/{gap_check['id']}", headers=h).json()
    assert one["dispositions"][0]["action"] == "复核"
    assert one["release"]["status"] == "拒绝"
    assert one["watermark"] == {}


def test_watermark_is_anchored_to_biz_date(client, _db):
    """水位锚定业务日，不得把 11-02 的推进写成系统当前日期。"""
    h = _login(client)
    client.post("/api/handoff/baseline", headers=h,
                json={"consumer": "ads_order", "biz_date": "2026-11-02",
                      "watermark": "2026-11-02T09:00:00"})
    s = client.post("/api/slices", headers=h,
                    json={"slice_no": "SL-2026-11-03-mid", "biz_date": "2026-11-03",
                          "partition_key": "mid", "row_count": 5, "expected_count": 5}).json()
    ok = client.post(f"/api/slices/{s['id']}/release", headers=h, json={"consumer": "ads_order"})
    assert ok.status_code == 200
    value = ok.json()["watermark"]
    assert value.startswith("2026-11-03T"), value
    assert value > "2026-11-02T09:00:00"

    detail = client.get("/api/watermarks/detail", headers=h).json()["detail"]
    consumer = next(x for x in detail["consumers"] if x["consumer"] == "ads_order")
    assert consumer["yesterday_biz_date"] == "2026-11-02"
    assert consumer["yesterday_ok"] is True
    assert consumer["current_biz_date"] == "2026-11-03"


def test_deviation_on_incomplete_slice_is_still_recorded(client, _db):
    """偏离口径优先于完整度判定：未完整切片上的偏离调用也要留痕，不能被“未完整”吞掉。"""
    h = _login(client)
    # 切片 1 是部分完整（980/1000）
    r = client.post("/api/slices/1/release", headers=h,
                    json={"consumer": "ads_order", "policy_code": "乙"})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == doctrine.VERDICT_DEVIATION
    with connect(_db) as c:
        assert c.execute("SELECT COUNT(*) n FROM handoff_checks WHERE verdict=? AND slice_id=1",
                         (doctrine.VERDICT_DEVIATION,)).fetchone()["n"] == 1
        assert c.execute("SELECT COUNT(*) n FROM watermarks WHERE source_slice='SL-2026-11-01-east'"
                         ).fetchone()["n"] == 0


def test_baseline_must_be_anchored_to_biz_date(client, _db):
    h = _login(client)
    bad = client.post("/api/handoff/baseline", headers=h,
                      json={"consumer": "ads_order", "biz_date": "2026-11-02",
                            "watermark": "2026-11-17T09:00:00"})
    assert bad.status_code == 409
    with connect(_db) as c:
        assert c.execute("SELECT COUNT(*) n FROM watermarks WHERE biz_date='2026-11-02'"
                         ).fetchone()["n"] == 0
