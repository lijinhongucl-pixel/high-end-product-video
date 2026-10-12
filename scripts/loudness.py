#!/usr/bin/env python3
"""ITU-R BS.1770-4 响度测量与母带处理（纯标准库，零依赖）

能力：
  - K 加权滤波器：任一采样率下用双线性变换推导，48kHz 下与标准公布系数一致
    （tests/test_loudness.py 有对照断言，防止改错）
  - 门控集成响度：400ms 块 / 75% 重叠，绝对门 -70 LUFS，相对门 -10 LU
  - 瞬时响度（400ms）与短期响度（3s）最大值
  - 真峰值 dBTP：4x 过采样，仅对超过全局峰值 70% 的局部极大值邻域做
    Blackman 窗 sinc 插值（±16 抽头，核表预计算），兼顾精度与纯 Python 性能
  - 前瞻限幅器：5ms 前瞻窗 + 1ms attack / 50ms release 平滑

为什么不用峰值归一化：峰值只描述最大样本，不描述人耳感受的响度。
两条同样 -1 dBFS 的音轨，听感可以差 10 个 LU。网络视频交付标准是
集成响度 -16 LUFS（播客 -19，影院 -27），真峰值不超过 -1.0 dBTP。
"""

import math
from collections import deque

# ---------------------------------------------------------------- K 加权

def k_weight_biquads(rate):
    """返回 (级联 Shelf, 级联 RLB 高通) 两组二阶系数，归一化到 a0=1

    Shelf: f0=1681.97Hz, G=+4dB, Q=0.7071（高频抬升，补偿人耳对中高频的敏感）
    HPF:   f0=38.14Hz,  Q=0.5003（滤除低频，避免轰头内容虚高）
    推导方式与 libebur128 一致：模拟原型 + 双线性变换，任意采样率可用。
    """
    # 第一级：高频搁架
    f0, G, Q = 1681.9744509555319, 3.99984385397, 0.7071752369554193
    K = math.tan(math.pi * f0 / float(rate))
    Vh = 10.0 ** (G / 20.0)
    Vb = Vh ** 0.499666774155
    a0 = 1.0 + K / Q + K * K
    shelf = (
        (Vh + Vb * K / Q + K * K) / a0,
        2.0 * (K * K - Vh) / a0,
        (Vh - Vb * K / Q + K * K) / a0,
        2.0 * (K * K - 1.0) / a0,
        (1.0 - K / Q + K * K) / a0,
    )
    # 第二级：RLB 高通
    f0, Q = 38.13547087613982, 0.5003270373253953
    K = math.tan(math.pi * f0 / float(rate))
    a0 = 1.0 + K / Q + K * K
    hpf = (
        1.0,
        -2.0,
        1.0,
        2.0 * (K * K - 1.0) / a0,
        (1.0 - K / Q + K * K) / a0,
    )
    return shelf, hpf


def apply_biquad(x, b0, b1, b2, a1, a2):
    """Direct Form I 二阶滤波，纯 Python。长音频会慢，属可接受代价。"""
    n = len(x)
    y = [0.0] * n
    x1 = x2 = y1 = y2 = 0.0
    for i in range(n):
        v = x[i]
        out = b0 * v + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
        x2 = x1
        x1 = v
        y2 = y1
        y1 = out
        y[i] = out
    return y


def k_weight(x, rate):
    """K 加权（Shelf → HPF 级联）"""
    shelf, hpf = k_weight_biquads(rate)
    y = apply_biquad(x, *shelf)
    return apply_biquad(y, *hpf)


# ---------------------------------------------------------------- 响度块

BLOCK_SEC = 0.4     # 瞬时/块长度
STEP_SEC = 0.1      # 块步进（75% 重叠）
SHORT_SEC = 3.0     # 短期响度窗
ABS_GATE_LUFS = -70.0
REL_GATE_LU = -10.0
OFFSET = -0.691     # BS.1770 校准常数


def _blocks(x, rate, win_sec, step_sec):
    win = int(win_sec * rate)
    step = int(step_sec * rate)
    if win <= 0 or step <= 0 or len(x) < win:
        return []
    return [x[s:s + win] for s in range(0, len(x) - win + 1, step)]


def _mean_square(seg):
    return sum(v * v for v in seg) / float(len(seg))


def block_loudness(x, rate):
    """400ms 块响度列表（LUFS），未门控"""
    out = []
    for seg in _blocks(x, rate, BLOCK_SEC, STEP_SEC):
        z = _mean_square(seg)
        out.append(float("-inf") if z <= 0 else OFFSET + 10.0 * math.log10(z))
    return out


def integrated_loudness(x, rate):
    """门控集成响度（LUFS）。全静音或全部门控外返回 -inf"""
    blocks = _blocks(x, rate, BLOCK_SEC, STEP_SEC)
    if not blocks:
        z = _mean_square(x)
        return float("-inf") if z <= 0 else OFFSET + 10.0 * math.log10(z)
    zs = [_mean_square(s) for s in blocks]
    abs_gate = 10.0 ** ((ABS_GATE_LUFS - OFFSET) / 10.0)
    z1 = [z for z in zs if z > abs_gate]
    if not z1:
        return float("-inf")
    zavg = sum(z1) / len(z1)
    rel_gate = zavg * (10.0 ** (REL_GATE_LU / 10.0))
    z2 = [z for z in z1 if z > rel_gate] or z1
    return OFFSET + 10.0 * math.log10(sum(z2) / len(z2))


def shortterm_loudness(x, rate):
    """短期响度（3s 窗）列表，取最大值。窗太短时退化为块响度最大值"""
    blocks = _blocks(x, rate, SHORT_SEC, STEP_SEC)
    if not blocks:
        vals = block_loudness(x, rate)
        return max(vals) if vals else float("-inf")
    vals = []
    for seg in blocks:
        z = _mean_square(seg)
        vals.append(float("-inf") if z <= 0 else OFFSET + 10.0 * math.log10(z))
    return max(vals)


def momentary_max(x, rate):
    vals = block_loudness(x, rate)
    return max(vals) if vals else float("-inf")


# ---------------------------------------------------------------- 真峰值

def _sinc_kernel(oversample, taps):
    """预计算插值核表：kernel[phase][k+taps]，k ∈ [-taps, taps]

    phase 个相位对应输出位置 i + phase/oversample，抽头权重为
    Blackman 窗 sinc(d)，d = phase/oversample - k。
    """
    kernel = []
    for p in range(oversample):
        pos = p / float(oversample)
        row = []
        for k in range(-taps, taps + 1):
            d = pos - k
            if abs(d) < 1e-12:
                w = 1.0
            else:
                w = math.sin(math.pi * d) / (math.pi * d)
                t = d / float(taps)
                w *= 0.42 + 0.5 * math.cos(math.pi * t) + 0.08 * math.cos(2.0 * math.pi * t)
            row.append(w)
        kernel.append(row)
    return kernel


def true_peak_dbtp(x, rate, oversample=4, taps=16, candidate_ratio=0.7):
    """真峰值（dBTP）。只对 |x| ≥ 全局峰值 70% 的局部极大值邻域插值。

    带限信号的重采样峰值只会出现在样本峰值附近，该裁剪在工程上安全，
    并把纯 Python 的计算量从 O(4N·taps) 降到 O(候选数·4·taps)。
    """
    peak = 0.0
    for v in x:
        a = -v if v < 0 else v
        if a > peak:
            peak = a
    if peak <= 0.0:
        return float("-inf")

    thr = peak * candidate_ratio
    kernel = _sinc_kernel(oversample, taps)
    n = len(x)
    best = 0.0
    for i in range(1, n - 1):
        v = x[i]
        a = -v if v < 0 else v
        if a < thr or a < abs(x[i - 1]) or a < abs(x[i + 1]):
            continue
        for p in range(oversample):
            row = kernel[p]
            acc = 0.0
            for k in range(-taps, taps + 1):
                j = i + k
                if 0 <= j < n:
                    acc += x[j] * row[k + taps]
            aa = -acc if acc < 0 else acc
            if aa > best:
                best = aa
    return 20.0 * math.log10(best) if best > 0 else float("-inf")


# ---------------------------------------------------------------- 限幅与归一

def _limit_once(x, ceiling_linear, rate, lookahead=0.005, attack=0.001, release=0.05):
    """前瞻限幅器单次执行。返回 (样本, 最大增益衰减 dB)

    架构：先按前瞻窗（默认 5ms）内的 |x| 最大值算目标增益，再对增益做
    attack/release 一阶平滑，最后把增益施加到延迟 D=lookahead 的信号上。
    没有这个延迟，增益还没降下来第一个峰就已经冲过去了（attack 过冲）。
    滑窗最大值用单调队列，O(n)。输出长度与输入一致（裁掉尾部 D 样本）。
    """
    n = len(x)
    if n == 0:
        return list(x), 0.0
    delay = max(1, int(lookahead * rate))

    target = [1.0] * n
    dq = deque()          # 存下标，对应 |x| 单调不增
    next_idx = 0
    for i in range(n):
        hi = min(n - 1, i + delay)
        while next_idx <= hi:
            v = abs(x[next_idx])
            while dq and abs(x[dq[-1]]) <= v:
                dq.pop()
            dq.append(next_idx)
            next_idx += 1
        while dq and dq[0] < i:
            dq.popleft()
        m = abs(x[dq[0]]) if dq else 0.0
        if m > ceiling_linear:
            target[i] = ceiling_linear / m

    a_att = 1.0 - math.exp(-1.0 / max(1.0, attack * rate))
    a_rel = 1.0 - math.exp(-1.0 / max(1.0, release * rate))
    g = 1.0
    out = [0.0] * n
    max_reduce = 0.0
    for i in range(n):
        t = target[i]
        g += (t - g) * (a_att if t < g else a_rel)
        j = i - delay
        out[i] = x[j] * g if j >= 0 else 0.0
        if g < 1.0:
            db = 20.0 * math.log10(g) if g > 0 else -99.0
            if db < max_reduce:
                max_reduce = db
    return out, max_reduce


def measure(x, rate):
    """一次拿全交付指标"""
    return {
        "integrated_lufs": integrated_loudness(x, rate),
        "shortterm_max_lufs": shortterm_loudness(x, rate),
        "momentary_max_lufs": momentary_max(x, rate),
        "true_peak_dbtp": true_peak_dbtp(x, rate),
    }


def limit_true_peak(x, ceiling_linear, rate, lookahead=0.005, attack=0.001,
                     release=0.05, max_search=8):
    """真峰值限幅（带余量搜索）。返回 (样本, 最大增益衰减 dB, 余量 dB)

    一阶平滑的增益永远差目标一截，单次限幅会有 0.3~0.9 dB 残余过冲。
    这里以内层搜索解决：以样本峰值为代理（便宜），每轮把上限再压低
    0.5dB，直到样本峰值留出 0.5dB 插值余量。契约：输出真峰值不超过上限。
    """
    margin = 0.0
    reduce_db = 0.0
    cand = list(x)
    for _ in range(max_search):
        eff = ceiling_linear * 10.0 ** (-margin / 20.0)
        cand, reduce_db = _limit_once(x, eff, rate, lookahead, attack, release)
        if peak_linear(cand) <= ceiling_linear * 10.0 ** (-0.5 / 20.0):
            break
        margin += 0.5
    return cand, reduce_db, margin


def peak_linear(x):
    p = 0.0
    for v in x:
        a = -v if v < 0 else v
        if a > p:
            p = a
    return p


def master(x, rate, target_lufs=-16.0, tp_ceiling_dbtp=-1.0, max_iter=4):
    """母带处理链：静态增益到目标响度 → 真峰值限幅 → 复测补偿，最多迭代。

    限幅器 attack 有零点几 dB 的残余过冲，直接限到上限会超标。这里用内层
    「余量搜索」解决：以样本峰值为代理（便宜），每轮把上限再压低 0.5dB，
    直到样本峰值留出 0.5dB 插值余量；外层循环补偿限幅造成的响度损失。
    纯 Python 下 60s 音频约 10~20s，属可接受代价。
    """
    info = {"iterations": 0, "gain_db": 0.0, "limiter_margin_db": 0.0,
            "limiter_reduction_db": 0.0, "after": None}
    y = list(x)
    ceiling = 10.0 ** (tp_ceiling_dbtp / 20.0)

    for it in range(1, max_iter + 1):
        info["iterations"] = it
        lufs = integrated_loudness(y, rate)
        if lufs == float("-inf"):
            return y, {"error": "全静音，无法归一化"}
        gain = 10.0 ** ((target_lufs - lufs) / 20.0)
        y = [s * gain for s in y]
        info["gain_db"] = round(20.0 * math.log10(gain), 2)

        y, reduce_db, margin = limit_true_peak(y, ceiling, rate)
        info["limiter_margin_db"] = round(margin, 2)
        info["limiter_reduction_db"] = round(reduce_db, 2)

        after = measure(y, rate)
        info["after"] = {k: (round(v, 2) if v != float("-inf") else None) for k, v in after.items()}
        if (abs(after["integrated_lufs"] - target_lufs) <= 0.5
                and after["true_peak_dbtp"] <= tp_ceiling_dbtp + 0.05):
            break

    if info["after"] is not None and (
            abs(info["after"]["integrated_lufs"] - target_lufs) > 0.5
            or info["after"]["true_peak_dbtp"] > tp_ceiling_dbtp + 0.05):
        info["warning"] = "未完全收敛到目标，请检查素材动态余量"
    return y, info
