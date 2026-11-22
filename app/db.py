"""SQLite 数据访问层。

表结构：
- meta     系统状态（是否锁定）与名录规模
- items    去重后的藏品并集（同号只算一件）
- draws    两组中签号
- registrations  两组对各自中签藏品的结论登记与更正
- reg_audit       结论变更审计
- snapshots       出数时点快照
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    locked INTEGER NOT NULL DEFAULT 0,
    n_a INTEGER,
    n_b INTEGER,
    created_at TEXT NOT NULL,
    locked_at TEXT
);
CREATE TABLE IF NOT EXISTS items (
    catalog_id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    in_a INTEGER NOT NULL DEFAULT 0,
    in_b INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS draws (
    grp TEXT NOT NULL CHECK (grp IN ('A', 'B')),
    catalog_id TEXT NOT NULL,
    PRIMARY KEY (grp, catalog_id)
);
CREATE TABLE IF NOT EXISTS registrations (
    grp TEXT NOT NULL CHECK (grp IN ('A', 'B')),
    catalog_id TEXT NOT NULL,
    conclusion TEXT CHECK (conclusion IN ('error', 'ok')),
    note TEXT NOT NULL DEFAULT '',
    updated_at TEXT,
    PRIMARY KEY (grp, catalog_id),
    FOREIGN KEY (grp, catalog_id) REFERENCES draws (grp, catalog_id)
);
CREATE TABLE IF NOT EXISTS reg_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    grp TEXT NOT NULL,
    catalog_id TEXT NOT NULL,
    old_conclusion TEXT,
    new_conclusion TEXT,
    old_note TEXT,
    new_note TEXT,
    changed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    taken_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT OR IGNORE INTO meta (id, locked, created_at) VALUES (1, 0, ?)",
        (utcnow(),),
    )
    conn.commit()


@contextmanager
def transaction(conn: sqlite3.Connection):
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def is_locked(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT locked FROM meta").fetchone()[0] == 1


def get_meta(conn: sqlite3.Connection) -> sqlite3.Row:
    return conn.execute("SELECT * FROM meta").fetchone()


def import_dataset(
    conn: sqlite3.Connection,
    items: list[dict],
    draws_a: list[str],
    draws_b: list[str],
) -> None:
    """首次导入：写入藏品并集、中签号，并锁定名录与中签号。"""
    with transaction(conn):
        conn.executemany(
            "INSERT INTO items (catalog_id, title, in_a, in_b) "
            "VALUES (:catalog_id, :title, :in_a, :in_b)",
            items,
        )
        conn.executemany(
            "INSERT INTO draws (grp, catalog_id) VALUES ('A', ?)",
            [(c,) for c in draws_a],
        )
        conn.executemany(
            "INSERT INTO draws (grp, catalog_id) VALUES ('B', ?)",
            [(c,) for c in draws_b],
        )
        # 为每个中签号预建空白登记行，便于统一查询
        conn.executemany(
            "INSERT INTO registrations (grp, catalog_id) VALUES ('A', ?)",
            [(c,) for c in draws_a],
        )
        conn.executemany(
            "INSERT INTO registrations (grp, catalog_id) VALUES ('B', ?)",
            [(c,) for c in draws_b],
        )
        conn.execute(
            "UPDATE meta SET locked = 1, n_a = ?, n_b = ?, locked_at = ?",
            (len(draws_a), len(draws_b), utcnow()),
        )


def list_items(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM items ORDER BY catalog_id"
    ).fetchall()


def get_draw(conn: sqlite3.Connection, grp: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT d.catalog_id, i.title, r.conclusion, r.note, r.updated_at
        FROM draws d
        JOIN items i ON i.catalog_id = d.catalog_id
        LEFT JOIN registrations r
          ON r.grp = d.grp AND r.catalog_id = d.catalog_id
        WHERE d.grp = ?
        ORDER BY d.catalog_id
        """,
        (grp,),
    ).fetchall()


def upsert_registration(
    conn: sqlite3.Connection,
    grp: str,
    catalog_id: str,
    conclusion: str,
    note: str,
) -> None:
    """登记或更正一条结论，并写审计。"""
    old = conn.execute(
        "SELECT conclusion, note FROM registrations "
        "WHERE grp = ? AND catalog_id = ?",
        (grp, catalog_id),
    ).fetchone()
    if old is None:
        raise ValueError("该藏品不在本组中签号内")
    now = utcnow()
    with transaction(conn):
        conn.execute(
            "UPDATE registrations SET conclusion = ?, note = ?, "
            "updated_at = ? WHERE grp = ? AND catalog_id = ?",
            (conclusion, note, now, grp, catalog_id),
        )
        if old["conclusion"] != conclusion or old["note"] != note:
            conn.execute(
                "INSERT INTO reg_audit (grp, catalog_id, old_conclusion, "
                "new_conclusion, old_note, new_note, changed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    grp,
                    catalog_id,
                    old["conclusion"],
                    conclusion,
                    old["note"] or "",
                    note or "",
                    now,
                ),
            )


def get_registrations(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM registrations ORDER BY grp, catalog_id"
    ).fetchall()


def review_rows(conn: sqlite3.Connection) -> list[dict]:
    """按藏品汇总两组结论，标出待核原因。"""
    items = {
        r["catalog_id"]: r
        for r in conn.execute("SELECT * FROM items").fetchall()
    }
    regs: dict[str, dict[str, sqlite3.Row]] = {}
    for r in conn.execute("SELECT * FROM registrations").fetchall():
        regs.setdefault(r["catalog_id"], {})[r["grp"]] = r

    rows = []
    for catalog_id in sorted(regs):
        per = regs[catalog_id]
        reasons = []
        missing_groups = [
            g for g in ("A", "B") if g in per and per[g]["conclusion"] is None
        ]
        if missing_groups:
            reasons.append("未补齐：" + "、".join(group_name(g) for g in missing_groups))
        conclusions = {
            g: per[g]["conclusion"] for g in per if per[g]["conclusion"] is not None
        }
        if len(set(conclusions.values())) > 1:
            reasons.append("两组结论冲突")
        if reasons:
            rows.append(
                {
                    "catalog_id": catalog_id,
                    "title": items[catalog_id]["title"],
                    "groups": per,
                    "reasons": reasons,
                }
            )
    return rows


def current_conclusions(conn: sqlite3.Connection) -> dict[str, int]:
    """同一时点读取：当前全部已登记结论（同号多组结论须一致才可出数）。"""
    rows = conn.execute(
        "SELECT catalog_id, conclusion FROM registrations "
        "WHERE conclusion IS NOT NULL ORDER BY catalog_id"
    ).fetchall()
    y: dict[str, int] = {}
    for r in rows:
        value = 1 if r["conclusion"] == "error" else 0
        if r["catalog_id"] in y and y[r["catalog_id"]] != value:
            raise ValueError("存在冲突结论，不能出数")
        y[r["catalog_id"]] = value
    return y


def pending_count(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM registrations WHERE conclusion IS NULL"
    ).fetchone()[0]


def has_conflict(conn: sqlite3.Connection) -> bool:
    rows = conn.execute(
        "SELECT catalog_id, COUNT(DISTINCT conclusion) AS c "
        "FROM registrations WHERE conclusion IS NOT NULL "
        "GROUP BY catalog_id HAVING c > 1"
    ).fetchall()
    return len(rows) > 0


def save_snapshot(conn: sqlite3.Connection, payload: dict) -> int:
    with transaction(conn):
        cur = conn.execute(
            "INSERT INTO snapshots (taken_at, payload) VALUES (?, ?)",
            (utcnow(), json.dumps(payload, ensure_ascii=False)),
        )
        return cur.lastrowid


def list_snapshots(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, taken_at, payload FROM snapshots ORDER BY id DESC"
    ).fetchall()


def get_audit(conn: sqlite3.Connection, limit: int = 100) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM reg_audit ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()


def reset_all(conn: sqlite3.Connection) -> None:
    """清空全部业务数据（仅用于重新演示）。"""
    with transaction(conn):
        for table in (
            "reg_audit",
            "snapshots",
            "registrations",
            "draws",
            "items",
        ):
            conn.execute(f"DELETE FROM {table}")
        conn.execute(
            "UPDATE meta SET locked = 0, n_a = NULL, n_b = NULL, "
            "locked_at = NULL"
        )


def group_name(grp: str) -> str:
    return {"A": "甲组", "B": "乙组"}.get(grp, grp)
