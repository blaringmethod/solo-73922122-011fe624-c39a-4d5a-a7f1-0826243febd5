"""名录与中签号导入解析、校验。

支持 Web 表单粘贴 CSV 文本（带不带表头均可）：
- 名录：catalog_id[,title]
- 中签号：每行一个编号
校验规则：
- 每组 2 <= 中签数 <= 200，且不超过名录规模
- 编号去重；中签号必须都在对应名录内
"""
from __future__ import annotations

import csv
import io

MAX_SIZE = 200
MIN_DRAW = 2
ID_HEADER_CANDIDATES = {"catalog_id", "id", "编号", "藏号", "藏品编号", "no"}
TITLE_HEADER_CANDIDATES = {"title", "名称", "题名", "藏品名称"}


class ImportError_(ValueError):
    pass


def _read_rows(text: str) -> list[list[str]]:
    text = (text or "").strip()
    if not text:
        return []
    reader = csv.reader(io.StringIO(text))
    return [[cell.strip() for cell in row] for row in reader if any(c.strip() for c in row)]


def parse_roster(text: str, group_label: str) -> dict[str, str]:
    """解析名录，返回 {catalog_id: title}。"""
    rows = _read_rows(text)
    if not rows:
        raise ImportError_(f"{group_label}名录为空")
    start = 0
    first = rows[0]
    if first and first[0].lower() in ID_HEADER_CANDIDATES:
        start = 1
    roster: dict[str, str] = {}
    for lineno, row in enumerate(rows[start:], start=start + 1):
        if not row or not row[0]:
            raise ImportError_(f"{group_label}名录第 {lineno} 行缺少编号")
        catalog_id, title = row[0], row[1] if len(row) > 1 else ""
        if catalog_id in roster:
            raise ImportError_(
                f"{group_label}名录内编号重复：{catalog_id}（第 {lineno} 行）"
            )
        roster[catalog_id] = title
    return roster


def parse_draws(text: str, group_label: str) -> list[str]:
    """解析中签号文本，每行一个编号，返回有序去重列表。"""
    rows = _read_rows(text)
    if not rows:
        raise ImportError_(f"{group_label}中签号为空")
    start = 0
    first = rows[0][0]
    if first.lower() in ID_HEADER_CANDIDATES or first in {"中签号", "中签编号"}:
        start = 1
    draws: list[str] = []
    seen: set[str] = set()
    for lineno, row in enumerate(rows[start:], start=start + 1):
        catalog_id = row[0]
        if not catalog_id:
            raise ImportError_(f"{group_label}中签号第 {lineno} 行为空")
        if catalog_id in seen:
            raise ImportError_(
                f"{group_label}中签号重复：{catalog_id}（第 {lineno} 行）"
            )
        seen.add(catalog_id)
        draws.append(catalog_id)
    return draws


def validate_and_build(
    roster_a: dict[str, str],
    roster_b: dict[str, str],
    draws_a: list[str],
    draws_b: list[str],
) -> tuple[list[dict], list[str], list[str]]:
    """全部校验，返回 (并集藏品行, 甲中签号, 乙中签号)。"""
    if not (MIN_DRAW <= len(draws_a) <= MAX_SIZE):
        raise ImportError_(
            f"甲组中签数须在 {MIN_DRAW}–{MAX_SIZE} 件之间，当前 {len(draws_a)} 件"
        )
    if not (MIN_DRAW <= len(draws_b) <= MAX_SIZE):
        raise ImportError_(
            f"乙组中签数须在 {MIN_DRAW}–{MAX_SIZE} 件之间，当前 {len(draws_b)} 件"
        )
    for catalog_id in draws_a:
        if catalog_id not in roster_a:
            raise ImportError_(f"甲组中签号 {catalog_id} 不在甲名录内")
    for catalog_id in draws_b:
        if catalog_id not in roster_b:
            raise ImportError_(f"乙组中签号 {catalog_id} 不在乙名录内")

    all_ids = sorted(set(roster_a) | set(roster_b))
    items = []
    for catalog_id in all_ids:
        items.append(
            {
                "catalog_id": catalog_id,
                "title": roster_a.get(catalog_id) or roster_b.get(catalog_id, ""),
                "in_a": int(catalog_id in roster_a),
                "in_b": int(catalog_id in roster_b),
            }
        )
    return items, draws_a, draws_b
