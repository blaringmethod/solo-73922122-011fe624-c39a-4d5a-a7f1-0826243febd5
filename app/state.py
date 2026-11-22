"""藏品单元状态：共有编号的拆分、登记归属与待核判定。

两份名录可能把同一个编号登记给了两件不同的藏品。平时并集表按编号去重，
一个编号就是一个“藏品单元”；馆员在待核页把共有编号拆开后，同一个编号
变成两个独立单元：

- 甲件：只属于甲名录，沿用甲组中签号与甲组登记；
- 乙件：只属于乙名录，沿用乙组中签号与乙组登记。

原始 items / draws / registrations 三张表都不改动，只增加一条拆分标记，
因此两组原名录、中签号与各自的登记始终保留；并回只是删除该标记。

本模块同时集中实现“按单元”的待核判定，避免把拆分后的甲、乙两件误判为
同号结论冲突。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import stats

PIECE_NAMES = {"A": "甲件", "B": "乙件"}
GROUP_NAMES = {"A": "甲组", "B": "乙组"}
# 拆分件内部键的分隔符（只出现在内部键里，不显示给用户）
SEP = "␟"


@dataclass(frozen=True)
class Unit:
    """一个藏品单元（并集里的一件实际藏品）。"""

    key: str                  # 统计与结果明细使用的唯一键
    catalog_id: str           # 原名录编号
    label: str                # 页面显示名（拆开时为“编号（甲件）”）
    title: str
    in_a: bool
    in_b: bool
    piece: str | None = None  # None=未拆；"A"/"B"=拆分件

    @property
    def groups(self) -> tuple[str, ...]:
        return tuple(
            g for g, flag in (("A", self.in_a), ("B", self.in_b)) if flag
        )

    @property
    def check_groups(self) -> tuple[str, ...]:
        """判断待核与取结论所用的分组。

        拆分件只看本件所属那一组；未拆件（含未拆的共有编号）看全部归属组，
        要求各组结论一致。
        """
        if self.piece is not None:
            return (self.piece,)
        return self.groups


@dataclass
class State:
    revision: int
    locked: bool
    n_a: int
    n_b: int
    units: list[Unit]
    splits: dict[str, str]
    draws: dict[str, set[str]]
    regs: dict[tuple[str, str], dict]
    roster_titles: dict[tuple[str, str], str]
    item_titles: dict[str, str]
    shared_ids: list[str]
    locked_at: str | None = None
    piece_by_cid_grp: dict[tuple[str, str], Unit] = field(default_factory=dict)

    # ---- 抽样归属 ----------------------------------------------------
    def sampled(self, unit: Unit, grp: str) -> bool:
        """该单元是否被 grp 组抽中（只有抽中才需要/能够登记）。"""
        return grp in unit.groups and unit.catalog_id in self.draws.get(grp, set())

    def reg(self, grp: str, catalog_id: str) -> dict | None:
        return self.regs.get((grp, catalog_id))


def build_state(conn) -> State:
    """同一已提交状态：在一条只读事务里一次读齐全部数据。

    读取期间其他馆员仍可登记或改判（WAL 允许并发读；他们的提交要等本事务
    结束才对本次读可见），因此本次取数看到的是一个一致的已提交时点。
    """
    started = not conn.in_transaction
    if started:
        conn.execute("BEGIN")
    try:
        st = _read_state(conn)
    finally:
        if started:
            conn.execute("COMMIT")
    return st


def _read_state(conn) -> State:
    from . import db

    meta = db.get_meta(conn)
    revision = db.get_revision(conn)
    items = db.list_items(conn)
    splits = {
        r["catalog_id"]: r["split_at"] for r in db.list_splits(conn)
    }
    draws = {
        "A": {r["catalog_id"] for r in db.get_draw(conn, "A")},
        "B": {r["catalog_id"] for r in db.get_draw(conn, "B")},
    }
    regs = {
        (r["grp"], r["catalog_id"]): dict(r)
        for r in db.get_registrations(conn)
    }
    roster_titles: dict[tuple[str, str], str] = {}
    for r in conn.execute(
        "SELECT grp, catalog_id, title FROM roster_items"
    ).fetchall():
        roster_titles[(r["grp"], r["catalog_id"])] = r["title"]
    item_titles = {r["catalog_id"]: r["title"] for r in items}
    shared_ids = [
        r["catalog_id"] for r in items if r["in_a"] and r["in_b"]
    ]

    units: list[Unit] = []
    for row in items:
        cid = row["catalog_id"]
        in_a, in_b = bool(row["in_a"]), bool(row["in_b"])
        title = row["title"]
        piece_titles = {
            "A": roster_titles.get(("A", cid), title),
            "B": roster_titles.get(("B", cid), title),
        }
        if cid in splits:
            for grp, flag in (("A", in_a), ("B", in_b)):
                if not flag:
                    continue
                units.append(
                    Unit(
                        key=f"{cid}{SEP}{grp}",
                        catalog_id=cid,
                        label=f"{cid}（{PIECE_NAMES[grp]}）",
                        title=piece_titles[grp],
                        in_a=(grp == "A"),
                        in_b=(grp == "B"),
                        piece=grp,
                    )
                )
        else:
            units.append(
                Unit(
                    key=cid,
                    catalog_id=cid,
                    label=cid,
                    title=title,
                    in_a=in_a,
                    in_b=in_b,
                    piece=None,
                )
            )

    st = State(
        revision=revision,
        locked=bool(meta["locked"]),
        n_a=meta["n_a"] or 0,
        n_b=meta["n_b"] or 0,
        units=units,
        splits=splits,
        draws=draws,
        regs=regs,
        roster_titles=roster_titles,
        item_titles=item_titles,
        shared_ids=shared_ids,
        locked_at=meta["locked_at"],
    )
    for u in units:
        if u.piece is not None:
            st.piece_by_cid_grp[(u.catalog_id, u.piece)] = u
    return st


def pending_units(st: State) -> list[tuple[Unit, list[str]]]:
    """按单元判定待核：单元需要核对的组必须全部有结论，且结论一致。

    拆分后的甲件、乙件各自只看本件所属组，因此两件的登记互不牵制；
    未拆的共有编号仍要求两组结论一致。
    """
    rows: list[tuple[Unit, list[str]]] = []
    for unit in st.units:
        pending_groups = [
            g for g in unit.check_groups
            if st.sampled(unit, g)
            and (st.reg(g, unit.catalog_id) or {}).get("conclusion") is None
        ]
        conclusions = [
            st.reg(g, unit.catalog_id)["conclusion"]
            for g in unit.check_groups
            if st.sampled(unit, g)
            and (st.reg(g, unit.catalog_id) or {}).get("conclusion") is not None
        ]
        reasons = []
        if pending_groups:
            reasons.append(
                "未补齐：" + "、".join(GROUP_NAMES[g] for g in pending_groups)
            )
        if len(set(conclusions)) > 1:
            reasons.append("两组结论冲突")
        if reasons:
            rows.append((unit, reasons))
    return rows


def review_rows(st: State) -> list[dict]:
    """待核页展示用的逐单元明细（拆分件按甲件/乙件分别成行）。"""
    rows = []
    for unit, reasons in pending_units(st):
        groups = {}
        for grp in unit.groups:
            if not st.sampled(unit, grp):
                continue
            reg = st.reg(grp, unit.catalog_id) or {}
            groups[grp] = {
                "conclusion": reg.get("conclusion"),
                "note": reg.get("note") or "",
            }
        rows.append(
            {
                "catalog_id": unit.catalog_id,
                "key": unit.key,
                "label": unit.label,
                "title": unit.title,
                "piece": unit.piece,
                "groups": groups,
                "reasons": reasons,
            }
        )
    return rows

def pending_count(st: State) -> int:
    return len(pending_units(st))


def has_conflict(st: State) -> bool:
    return any(
        any(reason == "两组结论冲突" for reason in reasons)
        for _, reasons in pending_units(st)
    )


def build_frame(st: State) -> stats.Frame:
    membership: dict[str, tuple[bool, bool]] = {}
    for unit in st.units:
        if unit.in_a or unit.in_b:
            membership[unit.key] = (unit.in_a, unit.in_b)
    return stats.Frame(
        n_a=st.n_a,
        n_b=st.n_b,
        N_a=sum(1 for u in st.units if u.in_a),
        N_b=sum(1 for u in st.units if u.in_b),
        membership=membership,
    )


def conclusions(st: State) -> dict[str, int]:
    """按单元取结论。

    拆分件只使用与本件所属组对应的登记；未拆的共有编号要求两组结论一致。
    """
    y: dict[str, int] = {}
    for unit in st.units:
        if not (unit.in_a or unit.in_b):
            continue
        values = []
        for g in unit.check_groups:
            reg = st.reg(g, unit.catalog_id)
            if reg and reg["conclusion"] is not None:
                values.append(1 if reg["conclusion"] == "error" else 0)
        if len(set(values)) > 1:
            raise ValueError("存在冲突结论，不能出数")
        if values:
            y[unit.key] = values[0]
    return y


def can_estimate(st: State) -> bool:
    return pending_count(st) == 0 and not has_conflict(st)


def compute(st: State) -> dict:
    """按更新后的名录归属重算全部结果（调用前须无待核）。"""
    frame = build_frame(st)
    y = conclusions(st)
    result = stats.estimate(frame, y)
    unit_meta = {
        u.key: {
            "catalog_id": u.catalog_id,
            "label": u.label,
            "title": u.title,
            "in_a": u.in_a,
            "in_b": u.in_b,
            "piece": u.piece,
        }
        for u in st.units
        if u.in_a or u.in_b
    }
    # 明细里的内部键（拆分件含分隔符）在页面上改用可读名称
    result["flagged_labels"] = [
        unit_meta[k]["label"] for k in result["flagged"]
    ]
    result["pair_labels"] = [
        {
            "i": unit_meta[p["i"]]["label"],
            "j": unit_meta[p["j"]]["label"],
            "pi_i": p["pi_i"],
            "pi_j": p["pi_j"],
            "pi_ij": p["pi_ij"],
            "term": p["term"],
            "note": p.get("note", ""),
        }
        for p in result["pair_details"]
    ]
    for key in result["pi"]:
        if key in unit_meta:
            unit_meta[key]["pi"] = result["pi"][key]
    return {"frame": frame, "y": y, "result": result, "unit_meta": unit_meta}
