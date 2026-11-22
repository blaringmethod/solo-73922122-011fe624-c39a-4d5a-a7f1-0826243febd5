"""同号拆分 / 并回、分组待核、按更新归属重算与快照时点校验的测试。"""
import json
import re
import tempfile
import unittest

from app import create_app, db, demo, state
from app.importer import parse_draws, parse_roster, validate_and_build


class SplitFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.app = create_app(db_path=self.tmp.name)
        self.client = self.app.test_client()
        self._import_demo()

    def tearDown(self):
        self.app.app_context().push()
        import os
        os.unlink(self.tmp.name)

    # ---- helpers -----------------------------------------------------
    def _import_demo(self):
        ra = parse_roster(demo.DEMO_ROSTER_A, "甲")
        rb = parse_roster(demo.DEMO_ROSTER_B, "乙")
        da = parse_draws(demo.DEMO_DRAWS_A, "甲")
        db_ = parse_draws(demo.DEMO_DRAWS_B, "乙")
        items, da, db_ = validate_and_build(ra, rb, da, db_)
        with self.app.app_context():
            conn = db.connect(self.tmp.name)
            db.init_db(conn)
            db.import_dataset(conn, items, da, db_, roster_a=ra, roster_b=rb)
            conn.close()

    def _conn(self):
        return db.connect(self.tmp.name)

    def _fill_demo(self, fix_conflict: bool = True):
        self.client.post("/register/A/demo")
        self.client.post("/register/B/demo")
        if fix_conflict:
            with self.app.app_context():
                conn = self._conn()
                db.upsert_registration(conn, "B", "C011", "ok", "复核后维持无误")
                conn.close()

    def _results_token(self) -> int:
        r = self.client.get("/results")
        m = re.search(rb'name="revision" value="(\d+)"', r.data)
        self.assertIsNotNone(m, "results page should expose revision token")
        return int(m.group(1))

    # ---- splitting ---------------------------------------------------
    def test_split_builds_two_units_and_preserves_sources(self):
        self._fill_demo()
        with self.app.app_context():
            conn = self._conn()
            before = state.build_state(conn)
            db.split_item(conn, "C011", "馆员甲")
            after = state.build_state(conn)

            # 原名录、中签号、两组登记一行不少
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM roster_items").fetchone()[0],
                22,  # 甲 12 + 乙 10
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM draws").fetchone()[0], 12,
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM registrations").fetchone()[0],
                12,
            )
            # 拆分只增加一个标记，不动 items 主键
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM items").fetchone()[0], 16,
            )
            conn.close()

        self.assertEqual(len(before.units), 16)
        self.assertEqual(len(after.units), 17)  # 同号两件
        a_piece = after.piece_by_cid_grp[("C011", "A")]
        b_piece = after.piece_by_cid_grp[("C011", "B")]
        self.assertEqual((a_piece.in_a, a_piece.in_b), (True, False))
        self.assertEqual((b_piece.in_a, b_piece.in_b), (False, True))
        self.assertEqual(a_piece.title, "木雕彩绘观音立像")
        self.assertEqual(b_piece.title, "木雕彩绘观音立像")

    def test_split_checks_each_piece_independently(self):
        # 只登记甲组：拆开前 C011 因乙组待登记而待核；
        # 拆开后甲件已登记、不再待核，只剩乙件待核。
        self.client.post("/register/A/demo")
        with self.app.app_context():
            conn = self._conn()
            db.split_item(conn, "C011")
            st = state.build_state(conn)
            conn.close()
        rows = state.review_rows(st)
        pieces = {r["label"]: r["reasons"] for r in rows}
        self.assertIn("C011（乙件）", pieces)
        self.assertNotIn("C011（甲件）", pieces)
        # 其余未登记的乙组藏品仍然待核
        self.assertIn("C008", pieces)

    def test_split_resolves_cross_group_conflict_without_editing(self):
        # 甲 C011=无误、乙 C011=有误：未拆时冲突；拆开后两件各自一致。
        self.client.post("/register/A/demo")
        self.client.post("/register/B/demo")
        with self.app.app_context():
            conn = self._conn()
            db.split_item(conn, "C011")
            st = state.build_state(conn)
            conn.close()
        self.assertTrue(state.can_estimate(st))
        self.assertFalse(state.has_conflict(st))

    def test_merge_of_conflicting_pieces_returns_to_pending(self):
        self.client.post("/register/A/demo")
        self.client.post("/register/B/demo")
        with self.app.app_context():
            conn = self._conn()
            db.split_item(conn, "C011")
            self.assertFalse(state.has_conflict(state.build_state(conn)))
            db.merge_item(conn, "C011")
            self.assertTrue(state.has_conflict(state.build_state(conn)))
            conn.close()
        r = self.client.get("/review")
        self.assertIn("C011".encode(), r.data)
        self.assertIn("两组结论冲突".encode(), r.data)

    def test_piece_with_only_other_group_sampled_is_not_pending(self):
        # C012 只在甲组中签：拆开后乙件没有需要核对的登记，不应待核；
        # 甲件有结论，因此整件不待核。
        with self.app.app_context():
            conn = self._conn()
            db.split_item(conn, "C012")
            # 甲组尚未登记 → 甲件待核；乙件不因“乙组待登记”被拖累
            st = state.build_state(conn)
            rows = state.review_rows(st)
            labels = {r["label"] for r in rows}
            self.assertIn("C012（甲件）", labels)
            self.assertNotIn("C012（乙件）", labels)
            conn.close()

    def test_split_then_merge_round_trip_is_lossless(self):
        self._fill_demo()
        with self.app.app_context():
            conn = self._conn()
            before = state.compute(state.build_state(conn))["result"]
            db.split_item(conn, "C011")
            db.merge_item(conn, "C011")
            after = state.compute(state.build_state(conn))["result"]
            conn.close()
        self.assertEqual(before["y_hat"], after["y_hat"])
        self.assertEqual(before["variance"], after["variance"])
        self.assertEqual(before["pi"], after["pi"])

    def test_split_estimate_union_grows_by_one(self):
        self._fill_demo()
        with self.app.app_context():
            conn = self._conn()
            before = state.compute(state.build_state(conn))["result"]
            # 让两件都有结论且分别为有误/无误，以便观察 HT 求和
            db.split_item(conn, "C011")
            db.upsert_registration(conn, "B", "C011", "error", "乙件另判有误")
            st = state.build_state(conn)
            self.assertEqual(state.pending_count(st), 0)
            after = state.compute(st)
            conn.close()
        # 甲件 y=0、乙件 y=1（乙组 π=0.5），因此 Ŷ 增加 1/0.5 = 2
        self.assertAlmostEqual(
            after["result"]["y_hat"], before["y_hat"] + 2.0, places=10
        )
        self.assertIn("C011␟B", after["result"]["flagged"])

    def test_split_rejects_unknown_and_unique_ids(self):
        with self.app.app_context():
            conn = self._conn()
            with self.assertRaises(ValueError):
                db.split_item(conn, "C001")  # 只在甲名录
            with self.assertRaises(ValueError):
                db.split_item(conn, "ZZZ")
            with self.assertRaises(ValueError):
                db.merge_item(conn, "C011")  # 尚未拆开
            conn.close()

    # ---- recomputation on updated membership --------------------------
    def test_probabilities_recomputed_after_split(self):
        with self.app.app_context():
            conn = self._conn()
            before = state.compute(state.build_state(conn))
            db.split_item(conn, "C011")
            after = state.compute(state.build_state(conn))
            conn.close()
        # 拆开后甲、乙件仅各属一组，π 各自等于单组抽中概率
        pa = after["result"]["pi"]["C011␟A"]
        pb = after["result"]["pi"]["C011␟B"]
        self.assertAlmostEqual(pa, before["frame"].n_a / before["frame"].N_a)
        self.assertAlmostEqual(pb, before["frame"].n_b / before["frame"].N_b)
        # 未拆前同号只算一件，π 为两组并集概率
        old = before["result"]["pi"]["C011"]
        self.assertAlmostEqual(
            old, 1 - (1 - pa) * (1 - pb)
        )
        # 并集规模 +1，两组名录规模不变
        self.assertEqual(after["frame"].union_size if hasattr(
            after["frame"], "union_size"
        ) else len(after["frame"].membership),
            len(before["frame"].membership) + 1)
        self.assertEqual(after["frame"].N_a, before["frame"].N_a)
        self.assertEqual(after["frame"].N_b, before["frame"].N_b)

    def test_ht_estimate_uses_piece_conclusions(self):
        self._fill_demo()
        with self.app.app_context():
            conn = self._conn()
            before = state.compute(state.build_state(conn))["result"]
            db.split_item(conn, "C011")
            after = state.compute(state.build_state(conn))
            conn.close()
        # 甲 C011=无误、乙 C011=无误（已消除冲突）→ 拆开后两件 y 都是 0
        self.assertEqual(after["y"].get("C011␟A"), 0)
        self.assertEqual(after["y"].get("C011␟B"), 0)
        flagged = set(after["result"]["flagged"])
        self.assertNotIn("C011␟A", flagged)
        self.assertNotIn("C011␟B", flagged)
        # 多出的一件 y=0 不进入 HT 求和，点估计不变
        self.assertAlmostEqual(after["result"]["y_hat"], before["y_hat"])

    # ---- snapshot token ------------------------------------------------
    def test_snapshot_records_split_ids_and_stays_viewable(self):
        self._fill_demo()
        with self.app.app_context():
            conn = self._conn()
            db.split_item(conn, "C011")
            conn.close()
        token = self._results_token()
        r = self.client.post("/snapshots", data={"revision": token},
                             follow_redirects=True)
        self.assertIn("出数时点快照".encode(), r.data)
        self.assertIn("C011".encode(), r.data)
        with self.app.app_context():
            conn = self._conn()
            snaps = db.list_snapshots(conn)
            payload = json.loads(snaps[0]["payload"])
            self.assertEqual(payload["split_ids"], ["C011"])
            self.assertIn("C011␟A", payload["unit_meta"])
            # 之后并回，旧快照仍然可读
            db.merge_item(conn, "C011")
            conn.close()
        r = self.client.get("/snapshots/1")
        self.assertEqual(r.status_code, 200)
        self.assertIn("当时拆开的共有编号".encode(), r.data)

    def test_snapshot_rejected_after_registration_change(self):
        self._fill_demo()
        token = self._results_token()
        with self.app.app_context():
            conn = self._conn()
            # 取数后先改判、再改回原值
            db.upsert_registration(conn, "A", "C001", "ok", "暂时改判")
            db.upsert_registration(conn, "A", "C001", "error", "改回原值")
            conn.close()
        r = self.client.post("/snapshots", data={"revision": token},
                             follow_redirects=True)
        self.assertIn("发生变化".encode(), r.data)
        self.assertIn("刷新".encode(), r.data)
        with self.app.app_context():
            conn = self._conn()
            self.assertEqual(len(db.list_snapshots(conn)), 0)
            conn.close()

    def test_snapshot_rejected_after_split_then_merge(self):
        self._fill_demo()
        token = self._results_token()
        with self.app.app_context():
            conn = self._conn()
            db.split_item(conn, "C011")
            db.merge_item(conn, "C011")  # 状态改回，但修订号已推进
            conn.close()
        r = self.client.post("/snapshots", data={"revision": token},
                             follow_redirects=True)
        self.assertIn("发生变化".encode(), r.data)
        with self.app.app_context():
            conn = self._conn()
            self.assertEqual(len(db.list_snapshots(conn)), 0)
            conn.close()

    def test_snapshot_allows_save_when_state_unchanged(self):
        self._fill_demo()
        token = self._results_token()
        r = self.client.post("/snapshots", data={"revision": token},
                             follow_redirects=True)
        self.assertIn("出数时点快照".encode(), r.data)
        with self.app.app_context():
            conn = self._conn()
            self.assertEqual(len(db.list_snapshots(conn)), 1)
            conn.close()


if __name__ == "__main__":
    unittest.main()
