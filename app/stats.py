"""Horvitz–Thompson 估计核心。

抽样设计：
- 甲、乙两组各自在自己的名录内做独立的等概率不放回简单随机抽样（SRS）。
- 总体 U 为两份名录去重后的并集；同号藏品只算一件。
- 对藏品 i 记 (a_i, b_i) 表示其是否属于甲/乙名录。

一阶入样概率（事件 S_i = i 至少被一组抽中），两组独立：
    π_i = P(S_i) = 1 - (1 - a_i n_A/N_A)(1 - b_i n_B/N_B)

二阶联合入样概率 π_ij = P(S_i ∩ S_j)，用容斥原理：
    π_ij = 1 - P(S_iᶜ) - P(S_jᶜ) + P(S_iᶜ ∩ S_jᶜ)
其中单件全不中签 P(S_iᶜ) = 1 - π_i；
两件在两组都未中签的概率按组独立分解为 m_A·m_B，单组 g 内
    两件都在名录：m_g = 1 - 2p_g + s_g
    恰一件在名录：m_g = 1 - p_g
    都不在名录：  m_g = 1
p_g = n_g/N_g，s_g = n_g(n_g-1)/(N_g(N_g-1))。
该公式已对全部归属组合做全样本空间精确枚举验证（见 tests/test_stats.py）。

HT 总量估计：            Ŷ = Σ_i y_i/π_i
HT 方差估计（代入式）：  V̂ = Σ_i (1-π_i)/π_i² y_i
        + Σ_i Σ_{j≠i} (π_ij-π_iπ_j)/(π_ij π_i π_j) y_i y_j
该代入式估计量可能取负值；V̂ < 0 时标准误不可用，按需求注明。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

# 浮点误差容差：理论为 0 的微小负值视为 0
NEG_EPS = 1e-9


@dataclass(frozen=True)
class Frame:
    """抽样框描述。"""

    n_a: int
    n_b: int
    N_a: int
    N_b: int
    # catalog_id -> (in_a, in_b)
    membership: Mapping[str, tuple[bool, bool]]


def inclusion_probability(in_a: bool, in_b: bool, frame: Frame) -> float:
    """一阶入样概率 π_i。"""
    p_a = frame.n_a / frame.N_a if (in_a and frame.N_a) else 0.0
    p_b = frame.n_b / frame.N_b if (in_b and frame.N_b) else 0.0
    return p_a + p_b - p_a * p_b


def _both_selected_prob(n: int, N: int) -> float:
    """单组 SRS 中两件名录内藏品同时入样的概率。"""
    if N < 2:
        return 0.0
    return n * (n - 1) / (N * (N - 1))


def _group_miss_prob(
    in_i: bool, in_j: bool, n: int, N: int
) -> float:
    """单组 g 内事件“i、j 至少一件未进入该组样本”的概率。"""
    if not N:
        return 1.0
    p = n / N
    if in_i and in_j:
        return 1.0 - 2.0 * p + _both_selected_prob(n, N)
    if in_i or in_j:
        return 1.0 - p
    return 1.0


def joint_inclusion_probability(
    id_i: str, id_j: str, frame: Frame
) -> float:
    """二阶联合入样概率 π_ij（两件各自至少中签一次）。

    容斥：P(S_i∩S_j) = 1 - P(S_iᶜ) - P(S_jᶜ) + P(S_iᶜ∩S_jᶜ)。
    """
    a_i, b_i = frame.membership[id_i]
    a_j, b_j = frame.membership[id_j]

    miss_i = (1.0 - inclusion_probability(a_i, b_i, frame))
    miss_j = (1.0 - inclusion_probability(a_j, b_j, frame))

    # 两件在两组都未中签：两组独立，逐组求“至少一件未入样”再相乘
    miss_both = _group_miss_prob(a_i, a_j, frame.n_a, frame.N_a) * \
        _group_miss_prob(b_i, b_j, frame.n_b, frame.N_b)

    return 1.0 - miss_i - miss_j + miss_both


def estimate(frame: Frame, y: Mapping[str, int]) -> dict:
    """计算 HT 估计。

    参数 y 仅包含已有结论的中签藏品（1=有误，0=无误）；未中签藏品 y=0，
    对估计量无贡献。返回完整的计算结果与逐件明细。
    """
    items = sorted(frame.membership)
    pi = {
        item_id: inclusion_probability(*frame.membership[item_id], frame)
        for item_id in items
    }

    flagged = [item_id for item_id in items if y.get(item_id) == 1]
    y_hat = sum(1.0 / pi[item_id] for item_id in flagged)

    # 方差：对角项 + 交叉项（交叉项按 i<j 求和后乘 2）
    diagonal = sum((1.0 - pi[i]) / pi[i] ** 2 for i in flagged)
    cross = 0.0
    pair_details = []
    for k, i in enumerate(flagged):
        for j in flagged[k + 1:]:
            pij = joint_inclusion_probability(i, j, frame)
            term = 2.0 * (pij - pi[i] * pi[j]) / (pij * pi[i] * pi[j])
            cross += term
            pair_details.append(
                {
                    "i": i,
                    "j": j,
                    "pi_i": pi[i],
                    "pi_j": pi[j],
                    "pi_ij": pij,
                    "term": term,
                }
            )
    v_hat_raw = diagonal + cross

    if v_hat_raw < -NEG_EPS:
        v_hat = v_hat_raw
        se = None
        se_available = False
        ci = None
    else:
        v_hat = max(v_hat_raw, 0.0)
        se = v_hat ** 0.5
        se_available = True
        ci = (max(0.0, y_hat - 1.96 * se), y_hat + 1.96 * se)

    return {
        "item_ids": items,
        "pi": pi,
        "flagged": flagged,
        "flagged_count": len(flagged),
        "y_hat": y_hat,
        "variance_raw": v_hat_raw,
        "variance": v_hat,
        "diagonal": diagonal,
        "cross": cross,
        "se_available": se_available,
        "se": se,
        "ci": ci,
        "pair_details": pair_details,
    }
