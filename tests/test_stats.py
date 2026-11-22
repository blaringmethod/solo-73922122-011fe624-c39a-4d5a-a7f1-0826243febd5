"""Horvitz–Thompson 统计核心测试。

概率公式用全样本空间的精确枚举校验；HT 点估计与方差的无偏性同样用
穷举所有抽签组合的方式精确验证（不依赖随机数）。
"""
import itertools
import unittest
from fractions import Fraction
from statistics import mean

from app import stats


def make_frame(flags, n_a: int, n_b: int) -> stats.Frame:
    membership = {f"C{i:02d}": flag for i, flag in enumerate(flags, start=1)}
    N_a = sum(1 for f in flags if f[0])
    N_b = sum(1 for f in flags if f[1])
    return stats.Frame(n_a, n_b, N_a, N_b, membership)


def exact_probabilities(flags, n_a, n_b):
    """穷举两组全部 SRS 组合，返回精确的 π 与 π_ij（Fraction）。"""
    frame = make_frame(flags, n_a, n_b)
    a_ids = [k for k, v in frame.membership.items() if v[0]]
    b_ids = [k for k, v in frame.membership.items() if v[1]]
    sa_combos = list(itertools.combinations(a_ids, n_a)) if a_ids else [()]
    sb_combos = list(itertools.combinations(b_ids, n_b)) if b_ids else [()]
    samples = list(itertools.product(sa_combos, sb_combos))
    total = len(samples)
    pi = {k: Fraction(0) for k in frame.membership}
    pij = {}
    for sa, sb in samples:
        selected = set(sa) | set(sb)
        for k in selected:
            pi[k] += 1
        for i, j in itertools.combinations(sorted(frame.membership), 2):
            if i in selected and j in selected:
                pij[(i, j)] = pij.get((i, j), Fraction(0)) + 1
    return frame, {k: v / total for k, v in pi.items()}, {
        k: v / total for k, v in pij.items()
    }


class ProbabilityTests(unittest.TestCase):
    def test_single_group_formula(self):
        frame = make_frame([(True, False)] * 5, n_a=2, n_b=0)
        self.assertAlmostEqual(
            stats.inclusion_probability(True, False, frame), 0.4
        )
        self.assertAlmostEqual(
            stats.joint_inclusion_probability("C01", "C02", frame),
            2 * 1 / (5 * 4),
        )

    def test_disjoint_lists_independent(self):
        flags = [(True, False)] * 4 + [(False, True)] * 4
        frame = make_frame(flags, 2, 2)
        pij = stats.joint_inclusion_probability("C01", "C05", frame)
        pi1 = stats.inclusion_probability(True, False, frame)
        pi5 = stats.inclusion_probability(False, True, frame)
        self.assertAlmostEqual(pij, pi1 * pi5)

    def test_exact_enumeration_all_patterns(self):
        # 3 件藏品 × 4 种归属 × 全部可行抽签量，逐一比对精确概率
        patterns = [(True, False), (False, True), (True, True), (False, False)]
        checked = 0
        for flags in itertools.product(patterns, repeat=3):
            used = [f for f in flags if f != (False, False)]
            if len(used) < 2:
                continue
            N_a = sum(1 for f in used if f[0])
            N_b = sum(1 for f in used if f[1])
            for n_a in range(1, max(N_a, 1) + 1):
                for n_b in range(1, max(N_b, 1) + 1):
                    frame, exact_pi, exact_pij = exact_probabilities(
                        used, n_a, n_b
                    )
                    for cid, truth in exact_pi.items():
                        got = stats.inclusion_probability(
                            *frame.membership[cid], frame
                        )
                        self.assertAlmostEqual(got, float(truth), places=12)
                    for (i, j), truth in exact_pij.items():
                        got = stats.joint_inclusion_probability(i, j, frame)
                        self.assertAlmostEqual(got, float(truth), places=12)
                    checked += 1
        self.assertGreater(checked, 100)


class HTUnbiasednessTests(unittest.TestCase):
    def _exact_sampling_distribution(self, frame, truth):
        a_ids = [k for k, v in frame.membership.items() if v[0]]
        b_ids = [k for k, v in frame.membership.items() if v[1]]
        sa_combos = list(itertools.combinations(a_ids, frame.n_a)) if a_ids else [()]
        sb_combos = list(itertools.combinations(b_ids, frame.n_b)) if b_ids else [()]
        hats, vars_, available = [], [], 0
        for sa, sb in itertools.product(sa_combos, sb_combos):
            # 只有进入实际样本（至少被一组抽中）的藏品才有观测 y；
            # 未中签藏品在 HT 求和中不贡献，按 y=0 处理。
            selected = set(sa) | set(sb)
            y = {
                k: (truth.get(k, 0) if k in selected else 0)
                for k in frame.membership
            }
            res = stats.estimate(frame, y)
            hats.append(res["y_hat"])
            if res["se_available"]:
                vars_.append(res["variance"])
                available += 1
        total = sum(truth.values())
        emp_var = sum((h - total) ** 2 for h in hats) / len(hats)
        return mean(hats), (mean(vars_) if vars_ else None), emp_var, available

    def test_two_groups_unbiased(self):
        flags = [
            (True, False), (True, False), (True, True),
            (True, True), (False, True), (False, True),
        ]
        frame = make_frame(flags, n_a=2, n_b=2)
        truth = {f"C{i:02d}": v for i, v in enumerate(
            [1, 0, 1, 0, 1, 0], start=1
        )}
        mean_hat, mean_var, emp_var, avail = \
            self._exact_sampling_distribution(frame, truth)
        self.assertAlmostEqual(mean_hat, 3, places=10)
        self.assertEqual(avail, len(list(itertools.product(
            list(itertools.combinations(
                [k for k, v in frame.membership.items() if v[0]], 2)),
            list(itertools.combinations(
                [k for k, v in frame.membership.items() if v[1]], 2)),
        ))))
        self.assertAlmostEqual(mean_var, emp_var, places=10)

    def test_single_group_matches_srs_textbook(self):
        frame = make_frame([(True, False)] * 5, n_a=2, n_b=0)
        truth = {f"C{i:02d}": v for i, v in enumerate(
            [1, 1, 0, 0, 0], start=1
        )}
        mean_hat, mean_var, emp_var, _ = self._exact_sampling_distribution(
            frame, truth
        )
        self.assertAlmostEqual(mean_hat, 2, places=10)
        # HT 代入式方差估计对真实抽样方差无偏：E[V̂] = Var(Ŷ)
        self.assertAlmostEqual(mean_var, emp_var, places=10)
        self.assertAlmostEqual(emp_var, 2.25, places=10)

    def test_full_census_zero_variance(self):
        flags = [(True, False)] * 3 + [(False, True)] * 3
        frame = make_frame(flags, n_a=3, n_b=3)
        res = stats.estimate(frame, {"C01": 1, "C04": 1})
        self.assertAlmostEqual(res["y_hat"], 2, places=10)
        self.assertAlmostEqual(res["variance"], 0.0, places=10)
        self.assertTrue(res["se_available"])


class NegativeVarianceTests(unittest.TestCase):
    def test_negative_variance_flags_se_unavailable(self):
        # 人工构造强负协调（π_ij 远小于 π_iπ_j）使代入式方差为负
        frame = stats.Frame(
            n_a=2, n_b=2, N_a=4, N_b=4,
            membership={"X": (True, True), "Y": (True, True)},
        )
        orig = stats.joint_inclusion_probability
        stats.joint_inclusion_probability = lambda i, j, f: 0.001
        try:
            res = stats.estimate(frame, {"X": 1, "Y": 1})
        finally:
            stats.joint_inclusion_probability = orig
        self.assertFalse(res["se_available"])
        self.assertIsNone(res["se"])
        self.assertIsNone(res["ci"])
        self.assertLess(res["variance"], 0)


if __name__ == "__main__":
    unittest.main()
