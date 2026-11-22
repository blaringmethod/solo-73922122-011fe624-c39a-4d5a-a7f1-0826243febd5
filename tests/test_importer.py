"""导入解析与校验测试。"""
import unittest

from app import importer


class RosterParseTests(unittest.TestCase):
    def test_with_and_without_header(self):
        with_header = "catalog_id,title\nX1,罐\nX2,盘\n"
        without = "X1,罐\nX2,盘"
        self.assertEqual(
            importer.parse_roster(with_header, "甲"),
            importer.parse_roster(without, "甲"),
        )

    def test_duplicate_id_rejected(self):
        with self.assertRaises(importer.ImportError_):
            importer.parse_roster("X1,a\nX1,b", "甲")

    def test_empty(self):
        with self.assertRaises(importer.ImportError_):
            importer.parse_draws("", "甲")

    def test_draws_dedup_input_rejected(self):
        with self.assertRaises(importer.ImportError_):
            importer.parse_draws("X1\nX1", "甲")


class ValidationTests(unittest.TestCase):
    def _rosters(self):
        a = {f"A{i}": f"甲{i}" for i in range(1, 6)}
        b = {f"B{i}": f"乙{i}" for i in range(1, 4)}
        b["A3"] = "重叠"  # 与甲名录重叠 1 件，乙名录共 4 件
        return a, b

    def test_happy_path_union(self):
        a, b = self._rosters()
        items, da, db_ = importer.validate_and_build(
            a, b, ["A1", "A2"], ["B1", "A3"]
        )
        ids = {it["catalog_id"] for it in items}
        self.assertEqual(len(ids), 8)  # 5 + 4 - 1 重叠
        overlap = next(it for it in items if it["catalog_id"] == "A3")
        self.assertEqual((overlap["in_a"], overlap["in_b"]), (1, 1))
        self.assertEqual(da, ["A1", "A2"])

    def test_draw_size_bounds(self):
        a, b = self._rosters()
        with self.assertRaises(importer.ImportError_):
            importer.validate_and_build(a, b, ["A1"], ["B1", "B2"])
        big = {f"X{i}": "" for i in range(201)}
        with self.assertRaises(importer.ImportError_):
            importer.validate_and_build(
                big, b, list(big)[:201], ["B1", "B2"]
            )

    def test_draw_must_be_in_roster(self):
        a, b = self._rosters()
        with self.assertRaises(importer.ImportError_):
            importer.validate_and_build(a, b, ["A1", "ZZZ"], ["B1", "B2"])


if __name__ == "__main__":
    unittest.main()
