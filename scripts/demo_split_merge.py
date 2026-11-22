"""命令行演示：导入演示数据 → 登记 → 拆分同号两件 → 出数 → 快照 → 并回。

用途：在没有浏览器手点的环境里，按 README 的演示流程走一遍并打印关键结果。
默认使用临时 SQLite 库和 Flask 测试客户端，不启动网络端口。

用法::

    PYTHONPATH=. python scripts/demo_split_merge.py

若要对着一个已运行的实例（例如 Docker Compose 起的 http://localhost:8000）
重放同样的 HTTP 步骤，可参考 tests/test_web_flow.py 的写法；本脚本刻意只依赖
应用工厂与测试客户端，保证每次运行都可重复。
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app, db, demo, state  # noqa: E402
from app.importer import parse_draws, parse_roster, validate_and_build  # noqa: E402


def main() -> int:
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    app = create_app(db_path=tmp.name)
    client = app.test_client()

    roster_a = parse_roster(demo.DEMO_ROSTER_A, "甲")
    roster_b = parse_roster(demo.DEMO_ROSTER_B, "乙")
    draws_a = parse_draws(demo.DEMO_DRAWS_A, "甲")
    draws_b = parse_draws(demo.DEMO_DRAWS_B, "乙")
    items, draws_a, draws_b = validate_and_build(
        roster_a, roster_b, draws_a, draws_b
    )
    with app.app_context():
        conn = db.connect(tmp.name)
        db.init_db(conn)
        db.import_dataset(
            conn, items, draws_a, draws_b,
            roster_a=roster_a, roster_b=roster_b,
        )
        conn.close()
    print(f"[导入] 并集 {len(items)} 件；甲抽 {len(draws_a)}、乙抽 {len(draws_b)}")

    client.post("/register/A/demo")
    client.post("/register/B/demo")
    review = client.get("/review").get_data(as_text=True)
    print("[登记后] 待核含 C011 冲突：", "两组结论冲突" in review)
    results = client.get("/results").get_data(as_text=True)
    print("[拆分前] 出数被拦下：", "暂不能出数" in results)

    client.post("/review/split", data={"catalog_id": "C011", "operator": "演示"})
    client.post("/review/split", data={"catalog_id": "C012", "operator": "演示"})
    review = client.get("/review").get_data(as_text=True)
    print("[拆分后] 待核清单为空：", "待核清单为空" in review)
    print("[拆分后] 拆分区仍可回看并回入口：", "C011" in review and "并回同号" in review)

    results = client.get("/results").get_data(as_text=True)
    print("[拆分后] 出数页列出甲/乙两件：",
          "C011（甲件）" in results and "C011（乙件）" in results)
    with app.app_context():
        conn = db.connect(tmp.name)
        comp = state.compute(state.build_state(conn))
        conn.close()
    print(
        f"[出数] 并集 {len(comp['frame'].membership)} 件；"
        f"Ŷ={comp['result']['y_hat']:.4f}  "
        f"SE={comp['result']['se']:.4f}"
    )
    print(
        "[出数] π(C011甲件)=%.6f  π(C011乙件)=%.6f"
        % (
            comp["result"]["pi"]["C011␟A"],
            comp["result"]["pi"]["C011␟B"],
        )
    )

    token = re.search(r'name="revision" value="(\d+)"', results).group(1)
    snap = client.post(
        "/snapshots", data={"revision": token}, follow_redirects=True
    ).get_data(as_text=True)
    print("[快照] 保存成功并注明拆开编号：",
          "出数时点快照" in snap and "当时拆开的共有编号" in snap)
    print("[快照] 记录 C011/C012：", "C011" in snap and "C012" in snap)

    client.post("/review/merge", data={"catalog_id": "C011"})
    review = client.get("/review").get_data(as_text=True)
    print("[并回 C011] 冲突重新列入待核：", "两组结论冲突" in review)

    stale = client.post(
        "/snapshots", data={"revision": token}, follow_redirects=True
    ).get_data(as_text=True)
    print("[并回后保存旧令牌] 被拒并提示刷新：",
          "发生变化" in stale and "刷新" in stale)
    with app.app_context():
        conn = db.connect(tmp.name)
        print("[快照列表] 仍为", len(db.list_snapshots(conn)), "条（未新增）")
        payload = json.loads(db.list_snapshots(conn)[0]["payload"])
        print("[快照 #1] split_ids =", payload["split_ids"],
              " revision =", payload["revision"])
        conn.close()

    old = client.get("/snapshots/1").get_data(as_text=True)
    print("[旧快照] 并回后仍可查看并保留拆分说明：",
          "当时拆开的共有编号" in old and "C011" in old)

    os.unlink(tmp.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
