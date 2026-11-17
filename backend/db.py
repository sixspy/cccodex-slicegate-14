import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path("data/app.db")
SESSIONS = {}


class _Conn(sqlite3.Connection):
    """with 语句结束时关闭连接；提交/回滚一律走 tx()，不靠隐式行为。"""

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None and self.in_transaction:
                self.execute("ROLLBACK")
        finally:
            self.close()
        return False


def connect(path=None):
    target = Path(path) if path is not None else DB_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, timeout=20, isolation_level=None, factory=_Conn)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=20000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def tx(conn):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def columns(conn, table):
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  username TEXT PRIMARY KEY, password_sha TEXT NOT NULL, role TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS slices(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  slice_no TEXT UNIQUE NOT NULL,
  biz_date TEXT NOT NULL,
  partition_key TEXT NOT NULL,
  row_count INTEGER NOT NULL,
  expected_count INTEGER NOT NULL,
  completeness TEXT NOT NULL,
  release_status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS releases(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  slice_id INTEGER NOT NULL,
  consumer TEXT NOT NULL,
  status TEXT NOT NULL,
  note TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS watermarks(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  consumer TEXT NOT NULL,
  biz_date TEXT NOT NULL,
  watermark TEXT NOT NULL,
  source_slice TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS handoff_policy(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT NOT NULL,
  label TEXT NOT NULL,
  rule TEXT NOT NULL,
  rejected_rule TEXT NOT NULL,
  sha TEXT NOT NULL,
  pinned_on TEXT NOT NULL,
  pinned_at TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS handoff_checks(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  check_no TEXT UNIQUE NOT NULL,
  consumer TEXT NOT NULL,
  biz_date TEXT NOT NULL,
  prev_biz_date TEXT NOT NULL DEFAULT '',
  prev_watermark TEXT NOT NULL DEFAULT '',
  prev_source_slice TEXT NOT NULL DEFAULT '',
  today_watermark TEXT NOT NULL DEFAULT '',
  verdict TEXT NOT NULL,
  state TEXT NOT NULL,
  slice_id INTEGER NOT NULL DEFAULT 0,
  slice_no TEXT NOT NULL DEFAULT '',
  release_id INTEGER NOT NULL DEFAULT 0,
  watermark_id INTEGER NOT NULL DEFAULT 0,
  detail TEXT NOT NULL DEFAULT '',
  operator TEXT NOT NULL DEFAULT '',
  doctrine_sha TEXT NOT NULL DEFAULT '',
  missing_dates TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS handoff_dispositions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  check_id INTEGER NOT NULL,
  check_no TEXT NOT NULL,
  action TEXT NOT NULL,
  note TEXT NOT NULL,
  operator TEXT NOT NULL,
  created_at TEXT NOT NULL
);
"""


def migrate(conn):
    """水位账本加固：只追加、带链、带口径哈希；旧行仅做一次基线补登。"""
    # 迁移期间先摘掉只追加触发器，迁完立刻重建，保证可重复执行。
    for name in ("watermarks_no_update", "watermarks_no_delete",
                 "handoff_policy_no_update", "handoff_policy_no_delete"):
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")
    wm_cols = columns(conn, "watermarks")
    added = []
    for name, ddl in [
        ("prev_watermark", "TEXT NOT NULL DEFAULT ''"),
        ("event", "TEXT NOT NULL DEFAULT '推进'"),
        ("ref_release_id", "INTEGER NOT NULL DEFAULT 0"),
        ("ref_check_id", "INTEGER NOT NULL DEFAULT 0"),
        ("operator", "TEXT NOT NULL DEFAULT ''"),
        ("doctrine_sha", "TEXT NOT NULL DEFAULT ''"),
    ]:
        if name not in wm_cols:
            conn.execute(f"ALTER TABLE watermarks ADD COLUMN {name} {ddl}")
            added.append(name)
    rel_cols = columns(conn, "releases")
    for name, ddl in [
        ("doctrine_sha", "TEXT NOT NULL DEFAULT ''"),
        ("check_id", "INTEGER NOT NULL DEFAULT 0"),
        ("operator", "TEXT NOT NULL DEFAULT ''"),
    ]:
        if name not in rel_cols:
            conn.execute(f"ALTER TABLE releases ADD COLUMN {name} {ddl}")
    if "event" in added:
        # 存量水位不是本次放行写入的，明确标成基线，不能冒充今日推进。
        conn.execute("UPDATE watermarks SET event='存量基线' WHERE event='推进'")
    if "ref_release_id" in added:
        rows = conn.execute("SELECT * FROM watermarks WHERE ref_release_id=0").fetchall()
        for wm in rows:
            hit = conn.execute(
                """SELECT r.id FROM releases r JOIN slices s ON s.id=r.slice_id
                   WHERE r.consumer=? AND s.slice_no=? AND s.biz_date=? AND r.status='成功'
                   ORDER BY r.id LIMIT 1""",
                (wm["consumer"], wm["source_slice"], wm["biz_date"]),
            ).fetchone()
            if hit:
                conn.execute(
                    "UPDATE watermarks SET ref_release_id=?, event='存量基线' WHERE id=?",
                    (hit["id"], wm["id"]))
    conn.executescript(
        """
        CREATE TRIGGER IF NOT EXISTS watermarks_no_update
        BEFORE UPDATE ON watermarks
        BEGIN SELECT RAISE(ABORT, '水位账本只追加不覆盖'); END;
        CREATE TRIGGER IF NOT EXISTS watermarks_no_delete
        BEFORE DELETE ON watermarks
        BEGIN SELECT RAISE(ABORT, '水位账本只追加不覆盖'); END;
        CREATE TRIGGER IF NOT EXISTS handoff_policy_no_update
        BEFORE UPDATE ON handoff_policy
        BEGIN SELECT RAISE(ABORT, '衔接口径当天钉死，不得改写'); END;
        CREATE TRIGGER IF NOT EXISTS handoff_policy_no_delete
        BEFORE DELETE ON handoff_policy
        BEGIN SELECT RAISE(ABORT, '衔接口径当天钉死，不得删除'); END;
        """
    )
