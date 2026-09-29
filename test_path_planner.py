"""调光路径规划的正确性冒烟测试（不依赖 pytest）。

3 路场景用动态规划枚举全部合法驱动组合，核对：
  - 返回路径的可行性（步长、参考色 ±容差、0～1000）；
  - 各步功率之和的全局最小性；
  - 字典序 tie-break 与参考色插值（不先取整）的口径。
"""

import sys
from fractions import Fraction

sys.path.insert(0, "/workspace")

from app.path_planner import plan_path  # noqa: E402
from app.solver import Channel  # noqa: E402


def start_num(channels, drives):
    return [
        sum(channels[i].rgb[c] * drives[i] for i in range(len(channels)))
        for c in range(3)
    ]


def validate(sol, channels, start, target, K, delta, tol):
    n = len(channels)
    num0 = start_num(channels, start)
    assert [round(x, 9) for x in sol.start_actual_rgb] == \
        [num0[c] / 255 for c in range(3)] or all(
            abs(sol.start_actual_rgb[c] - num0[c] / 255) < 1e-12 for c in range(3)
        )
    assert len(sol.steps) == K
    total = 0
    prev = start
    for step in sol.steps:
        k = step.index
        for i in range(n):
            assert abs(step.drives[i] - prev[i]) <= delta, (k, i)
        # 整数分子精确核对
        for c in range(3):
            s = sum(channels[i].rgb[c] * step.drives[i] for i in range(n))
            assert s == step.scaled_rgb[c]
            assert abs(step.actual_rgb[c] - s / 255) < 1e-12
            # 参考色（Fraction 精确值，不先取整）
            ref = Fraction((K - k) * num0[c] + k * 255 * target[c], 255 * K)
            assert abs(step.reference_rgb[c] - float(ref)) < 1e-9
            err = Fraction(s, 255) - ref
            assert abs(step.errors[c] - float(err)) < 1e-9
            assert 0 <= s <= 255 * 1000
            assert step.in_range[c] == (abs(err) <= tol)
            assert abs(err) <= tol, (k, c, float(err), tol)
        pnum = sum(channels[i].power_mw * step.drives[i] for i in range(n))
        assert pnum == step.power_num
        total += pnum
        prev = step.drives
    assert total == sol.power_num
    return total


def brute_optimal(channels, start, target, K, delta, tol):
    """动态规划求最小各步功率和；返回 (最小值, 字典序最小展平序列) 或 None。

    可达性用按 z 分层的位集（行步长 257）三维膨胀（Δ 次大整数移位，
    在 C 层完成）；每步可行驱动集合 F_k 用固定两路驱动、第三路分量区间
    求交得到（容差小，F_k 很小）。适用于 3 路小规模核对。
    """
    n = len(channels)
    assert n == 3
    num0 = start_num(channels, start)
    read = [channels[i].rgb for i in range(3)]
    STRIDE = 257
    ROW_MASK = (1 << 256) - 1
    PLANE_MASK = ROW_MASK
    for _ in range(255):
        PLANE_MASK = (PLANE_MASK << STRIDE) | ROW_MASK

    def box_constants(k):
        """每个分量对 s_c=Σ read[i][c]·d_i 的整数允许区间。"""
        boxes = []
        for c in range(3):
            ref_num = (K - k) * num0[c] + k * 255 * target[c]
            margin = 255 * K * tol
            # K·s ∈ [ref_num - margin, ref_num + margin]，且 0 ≤ s ≤ 255000
            lo = (ref_num - margin + K - 1) // K  # ceil((ref_num-margin)/K)
            lo = max(0, lo)
            hi = min(255000, (ref_num + margin) // K)
            boxes.append((lo, hi))
        return boxes

    def feasible_set(k):
        """枚举 (d0,d1)，由三个分量区间求 d2 的允许整数区间。"""
        (l0, h0), (l1, h1), (l2, h2) = box_constants(k)
        out = []
        a2 = [read[2][c] for c in range(3)]
        for d0 in range(256):
            for d1 in range(256):
                lo3, hi3 = 0, 255
                ok = True
                for c, (lc, hc) in enumerate(((l0, h0), (l1, h1), (l2, h2))):
                    base = read[0][c] * d0 + read[1][c] * d1
                    if a2[c] == 0:
                        if not (lc <= base <= hc):
                            ok = False
                            break
                        continue
                    l = lc - base
                    h = hc - base
                    # a2·d2 ∈ [l, h]
                    clo = (l + a2[c] - 1) // a2[c] if l > 0 else l // a2[c]
                    chi = h // a2[c] if h >= 0 else -((-h + a2[c] - 1) // a2[c])
                    lo3 = max(lo3, clo)
                    hi3 = min(hi3, chi)
                    if lo3 > hi3:
                        ok = False
                        break
                if ok and lo3 <= hi3:
                    for d2 in range(max(0, lo3), min(255, hi3) + 1):
                        out.append((d0, d1, d2))
        return out

    def dilate(planes):
        """z -> int bitset；沿三轴各膨胀 delta。"""
        # 先在 z 轴膨胀：每层得到其 z 邻域内所有层的并
        zs = sorted(planes)
        zext = {}
        for z in range(256):
            v = 0
            for zz in range(max(0, z - delta), min(255, z + delta) + 1):
                v |= planes.get(zz, 0)
            if v:
                zext[z] = v
        out = {}
        xmask = PLANE_MASK
        for z, v in zext.items():
            # x 轴膨胀（屏蔽哨兵列，避免跨行串位）
            vx = v
            for _ in range(delta):
                vx = ((vx | (vx << 1) | (vx >> 1)) & xmask)
            # y 轴膨胀（行步长 257，跨行不串位）
            vy = vx
            for _ in range(delta):
                vy |= vy << STRIDE
                vy |= vy >> STRIDE
            out[z] = vy & xmask
        return out

    def bit_of(t):
        d0, d1, d2 = t
        return d2, d1 * STRIDE + d0

    order = sorted(range(n), key=lambda i: channels[i].id)

    def key_of(ds):
        return tuple(ds[i] for i in order)

    # states: tuple(drives) -> (cost, flat_prefix)
    states = {tuple(start): (0, ())}
    for k in range(1, K + 1):
        feasible = feasible_set(k)
        planes = {}
        for t in states:
            z, bit = bit_of(t)
            planes[z] = planes.get(z, 0) | (1 << bit)
        occ = dilate(planes)
        nxt = {}
        items = list(states.items())
        for t in feasible:
            z, bit = bit_of(t)
            if not ((occ.get(z, 0) >> bit) & 1):
                continue
            best = None
            for pt, (pcost, pprefix) in items:
                if all(abs(t[i] - pt[i]) <= delta for i in range(3)):
                    p = sum(channels[j].power_mw * t[j] for j in range(3))
                    cand_cost = pcost + p
                    cand_prefix = pprefix + key_of(list(t))
                    key = (cand_cost, cand_prefix)
                    if best is None or key < best:
                        best = key
            if best is not None:
                old = nxt.get(t)
                if old is None or best < old:
                    nxt[t] = best
        states = nxt
        if not states:
            return None
    best = None
    for t, (cost, prefix) in states.items():
        key = (cost, prefix)
        if best is None or key < best:
            best = key
    return (best[0], best[1], None) if best else None


def check(name, channels, start, target, K, delta, tol, expect):
    status, sol = plan_path(
        channels, tuple(start), target, K, delta, tol, time_limit_seconds=10
    )
    print(f"-- {name}: status={status}", end="")
    if sol:
        print(f" power_num={sol.power_num} drives={[s.drives for s in sol.steps]}")
    else:
        print()
    assert status == expect, (status, expect)
    if expect == "infeasible":
        assert sol is None
        print(f"[OK] {name}: 正确判定无完整路径")
        return
    assert sol is not None and status == "optimal"
    total = validate(sol, channels, start, target, K, delta, tol)
    assert sol.tiebreak_complete, "小规模应跑完字典序 tie-break"

    if len(channels) == 3:
        b = brute_optimal(channels, start, target, K, delta, tol)
        assert b is not None, "穷举认为有解"
        assert total == b[0], (total, b[0])
        # 字典序（先步后 ID）核对
        flat = []
        order = sorted(range(3), key=lambda i: channels[i].id)
        for step in sol.steps:
            flat.extend(step.drives[i] for i in order)
        assert list(flat) == list(b[1]), (flat, b[1])
        print(f"[OK] {name}: 与穷举一致，总功率分子={total}，字典序列={flat}")
    else:
        print(f"[OK] {name}: 可行且最优，总功率分子={total}")


def main():
  small = [
    Channel("A", (900, 100, 50), 3000),
    Channel("B", (100, 900, 200), 2500),
    Channel("C", (300, 400, 800), 4000),
  ]

  # 1) 3 路穷举对照：起点 (0,0,0) → 目标 (300,250,200)，3 步，Δ=128，容差 20
  check("穷举3路-零起点", small, [0, 0, 0], (300, 250, 200), 3, 128, 20, "optimal")

  # 2) 3 路，非零起点、4 步
  check("穷举3路-非零起点4步", small, [60, 40, 20], (500, 400, 300), 4, 128, 18,
        "optimal")

  # 3) 无解：Δ 太小，2 步内无法从 0 到目标附近（目标离零起点过远）
  check("无完整路径-步长不足",
        [Channel("R", (1000, 0, 0), 1000),
         Channel("G", (0, 1000, 0), 1000),
         Channel("B", (0, 0, 1000), 1000)],
        [0, 0, 0], (1000, 1000, 1000), 2, 100, 10, "infeasible")

  # 4) 无解：中间步参考色无法命中（插值点超出可达色域，Δ=255 排除步长因素）
  #    纯 RGB 三路，起点 (255,0,0)=红，2 步到蓝；第 1 步参考色各 500，
  #    容差 1 时 1000·d/255 无法落在 500±1（最近 498.04/501.96）
  check("无完整路径-中点不可达",
        [Channel("R", (1000, 0, 0), 1000),
         Channel("G", (0, 1000, 0), 1000),
         Channel("B", (0, 0, 1000), 1000)],
        [255, 0, 0], (0, 0, 1000), 2, 255, 1, "infeasible")
  # 同上但容差 2 → 可行
  check("中点可达-容差2",
        [Channel("R", (1000, 0, 0), 1000),
         Channel("G", (0, 1000, 0), 1000),
         Channel("B", (0, 0, 1000), 1000)],
        [255, 0, 0], (0, 0, 1000), 2, 255, 2, "optimal")

  # 5) 起点驱动 d=255 → 起点色为通道 A 满档色；目标取该色、容差 0，
  #    每步参考色都等于起点色，最小功率路径即始终保持起点驱动
  start_d = [255, 0, 0]
  tgt = (small[0].rgb[0], small[0].rgb[1], small[0].rgb[2])
  status5, sol5 = plan_path(small, start_d, tgt, 3, 80, 0, 5.0)
  assert status5 == "optimal", status5
  for step in sol5.steps:
      assert step.drives == [255, 0, 0], step.drives
      assert all(abs(e) < 1e-9 for e in step.errors)
  print("[OK] 起点即目标-容差0：最优路径保持起点驱动")

  # 6) 4 路 / 6 路可规划（只核对可行性与最优状态，不穷举）
  sample = [
    Channel("R", (1000, 120, 40), 3200),
    Channel("G", (160, 1000, 90), 2800),
    Channel("B", (60, 80, 1000), 3400),
    Channel("W", (820, 800, 760), 6500),
  ]
  check("4路-示例", sample, [120, 80, 40, 30], (600, 420, 180), 3, 60, 12,
        "optimal")
  six = sample + [Channel("W2", (700, 720, 750), 5000),
                  Channel("Amber", (950, 600, 60), 2600)]
  check("6路-3步", six, [100, 100, 100, 50, 40, 20], (500, 500, 500), 3, 100, 18,
        "optimal")

  # 7) ID 逆序输入时 tie-break 仍按 ID 字符串序
  check("穷举3路-ID逆序", list(reversed(small)), [20, 40, 60],
        (300, 250, 200), 3, 128, 20, "optimal")

  print("\n全部通过 ✔")


if __name__ == "__main__":
    main()
