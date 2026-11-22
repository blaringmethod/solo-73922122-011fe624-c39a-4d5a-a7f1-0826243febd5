"""SQLite 数据访问层。

表结构：
- meta     系统状态（是否锁定）、名录规模与状态修订号
- roster_items    两份名录的原始行（保留各组自己的编号与题名）
- items    去重后的藏品并集（同号只算一件）
- draws    两组中签号
- item_splits     共有编号已拆为甲、乙两件的标记
- split_audit     拆分 / 并回操作审计
- registrations  两组对各自中签藏品的结论登记与更正
- reg_audit       结论变更审计
- snapshots       出数时点快照

拆分不修改 items/draws/registrations，只写 item_splits；因此两组原名录、
中签号与各自的登记始终原样保留，并回只需删除该标记。

meta.revision 是单调递增的“状态修订号”：拆分、并回、以及任何结论变化都会
+1，即使随后改回原值。出数页把它作为本次取数的令牌，保存快照时以此判断
数据是否已变，避免把不同时点的结果与登记混在一起。
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
    locked_at TEXT,
    revision INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS roster_items (
    grp TEXT NOT NULL CHECK (grp IN ('A', 'B')),
    catalog_id TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (grp, catalog_id)
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
CREATE TABLE IF NOT EXISTS item_splits (
    catalog_id TEXT PRIMARY KEY,
    split_at TEXT NOT NULL,
    split_by TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS split_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL CHECK (action IN ('split', 'merge')),
    catalog_id TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    changed_at TEXT NOT NULL
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
    # 兼容旧库：早期 meta 没有 revision 列
    cols = {
        r["name"] for r in conn.execute("PRAGMA table_info(meta)").fetchall()
    }
    if "revision" not in cols:
        conn.execute(
            "ALTER TABLE meta ADD COLUMN revision INTEGER NOT NULL DEFAULT 0"
        )
    # 兼容旧库：早期版本没有保存两份原名录，用当时并集里保留的题名补齐，
    # 保证拆分/并回页面能回看两组各自的登记
    if conn.execute("SELECT COUNT(*) FROM roster_items").fetchone()[0] == 0:
        for row in conn.execute("SELECT * FROM items").fetchall():
            for grp, flag in (("A", row["in_a"]), ("B", row["in_b"])):
                if flag:
                    conn.execute(
                        "INSERT OR IGNORE INTO roster_items "
                        "(grp, catalog_id, title) VALUES (?, ?, ?)",
                        (grp, row["catalog_id"], row["title"]),
                    )
    conn.commit()


def get_revision(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT revision FROM meta").fetchone()[0]


def bump_revision(conn: sqlite3.Connection) -> int:
    """状态修订号 +1（须在调用方的事务内执行）。"""
    conn.execute("UPDATE meta SET revision = revision + 1")
    return get_revision(conn)


@contextmanager
def transaction(conn: sqlite3.Connection, immediate: bool = False):
    try:
        if immediate:
            conn.execute("BEGIN IMMEDIATE")
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
    roster_a: dict[str, str] | None = None,
    roster_b: dict[str, str] | None = None,
) -> None:
    """首次导入：写入两份原名录、藏品并集、中签号，并锁定名录与中签号。"""
    with transaction(conn):
        if roster_a is not None:
            conn.executemany(
                "INSERT INTO roster_items (grp, catalog_id, title) "
                "VALUES ('A', ?, ?)",
                [(c, t) for c, t in roster_a.items()],
            )
        if roster_b is not None:
            conn.executemany(
                "INSERT INTO roster_items (grp, catalog_id, title) "
                "VALUES ('B', ?, ?)",
                [(c, t) for c, t in roster_b.items()],
            )
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
            "UPDATE meta SET locked = 1, n_a = ?, n_b = ?, locked_at = ?, "
            "revision = revision + 1",
            (len(draws_a), len(draws_b), utcnow()),
        )
        # 未显式传入原名录时（如测试或内置演示），用并集题名补齐，
        # 保证两份原名录始终可回看
        for row in items:
            for grp, flag in (("A", row["in_a"]), ("B", row["in_b"])):
                if flag and not roster_has(conn, grp, row["catalog_id"]):
                    conn.execute(
                        "INSERT OR IGNORE INTO roster_items "
                        "(grp, catalog_id, title) VALUES (?, ?, ?)",
                        (grp, row["catalog_id"], row["title"]),
                    )


def roster_has(conn: sqlite3.Connection, grp: str, catalog_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM roster_items WHERE grp = ? AND catalog_id = ?",
        (grp, catalog_id),
    ).fetchone() is not None


def list_items(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM items ORDER BY catalog_id"
    ).fetchall()


def get_draw(conn: sqlite3.Connection, grp: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT d.catalog_id, COALESCE(ri.title, i.title) AS title,
               r.conclusion, r.note, r.updated_at
        FROM draws d
        JOIN items i ON i.catalog_id = d.catalog_id
        LEFT JOIN roster_items ri
          ON ri.grp = d.grp AND ri.catalog_id = d.catalog_id
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
            # 仅在结论或备注确有变化时推进修订号；改回原值同样会再推进一次
            bump_revision(conn)


def get_registrations(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM registrations ORDER BY grp, catalog_id"
    ).fetchall()


def list_splits(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT catalog_id, split_at, split_by FROM item_splits "
        "ORDER BY catalog_id"
    ).fetchall()


def split_item(
    conn: sqlite3.Connection, catalog_id: str, operator: str = ""
) -> None:
    """把共有编号拆为甲、乙两个藏品单元；原名录/中签/登记均不改动。"""
    row = conn.execute(
        "SELECT * FROM items WHERE catalog_id = ?", (catalog_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"未知编号 {catalog_id}")
    if not (row["in_a"] and row["in_b"]):
        raise ValueError(f"{catalog_id} 不是两份名录共有的编号，无需拆分")
    now = utcnow()
    with transaction(conn):
        if conn.execute(
            "SELECT 1 FROM item_splits WHERE catalog_id = ?", (catalog_id,)
        ).fetchone():
            raise ValueError(f"{catalog_id} 已经拆开")
        conn.execute(
            "INSERT INTO item_splits (catalog_id, split_at, split_by) "
            "VALUES (?, ?, ?)",
            (catalog_id, now, operator),
        )
        conn.execute(
            "INSERT INTO split_audit (action, catalog_id, detail, changed_at) "
            "VALUES ('split', ?, ?, ?)",
            (
                catalog_id,
                "拆为甲件（甲组名录/中签/登记）与乙件（乙组名录/中签/登记）"
                + (f"；经办人：{operator}" if operator else ""),
                now,
            ),
        )
        bump_revision(conn)


def merge_item(conn: sqlite3.Connection, catalog_id: str) -> None:
    """并回甲、乙两件：删除拆分标记，回到同号只算一件。"""
    now = utcnow()
    with transaction(conn):
        if not conn.execute(
            "SELECT 1 FROM item_splits WHERE catalog_id = ?", (catalog_id,)
        ).fetchone():
            raise ValueError(f"{catalog_id} 未拆开，无须并回")
        conn.execute(
            "DELETE FROM item_splits WHERE catalog_id = ?", (catalog_id,)
        )
        conn.execute(
            "INSERT INTO split_audit (action, catalog_id, detail, changed_at) "
            "VALUES ('merge', ?, ?, ?)",
            (catalog_id, "并回为同一编号；两组原名录、中签与登记保留", now),
        )
        bump_revision(conn)


def get_split_audit(conn: sqlite3.Connection, limit: int = 100) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM split_audit ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()


class SnapshotStale(ValueError):
    """本次取数之后状态已变，拒绝保存快照。"""


def save_snapshot(
    conn: sqlite3.Connection, payload: dict, expected_revision: int
) -> int:
    """在同一事务里核对修订号后写入快照。

    若拆分状态或任一登记自本次取数后发生过变化（包括改回原值），修订号
    必然不同，此时不插入任何记录并抛出 SnapshotStale。
    """
    with transaction(conn, immediate=True):
        if get_revision(conn) != expected_revision:
            raise SnapshotStale(
                "拆分状态或登记结论已在本次取数后发生变化，"
                "请刷新出数页后重新取数。"
            )
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
            "split_audit",
            "item_splits",
            "reg_audit",
            "snapshots",
            "registrations",
            "draws",
            "items",
            "roster_items",
        ):
            conn.execute(f"DELETE FROM {table}")
        conn.execute(
            "UPDATE meta SET locked = 0, n_a = NULL, n_b = NULL, "
            "locked_at = NULL, revision = revision + 1"
        )


def group_name(grp: str) -> str:
    return {"A": "甲组", "B": "乙组"}.get(grp, grp)
