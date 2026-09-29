"""从起始驱动级到目标色的等时调光路径规划（CP-SAT 精确求解）。

问题
====
给定 n 路通道（3～6，与配方台共用 Channel：满档线性 RGB 与满档功率）、
各路起始驱动级 ``start[i] ∈ [0, 255]``、目标色 ``target ∈ [0,1000]³``、
等时步骤数 ``K ∈ [2, 5]`` 与每步每路驱动变化上限 ``Δ ∈ [1, 255]``、
共用整数容差 ``tol ∈ [0, 20]``。

起点实际色按**现有未裁剪公式**计算（不四舍五入、不裁剪）：

    A0_c = num0_c / 255，  num0_c = Σ_i reading[i][c] · start[i]

第 k 步（k = 1…K）参考色为起点实际色到目标色按 k/K 的线性插值，
**不先取整**。令其分子为整数（分母为 255·K）：

    L[k][c] = (K − k) · num0_c + k · 255 · target_c
    ref_c   = L[k][c] / (255·K)

决策变量：第 k 步各路驱动级 ``d[k][i] ∈ [0, 255]`` 为整数。记

    s[k][c] = Σ_i reading[i][c] · d[k][i]

则第 k 步实际线性分量为 s[k][c]/255。全部约束乘上分母后均为整数线性式：

1. 每路与前一步（首步与起点）相差不超过 Δ：

       |d[k][i] − d[k−1][i]| ≤ Δ，  d[0][i] = start[i]

2. 每步实际 RGB 处于参考色 ±容差（含端点）。实际色与参考色分别为
   ``s[k][c]/255`` 与 ``L[k][c]/(255·K)``，故

       actual − ref = (K · s[k][c] − L[k][c]) / (255·K)

   不等式两边同乘 255·K，写成整数线性约束（无浮点变量）：

       L[k][c] − 255·K·tol ≤ K · s[k][c] ≤ L[k][c] + 255·K·tol

3. 每步实际 RGB 处于 0～1000（不裁剪，只约束）：0 ≤ s[k][c] ≤ 255·1000。

目标：最小化**各步功率之和**的分子（分母 255 为正常数，不影响最优性）：

    minimize  Σ_k Σ_i power_i · d[k][i]

起点功率是固定值、不参与优化。

结论三态：

- ``optimal``：CP-SAT 已证明总功率最小，返回完整 K 步路径；
- ``infeasible``：已证明不存在完整路径，不返回任何局部步骤；
- ``unknown``：搜索未能在时限内证明最优，也不返回步骤。

总功率最优确认后，再以“先按时间步、步内按通道 ID 字符串升序”展平的
驱动级序列做字典序最小化（顺序固定法逐变量求解，保证结果确定；该
tie-break 不影响总功率最优性，即使未能跑完也仍返回功率最优路径）。
"""

from __future__ import annotations

from dataclasses import dataclass

from ortools.sat.python import cp_model

# 状态：已证最优 / 已证无完整路径 / 搜索未完成
PathStatus = str  # "optimal" | "infeasible" | "unknown"


@dataclass(frozen=True)
class PathStep:
    # 1 基步骤号 k
    index: int
    # 与入参 channels 同序的各路驱动级
    drives: list[int]
    # Σ reading * drive，各色整数分子（实际值 = /255）
    scaled_rgb: list[int]
    actual_rgb: list[float]
    # 参考色分子 L[k][c]（参考色 = L / (255·K)）
    ref_num: list[int]
    reference_rgb: list[float]
    # 实际色 − 参考色
    errors: list[float]
    in_range: list[bool]
    # Σ power * drive（该步功率 = /255 mW）
    power_num: int
    power_mw: float


@dataclass(frozen=True)
class PathSolution:
    steps: list[PathStep]
    # 起点实际色（未裁剪）
    start_actual_rgb: list[float]
    # Σ_k Σ_i power_i · d[k][i]（各步功率之和 = /255 mW）
    power_num: int
    power_mw: float
    # 步内字典序 tie-break 使用的 ID 顺序
    sorted_ids: list[str]
    # 字典序 tie-break 是否完整跑完（不影响功率最优性）
    tiebreak_complete: bool


def plan_path(
    channels,
    start_drives: list[int],
    target: tuple[int, int, int],
    steps: int,
    max_delta: int,
    tolerance: int,
    time_limit_seconds: float = 10.0,
) -> tuple[PathStatus, PathSolution | None]:
    """规划最小化各步功率之和的完整调光路径。

    长度/取值合法性由调用方（schema）先行校验。
    返回 ``(status, solution)``：仅 ``optimal`` 时 solution 非空。
    """
    n = len(channels)
    k_total = steps
    tol = tolerance

    # 起点实际色分子（未裁剪，不四舍五入）
    num0 = [
        sum(channels[i].rgb[c] * start_drives[i] for i in range(n))
        for c in range(3)
    ]

    # tie-break 展平顺序：先时间步 k=1..K，步内按 ID 字符串升序
    id_order = sorted(range(n), key=lambda k: channels[k].id)
    flat_vars_order: list[tuple[int, int]] = []
    for k in range(1, k_total + 1):
        for i in id_order:
            flat_vars_order.append((k, i))
    n_flat = len(flat_vars_order)

    def build_model():
        m = cp_model.CpModel()
        # drives[(k, i)]：第 k 步通道 i 的驱动级
        drives: dict[tuple[int, int], cp_model.IntVar] = {}
        for k in range(1, k_total + 1):
            for i in range(n):
                d = m.new_int_var(0, 255, f"d_{k}_{i}")
                drives[(k, i)] = d
                prev = start_drives[i] if k == 1 else drives[(k - 1, i)]
                m.add(d - prev <= max_delta)
                m.add(prev - d <= max_delta)

        scaled: dict[tuple[int, int], cp_model.IntVar] = {}
        for k in range(1, k_total + 1):
            for c in range(3):
                s = m.new_int_var(0, 255 * 1000, f"s_{k}_{c}")
                m.add(
                    s
                    == sum(
                        channels[i].rgb[c] * drives[(k, i)] for i in range(n)
                    )
                )
                # 参考色分子 L = (K-k)·num0 + k·255·target
                ref_num = (k_total - k) * num0[c] + k * 255 * target[c]
                margin = 255 * k_total * tol
                # (L ± margin) ≤ K·s ≤ (L ± margin)
                m.add(k_total * s >= ref_num - margin)
                m.add(k_total * s <= ref_num + margin)
                scaled[(k, c)] = s

        power_num = m.new_int_var(
            0,
            k_total * 255 * sum(ch.power_mw for ch in channels),
            "power_num",
        )
        m.add(
            power_num
            == sum(
                channels[i].power_mw * drives[(k, i)]
                for k in range(1, k_total + 1)
                for i in range(n)
            )
        )
        return m, drives, scaled, power_num

    def new_solver() -> cp_model.CpSolver:
        s = cp_model.CpSolver()
        s.parameters.max_time_in_seconds = time_limit_seconds
        s.parameters.num_search_workers = 8
        return s

    # ---- 阶段 1：最小化各步功率之和 ----
    m1, drives1, scaled1, p1 = build_model()
    solver1 = new_solver()
    m1.minimize(p1)
    st1 = solver1.solve(m1)
    if st1 == cp_model.INFEASIBLE:
        return "infeasible", None
    if st1 != cp_model.OPTIMAL:
        return "unknown", None

    min_power = solver1.value(p1)

    def extract(solver: cp_model.CpSolver, dm, sm, pm):
        steps_out: list[PathStep] = []
        for k in range(1, k_total + 1):
            dvals = [solver.value(dm[(k, i)]) for i in range(n)]
            svals = [solver.value(sm[(k, c)]) for c in range(3)]
            ref_num = [
                (k_total - k) * num0[c] + k * 255 * target[c] for c in range(3)
            ]
            denom = 255.0 * k_total
            in_range = []
            for c in range(3):
                ks = k_total * svals[c]
                ok_box = (
                    ref_num[c] - 255 * k_total * tol <= ks
                    and ks <= ref_num[c] + 255 * k_total * tol
                )
                in_range.append(ok_box and 0 <= svals[c] <= 255 * 1000)
            pnum = sum(channels[i].power_mw * dvals[i] for i in range(n))
            steps_out.append(
                PathStep(
                    index=k,
                    drives=dvals,
                    scaled_rgb=svals,
                    actual_rgb=[v / 255.0 for v in svals],
                    ref_num=ref_num,
                    reference_rgb=[v / denom for v in ref_num],
                    errors=[
                        (k_total * svals[c] - ref_num[c])
                        / (255.0 * k_total)
                        for c in range(3)
                    ],
                    in_range=in_range,
                    power_num=pnum,
                    power_mw=pnum / 255.0,
                )
            )
        pval = solver.value(pm)
        return steps_out, pval

    # ---- 阶段 2：同功率下字典序最小（顺序固定法）----
    # 每次固定展平序列中更早的变量，最小化当前变量；逐变量求解保证
    # 整个 K·n 维序列字典序最小。模型规模小（≤30 个驱动变量），
    # 各次求解通常在毫秒级完成。
    cur_solver: cp_model.CpSolver | None = None
    cur_drives = drives1
    cur_scaled = scaled1
    cur_p = p1
    fixed: list[int] = []
    tiebreak_complete = True
    for pos in range(n_flat):
        k, i = flat_vars_order[pos]
        mm, dm, sm, pm = build_model()
        mm.add(pm <= min_power)
        for prev_pos, val in enumerate(fixed):
            pk, pi = flat_vars_order[prev_pos]
            mm.add(dm[(pk, pi)] == val)
        solver = new_solver()
        mm.minimize(dm[(k, i)])
        st = solver.solve(mm)
        if st != cp_model.OPTIMAL:
            # tie-break 未能完整证明：保留阶段 1 的功率最优解，
            # 不影响“各步功率之和最小”的结论
            tiebreak_complete = False
            break
        fixed.append(solver.value(dm[(k, i)]))
        cur_solver = solver
        cur_drives = dm
        cur_scaled = sm
        cur_p = pm

    base_steps, base_power = extract(cur_solver, cur_drives, cur_scaled, cur_p)

    return "optimal", PathSolution(
        steps=base_steps,
        start_actual_rgb=[v / 255.0 for v in num0],
        power_num=base_power,
        power_mw=base_power / 255.0,
        sorted_ids=[channels[i].id for i in id_order],
        tiebreak_complete=tiebreak_complete,
    )
