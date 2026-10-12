#!/usr/bin/env python3
"""音频引擎 —— 人声铺轨 + BGM 侧链闪避混音 + 广播级母带处理

按 tts_timeline.py 生成的 timeline.json 把各段人声铺到正确位置，
人声发声期间把 BGM 压到 18%，停顿后 250ms 内平滑恢复到 35%，
然后走母带链（默认 BS.1770-4 响度归一 + 真峰值限幅）：

  lufs 模式（默认）  集成响度归一到 -16 LUFS，真峰值限到 -1.0 dBTP
  peak 模式（旧行为） 峰值归一化到目标 dBFS

交付标准以响度（LUFS）为准，不以峰值为准：两条同样 -1 dBFS 的音轨，
听感可以差 10 个 LU。网络视频基线 -16 LUFS / -1.0 dBTP。

只用 Python 标准库。不自带 BGM，不联网下载，不内置任何密钥。

用法：
  python3 scripts/audio_engine.py --timeline timeline.json --bgm bgm.wav --out master.wav
  python3 scripts/audio_engine.py --timeline timeline.json --out master.wav --no-bgm
  python3 scripts/audio_engine.py --timeline timeline.json --bgm bgm.wav --lufs-mode peak --target -1.0
"""

import argparse
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import loudness as ld  # noqa: E402
import media_lib as ml  # noqa: E402

TARGET_RATE = 48000   # 视频交付标准采样率
DUCK_LEVEL = 0.18     # 人声期间 BGM 保留 18%
DUCK_REST = 0.35      # 停顿期间 BGM 恢复到 35%
TRANSITION = 0.25     # 状态切换的平滑时长（秒），约 3 倍时间常数
SEAM_XFADE = 0.005    # BGM 循环缝合处交叉淡化，防爆音
TAIL_SEC = 1.0        # 母带尾部多留 1 秒，给末镜头淡出空间
DEFAULT_LUFS = -16.0
DEFAULT_TP = -1.0


def resolve_audio(audio_field, timeline_path, audio_dir):
    """按优先级解析时间轴里的音频路径，返回第一个存在的路径"""
    if os.path.isabs(audio_field) and os.path.exists(audio_field):
        return audio_field
    base = os.path.basename(audio_field)
    candidates = [
        os.path.join(audio_dir, base) if audio_dir else None,
        audio_field,
        os.path.join(os.path.dirname(os.path.abspath(timeline_path)), audio_field),
        os.path.join(os.path.dirname(os.path.abspath(timeline_path)), base),
        base,
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    raise SystemExit("找不到音频文件：%s（试过 --audio-dir=%s 与时间轴同目录）" % (audio_field, audio_dir))


def build_voice_bus(timeline, timeline_path, audio_dir, rate):
    """按时间轴铺人声。返回 (人声总线, 有声区间列表[(start,end)])"""
    total = float(timeline.get("total_duration", 0.0)) + TAIL_SEC
    n_total = int(total * rate)
    bus = [0.0] * n_total
    pre = float(timeline.get("pre_pad", 0.6))
    spans = []
    prev_end = None

    for sc in timeline.get("scenes", []):
        path = resolve_audio(sc.get("audio", ""), timeline_path, audio_dir)
        src_rate, samples = ml.read_wav_mono(path)
        s_idx, e_idx, _peak = ml.vad_bounds(samples, src_rate)
        speech = samples[s_idx:e_idx]
        if src_rate != rate:
            speech = ml.resample_linear(speech, src_rate, rate)

        # 人声落点：镜头起点 + 前留白，保证每个镜头都有 0.6s 纯画面起手
        start = int(round((float(sc.get("scene_start", 0.0)) + pre) * rate))
        if start + len(speech) > n_total:
            bus.extend([0.0] * (start + len(speech) - n_total))
            n_total = start + len(speech)
        for i, v in enumerate(speech):
            bus[start + i] += v

        end = start + len(speech)
        spans.append((start / float(rate), end / float(rate)))
        if prev_end is not None and start / float(rate) < prev_end - 0.001:
            print("  警告：第 %d 段人声与上一段重叠（%.2fs < %.2fs），门禁会拦截"
                  % (sc.get("index", 0), start / float(rate), prev_end), file=sys.stderr)
        prev_end = end / float(rate)

    if ml.peak_linear(bus) <= 0.0:
        raise SystemExit("人声总线全静音，请检查音频文件与时间轴是否匹配。")
    return bus, spans


def fit_bgm(samples, src_rate, rate, total):
    """BGM 对齐到目标采样率与总时长，循环处 5ms 交叉淡化防爆音"""
    seg = ml.resample_linear(samples, src_rate, rate)
    if not seg:
        return [0.0] * total
    xf = min(int(SEAM_XFADE * rate), len(seg) // 2)
    if xf > 1:
        for i in range(xf):
            head = seg[i] * (i / float(xf))
            tail = seg[len(seg) - xf + i] * (1.0 - i / float(xf))
            seg[i] = head + tail
    if len(seg) >= total:
        return seg[:total]
    out = [0.0] * total
    full = len(seg)
    for i in range(total):
        out[i] = seg[i % full]
    return out


def duck_gain_curve(voice_bus, rate, duck=DUCK_LEVEL, rest=DUCK_REST, transition=TRANSITION):
    """由人声能量包络生成 BGM 闪避增益曲线（每样本一个增益值）"""
    env = ml.rms_envelope(voice_bus, rate)
    mask = ml.speech_mask(env)
    if not mask:
        return [rest] * len(voice_bus), 0.0

    tau = transition / 3.0
    alpha = 1.0 - math.exp(-ml.FRAME / tau)
    size = max(1, int(round(rate * ml.FRAME)))
    g = rest
    curve = []
    fi = 0
    for i in range(len(voice_bus)):
        if i % size == 0 and fi < len(mask):
            target = duck if mask[fi] else rest
            g += (target - g) * alpha
            fi += 1
        curve.append(g)
    coverage = sum(1 for m in mask if m) / float(len(mask))
    return curve, coverage


def fmt(v, suffix=""):
    return "-inf" if v == float("-inf") else "%.2f%s" % (v, suffix)


def main():
    ap = argparse.ArgumentParser(
        description="侧链闪避混音 + 母带处理",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--timeline", required=True, help="tts_timeline.py 生成的 timeline.json")
    ap.add_argument("--audio-dir", default="./voice", help="人声目录（时间轴路径解析不到时兜底）")
    ap.add_argument("--bgm", help="BGM 音频文件（wav）")
    ap.add_argument("--no-bgm", action="store_true", help="不要 BGM，只输出人声母带")
    ap.add_argument("--out", default="master.wav", help="输出母带路径")
    ap.add_argument("--rate", type=int, default=TARGET_RATE, help="输出采样率，默认 48000")
    ap.add_argument("--voice-gain", type=float, default=1.0, help="人声增益，默认 1.0")
    ap.add_argument("--bgm-gain", type=float, default=1.0, help="BGM 增益，默认 1.0")
    ap.add_argument("--duck", type=float, default=DUCK_LEVEL, help="人声期间 BGM 保留比例，默认 0.18")
    ap.add_argument("--rest", type=float, default=DUCK_REST, help="停顿期间 BGM 恢复比例，默认 0.35")
    ap.add_argument("--transition", type=float, default=TRANSITION, help="闪避平滑时长秒，默认 0.25")
    ap.add_argument("--lufs-mode", choices=("lufs", "peak"), default="lufs",
                    help="lufs=响度归一+真峰限幅（默认）；peak=旧版峰值归一")
    ap.add_argument("--lufs", type=float, default=DEFAULT_LUFS, help="目标集成响度 LUFS，默认 -16")
    ap.add_argument("--tp", type=float, default=DEFAULT_TP, help="真峰值上限 dBTP，默认 -1.0")
    ap.add_argument("--target", type=float, default=-1.0,
                    help="peak 模式的峰值归一目标 dBFS，默认 -1.0")
    args = ap.parse_args()

    if not args.no_bgm and not args.bgm:
        raise SystemExit("要么给 --bgm 指一个 wav，要么加 --no-bgm 只输出人声母带。")
    if args.no_bgm and args.bgm:
        raise SystemExit("--bgm 和 --no-bgm 只能选一个。")

    with open(args.timeline, encoding="utf-8") as f:
        timeline = json.load(f)
    scenes = timeline.get("scenes") or []
    if not scenes:
        raise SystemExit("时间轴里没有场景：%s" % args.timeline)

    print("  铺人声：%d 段 → %d Hz" % (len(scenes), args.rate))
    voice_bus, spans = build_voice_bus(timeline, args.timeline, args.audio_dir, args.rate)
    total = len(voice_bus) / float(args.rate)

    if args.no_bgm:
        print("  --no-bgm：跳过闪避，直接输出人声母带")
        mixed = [v * args.voice_gain for v in voice_bus]
        coverage = 0.0
    else:
        bgm_rate, bgm_samples = ml.read_wav_mono(args.bgm)
        print("  铺 BGM：%s（%.1fs @ %d Hz，循环缝合 %.0fms）"
              % (args.bgm, len(bgm_samples) / float(bgm_rate), bgm_rate, SEAM_XFADE * 1000))
        bgm_bus = fit_bgm(bgm_samples, bgm_rate, args.rate, len(voice_bus))
        curve, coverage = duck_gain_curve(voice_bus, args.rate, args.duck, args.rest, args.transition)
        mixed = [
            voice_bus[i] * args.voice_gain + bgm_bus[i] * args.bgm_gain * curve[i]
            for i in range(len(voice_bus))
        ]
        print("  侧链闪避：人声期间 BGM → %.0f%%，停顿 %.0fms 内恢复 → %.0f%%（有声覆盖率 %.1f%%）"
              % (args.duck * 100, args.transition * 1000, args.rest * 100, coverage * 100))

    if args.lufs_mode == "lufs":
        before = ld.measure(mixed, args.rate)
        print()
        print("  母带处理（BS.1770-4）：目标 %.1f LUFS / %.1f dBTP" % (args.lufs, args.tp))
        print("    处理前：%.2f LUFS，短期峰值 %.2f LUFS，真峰值 %.2f dBTP"
              % (before["integrated_lufs"], before["shortterm_max_lufs"], before["true_peak_dbtp"]))
        master, info = ld.master(mixed, args.rate, target_lufs=args.lufs, tp_ceiling_dbtp=args.tp)
        if "error" in info:
            raise SystemExit("母带处理失败：%s" % info["error"])
        after = info["after"] or {}
        print("    增益 %+.2f dB，限幅余量 %.1f dB（%d 轮）"
              % (info["gain_db"], info["limiter_margin_db"], info["iterations"]))
        print("    处理后：%s LUFS，短期峰值 %s LUFS，真峰值 %s dBTP"
              % (fmt(after.get("integrated_lufs")), fmt(after.get("shortterm_max_lufs")),
                 fmt(after.get("true_peak_dbtp"))))
        if info.get("warning"):
            print("    警告：%s" % info["warning"], file=sys.stderr)
    else:
        peak_before = ml.dbfs(ml.peak_linear(mixed))
        master, achieved = ml.normalize_peak(mixed, args.target)
        if achieved == float("-inf"):
            raise SystemExit("混音结果全静音，请检查输入音频。")
        print()
        print("  峰值归一（旧模式）：%.2f → %.2f dBFS" % (peak_before, achieved))

    ml.write_wav_mono(args.out, args.rate, master)

    print()
    print("  母带 %.2fs  已写入 %s" % (total, args.out))
    print("  下一步：python3 scripts/assemble_video.py --frames frames/ --audio %s --out final.mp4" % args.out)


if __name__ == "__main__":
    main()
