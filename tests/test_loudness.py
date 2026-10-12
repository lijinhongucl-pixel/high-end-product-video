#!/usr/bin/env python3
"""loudness.py 回归：ITU 系数对照、参考响度、门控、真峰值、限幅收敛。

运行：python -m pytest tests/test_loudness.py -v
"""

import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import loudness as ld  # noqa: E402

RATE = 48000


def sine(rate, freq, amp, seconds):
    return [amp * math.sin(2 * math.pi * freq * i / rate) for i in range(int(rate * seconds))]


def test_k_weight_matches_itu_published_coefficients():
    """48kHz 下必须复现 ITU-R BS.1770-4 公布系数（防改错滤波器的金丝雀）"""
    shelf, hpf = ld.k_weight_biquads(48000)
    pub_shelf = (1.53512485958697, -2.69169618940638, 1.19839281085285,
                 -1.69065929318241, 0.73248077421585)
    pub_hpf = (1.0, -2.0, 1.0, -1.99004745483398, 0.99007225036621)
    for name, got, pub in (("shelf", shelf, pub_shelf), ("hpf", hpf, pub_hpf)):
        err = max(abs(a - b) for a, b in zip(got, pub))
        assert err < 1e-6, "%s 系数偏差 %.2e" % (name, err)


def test_reference_loudness_mono_sine():
    """单声道 997Hz -20dBFS → -23.70 LUFS（由 BS.1770 公式推导的参考值）"""
    sig = sine(RATE, 997, 10 ** (-20.0 / 20.0), 10)
    lufs = ld.integrated_loudness(sig, RATE)
    assert abs(lufs - (-23.70)) < 0.15, "实测 %.2f LUFS" % lufs


def test_reference_loudness_stereo_sum():
    """双声道同相叠加功率翻倍 → +3.01 LU"""
    sig = sine(RATE, 997, 10 ** (-20.0 / 20.0), 10)
    lufs = ld.integrated_loudness(sig, RATE)
    doubled = ld.integrated_loudness([s * math.sqrt(2) for s in sig], RATE)
    assert abs((doubled - lufs) - 3.01) < 0.05


def test_silence_is_neg_inf():
    assert ld.integrated_loudness([0.0] * RATE, RATE) == float("-inf")


def test_gating_ignores_quiet_noise():
    """门控应把 -80 LUFS 级底噪排除在集成响度之外"""
    signal = sine(RATE, 997, 0.1, 10)
    noise = [0.0001 * math.sin(2 * math.pi * 50 * i / RATE) for i in range(RATE * 5)]
    mixed = noise + signal + noise
    pure = ld.integrated_loudness(signal, RATE)
    got = ld.integrated_loudness(mixed, RATE)
    assert abs(got - pure) < 0.3, "纯信号 %.2f vs 混合 %.2f" % (pure, got)


def test_true_peak_of_sine():
    """正弦的真峰值等于其幅度"""
    sig = sine(RATE, 997, 10 ** (-20.0 / 20.0), 2)
    tp = ld.true_peak_dbtp(sig, RATE)
    assert abs(tp - (-20.0)) < 0.05, "实测 %.2f dBTP" % tp


def test_true_peak_detects_intersample_peak():
    """构造样本峰值相同但插值峰更高的信号：接近 Nyquist 的高频正弦"""
    sig = sine(RATE, RATE * 0.45, 0.5, 1)   # 21.6kHz，接近 Nyquist
    tp = ld.true_peak_dbtp(sig, RATE)
    sp = 20 * math.log10(0.5)
    assert tp >= sp - 0.01                      # 不低于样本峰
    assert tp <= sp + 1.0                        # 插值峰上冲有限


def test_limiter_respects_ceiling():
    hot = [1.5 * math.sin(2 * math.pi * 440 * i / RATE) for i in range(RATE * 2)]
    lim, red, margin = ld.limit_true_peak(hot, 10 ** (-1.0 / 20), RATE)
    tp = ld.true_peak_dbtp(lim, RATE)
    assert tp <= -0.95, "限幅后真峰值 %.2f dBTP" % tp
    assert red < 0


def test_limiter_handles_transient_burst():
    base = [0.2 * math.sin(2 * math.pi * 300 * i / RATE) for i in range(RATE)]
    for i in range(RATE // 2, RATE // 2 + 200):
        base[i] += 2.0 * math.sin(2 * math.pi * 1000 * i / RATE)
    lim, _red, _margin = ld.limit_true_peak(base, 10 ** (-1.0 / 20), RATE)
    assert ld.true_peak_dbtp(lim, RATE) <= -0.95


def test_master_converges_lufs_and_tp():
    quiet = sine(RATE, 997, 0.05, 20)
    out, info = ld.master(quiet, RATE, target_lufs=-16.0, tp_ceiling_dbtp=-1.0)
    after = info["after"]
    assert abs(after["integrated_lufs"] - (-16.0)) <= 0.5, after
    assert after["true_peak_dbtp"] <= -0.95, after
    assert len(out) == len(quiet)


def test_master_on_quiet_material_needs_gain():
    quiet = sine(RATE, 997, 0.02, 10)
    _out, info = ld.master(quiet, RATE, target_lufs=-16.0)
    assert info["gain_db"] > 10, "安静素材应大幅增益，实测 %s" % info["gain_db"]


def test_master_rejects_silence():
    _out, info = ld.master([0.0] * RATE, RATE)
    assert "error" in info
