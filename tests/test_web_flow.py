"""端到端流程测试：导入锁定、登记、待核、出数、快照与更正审计。"""
import tempfile
import unittest

from app import create_app, db, demo
from app.importer import parse_draws, parse_roster, validate_and_build


class WebFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.app = create_app(db_path=self.tmp.name)
        self.client = self.app.test_client()

    def tearDown(self):
        import os
        self.app.app_context().push()
        os.unlink(self.tmp.name)

    def _import_demo(self):
        ra = parse_roster(demo.DEMO_ROSTER_A, "甲")
        rb = parse_roster(demo.DEMO_ROSTER_B, "乙")
        da = parse_draws(demo.DEMO_DRAWS_A, "甲")
        db_ = parse_draws(demo.DEMO_DRAWS_B, "乙")
        items, da, db_ = validate_and_build(ra, rb, da, db_)
        with self.app.app_context():
            conn = db.connect(self.tmp.name)
            db.init_db(conn)
            db.import_dataset(conn, items, da, db_)
            conn.close()

    def test_full_flow(self):
        # 未锁定时访问登记页被拦回
        r = self.client.get("/register/A", follow_redirects=True)
        self.assertIn("请先导入".encode(), r.data)

        self._import_demo()

        # 锁定后不能再次导入
        r = self.client.get("/import", follow_redirects=True)
        self.assertIn("已锁定".encode(), r.data)

        # 填入演示结论（C011 冲突）
        self.client.post("/register/A/demo")
        self.client.post("/register/B/demo")
        r = self.client.get("/review")
        self.assertIn("C011".encode(), r.data)
        self.assertIn("冲突".encode(), r.data)

        # 有冲突时不能出数
        r = self.client.post("/snapshots", follow_redirects=True)
        self.assertIn("不能出数".encode(), r.data)

        # 更正乙组 C011，消除冲突
        with self.app.app_context():
            conn = db.connect(self.tmp.name)
            db.upsert_registration(conn, "B", "C011", "ok", "复核后维持无误")
            conn.close()
        # 待核表已无 C011（拆分/并回区的“共有编号”表仍会列出入选编号）
        r = self.client.get("/review")
        self.assertIn("待核清单为空".encode(), r.data)
        self.assertNotIn("两组结论冲突".encode(), r.data)

        # 出数页展示 HT 结果并可固化快照（用页面上本次取数的修订号令牌）
        r = self.client.get("/results")
        self.assertIn("Horvitz".encode(), r.data)
        import re
        token = int(re.search(rb'name="revision" value="(\d+)"', r.data).group(1))
        r = self.client.post("/snapshots", data={"revision": token},
                             follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn("出数时点快照".encode(), r.data)

        with self.app.app_context():
            conn = db.connect(self.tmp.name)
            snaps = db.list_snapshots(conn)
            self.assertEqual(len(snaps), 1)
            audit = db.get_audit(conn)
            self.assertTrue(
                any(a["catalog_id"] == "C011" for a in audit)
            )
            conn.close()

    def test_pending_blocks_results_numbers(self):
        self._import_demo()
        # 只登记甲组，乙组全部空缺：不出数
        self.client.post("/register/A/demo")
        r = self.client.get("/results")
        self.assertIn("暂不能出数".encode(), r.data)


if __name__ == "__main__":
    unittest.main()
