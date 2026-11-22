"""按 README 的完整流程对正在运行的实例做一次冒烟演示。

用法（默认 http://127.0.0.1:8000，可用 --base 或环境变量 APP_BASE 指定）：

    python scripts/smoke_http_demo.py --base http://127.0.0.1:8000

脚本只使用标准库，可直接在容器内或宿主机运行。它假设实例是全新状态
（未导入），会先导入内置演示数据；如需重来，先在总览页点「清空并重置」。
"""
from __future__ import annotations

import argparse
import http.cookiejar
import os
import re
import sys
import urllib.parse
import urllib.request

# 内置演示数据与本仓库 app/demo.py 保持一致（脚本可在没有源码的容器里运行）
DEMO_ROSTER_A = """catalog_id,title
C001,青花缠枝莲纹罐
C002,白釉暗刻龙纹盘
C003,粉彩人物故事图瓶
C004,钧窑玫瑰紫釉花盆
C005,鎏金铜释迦牟尼坐像
C006,三彩马及牵马俑
C007,缂丝海屋添筹图挂屏
C008,紫砂仿古扁壶
C009,玉兽面纹璧
C010,成化款斗彩鸡缸杯
C011,木雕彩绘观音立像
C012,竹雕竹林七贤笔筒"""

DEMO_ROSTER_B = """catalog_id,title
C007,缂丝海屋添筹图挂屏
C008,紫砂仿古扁壶
C009,玉兽面纹璧
C010,成化款斗彩鸡缸杯
C011,木雕彩绘观音立像
C012,竹刻东山报捷图笔筒
C013,鎏金铜龟鹤香炉
C014,翡翠雕白菜摆件
C015,黑漆嵌螺钿人物盒
C016,矾红彩鱼藻纹缸"""

DEMO_DRAWS_A = """中签号
C001
C003
C005
C007
C009
C011
C012"""

DEMO_DRAWS_B = """中签号
C008
C009
C011
C013
C015"""


class Client:
    def __init__(self, base: str):
        self.base = base.rstrip("/")
        jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar)
        )

    def get(self, path: str) -> str:
        return self.opener.open(self.base + path).read().decode()

    def post(self, path: str, data: dict) -> str:
        body = urllib.parse.urlencode(data).encode()
        return self.opener.open(self.base + path, body).read().decode()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--base", default=os.environ.get("APP_BASE", "http://127.0.0.1:8000")
    )
    args = parser.parse_args()
    c = Client(args.base)
    checks = []

    def check(label: str, cond: bool) -> None:
        checks.append((label, bool(cond)))
        print(("PASS " if cond else "FAIL ") + label)

    import_page = c.get("/import")
    check("导入页可访问", "<form" in import_page)
    if "已锁定" not in import_page:
        c.post(
            "/import",
            {
                "roster_a": DEMO_ROSTER_A,
                "roster_b": DEMO_ROSTER_B,
                "draws_a": DEMO_DRAWS_A,
                "draws_b": DEMO_DRAWS_B,
            },
        )
    check("导入并锁定成功", "已锁定" in c.get("/"))

    c.post("/register/A/demo", {})
    c.post("/register/B/demo", {})
    check("待核列出 C011 两组冲突", "两组结论冲突" in c.get("/review"))
    check("冲突时出数被拦下", "暂不能出数" in c.get("/results"))

    c.post("/review/split", {"catalog_id": "C011", "operator": "冒烟"})
    c.post("/review/split", {"catalog_id": "C012", "operator": "冒烟"})
    check("拆分后待核清单为空", "待核清单为空" in c.get("/review"))

    results = c.get("/results")
    token = re.search(r'name="revision" value="(\d+)"', results)
    check("出数页列出甲件/乙件",
          "C011（甲件）" in results and "C011（乙件）" in results)
    check("出数页显示 HT 估计与标准误",
          "Horvitz" in results and "抽样标准误" in results)
    check("出数页带回取数令牌", token is not None)

    snap = c.post("/snapshots", {"revision": token.group(1)})
    check("快照保存成功", "出数时点快照" in snap)
    check("快照注明当时拆开的编号",
          "当时拆开的共有编号" in snap and "C011" in snap)
    check("总览快照列表出现记录", "#1" in c.get("/"))

    c.post("/review/merge", {"catalog_id": "C011"})
    check("并回后冲突重新列入待核", "两组结论冲突" in c.get("/review"))

    stale = c.post("/snapshots", {"revision": token.group(1)})
    check("并回后用旧令牌保存被拒", "发生变化" in stale and "刷新" in stale)
    check("旧快照仍可查看", "当时拆开的共有编号" in c.get("/snapshots/1"))
    check("审计页含拆分/并回记录", "拆分 / 并回审计" in c.get("/audit"))

    failed = [label for label, ok in checks if not ok]
    print(f"\n{len(checks) - len(failed)}/{len(checks)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
