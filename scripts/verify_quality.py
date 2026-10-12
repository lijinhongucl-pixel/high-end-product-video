#!/usr/bin/env python3
"""质量门禁 —— 交付前必须全绿，任一硬性检查失败即阻断

九项检查全部可机械执行，替代原版 SKILL 里「严禁偷懒」这类口号：

  AUDIO_DECODABLE       WAV 可读、时长 > 0、采样率/位宽合法            硬
  TRUE_PEAK_CEILING     真峰值（4x 过采样）<= -1.0 dBTP                硬
  LOUDNESS_TARGET       集成响度（BS.1770-4 门控）命中目标 ±1 LU       硬（需 --lufs）
  NO_SPEECH_OVERLAP     相邻人声间隙 >= 0.3s                           硬
  TIMELINE_CONTINUOUS   时间轴无断档、无重叠、总时长自洽                硬
  NO_TOFU_CHARS         字幕不含乱码方块字符（需 --subtitle）           硬
  FRAME_SEQUENCE        帧数/尺寸/连续性符合 fps 与时长（需 --frames）   硬
  MOTION_ALIVE          排除转场窗口后无静止死帧（需 --frames）         软
  AUDIO_COVERS_TIMELINE 母带不短于时间轴总时长                         软

退出码：硬性失败 → 1；软性失败 → 1（仅当 --strict），否则 0。

用法：
  python3 scripts/verify_quality.py --timeline timeline.json --audio master.wav --lufs -16
  python3 scripts/verify_quality.py --timeline timeline.json --audio master.wav \
      --subtitle subs.srt --frames frames/ --fps 30 --strict
"""

import argparse
import json
import os
import sys
import wave

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import loudness as ld  # noqa: E402
import media_lib as ml  # noqa: E402

# 乱码方块字符集。原版写作「不得含 '▢', '', '?'」，其中空字符串 '' 会让检查
# 100% 误报；这里改成明确的字符集，并允许 --extra-tofu 追加。
TOFU_CHARS = {
    "\ufffd": "替换字符 U+FFFD",
    "\u25a1": "白方块 U+25A1",
    "\u25a0": "黑方块 U+25A0",
    "\u25a7": "方块 U+25A7",
    "\u25a8": "方块 U+25A8",
    "\u25ae": "方块 U+25AE",
    "\u25af": "方块 U+25AF",
    "\u2b1b": "大方块 U+2B1B",
}

FRAME_EXTS = (".png", ".jpg", ".jpeg")
EPS = 1e-6


class Report(object):
    def __init__(self):
        self.rows = []

    def add(self, name, level, ok, detail, skipped=False):
        status = "SKIP" if skipped else ("PASS" if ok else "FAIL")
        self.rows.append({"name": name, "level": level, "status": status, "detail": detail})

    def print_report(self):
        print()
        print("  %-22s %-5s %-6s %s" % ("检查项", "级别", "结果", "说明"))
        print("  " + "-" * 74)
        for r in self.rows:
            print("  %-22s %-5s %-6s %s" % (r["name"], r["level"], r["status"], r["detail"]))
        hard_fail = [r for r in self.rows if r["level"] == "硬" and r["status"] == "FAIL"]
        soft_fail = [r for r in self.rows if r["level"] == "软" and r["status"] == "FAIL"]
        print("  " + "-" * 74)
        print("  硬性 %d 项：%d 失败；软性 %d 项：%d 失败"
              % (len([r for r in self.rows if r["level"] == "硬"]), len(hard_fail),
                 len([r for r in self.rows if r["level"] == "软"]), len(soft_fail)))
        return len(hard_fail), len(soft_fail)


# ---------------------------------------------------------------- 单项检查

def check_audio_decodable(path):
    try:
        with wave.open(path, "rb") as w:
            n = w.getnframes()
            rate = w.getframerate()
            width = w.getsampwidth()
    except (wave.Error, EOFError, OSError) as e:
        return False, "无法解码：%s" % e
    if n <= 0:
        return False, "时长为 0"
    if width not in ml.SUPPORTED_WIDTHS:
        return False, "位宽 %d bit 不在 %s" % (width * 8, ml.SUPPORTED_WIDTHS)
    if not (ml.RATE_RANGE[0] <= rate <= ml.RATE_RANGE[1]):
        return False, "采样率 %d Hz 超出 [%d, %d]" % (rate, ml.RATE_RANGE[0], ml.RATE_RANGE[1])
    return True, "%.2fs @ %d Hz, %d bit" % (n / float(rate), rate, width * 8)


def check_true_peak(path, ceiling_dbtp):
    _rate, samples = ml.read_wav_mono(path)
    tp = ld.true_peak_dbtp(samples, _rate)
    if tp == float("-inf"):
        return False, "音频全静音", float("-inf")
    ok = tp <= ceiling_dbtp + 0.05
    return ok, "真峰值 %.2f dBTP（上限 %.2f）" % (tp, ceiling_dbtp), tp


def check_loudness(path, target_lufs):
    rate, samples = ml.read_wav_mono(path)
    lufs = ld.integrated_loudness(samples, rate)
    if lufs == float("-inf"):
        return False, "音频全静音", float("-inf")
    ok = abs(lufs - target_lufs) <= 1.0
    return ok, "集成响度 %.2f LUFS（目标 %.1f ±1.0）" % (lufs, target_lufs), lufs


def voice_windows(timeline):
    """每个场景的人声绝对区间 [(start, end)]"""
    pre = float(timeline.get("pre_pad", 0.6))
    spans = []
    for sc in timeline.get("scenes", []):
        start = float(sc.get("scene_start", 0.0)) + pre
        spans.append((start, start + float(sc.get("voice_duration", 0.0))))
    return spans


def check_speech_overlap(timeline, min_gap):
    spans = voice_windows(timeline)
    bad = []
    for i in range(1, len(spans)):
        gap = spans[i][0] - spans[i - 1][1]
        if gap < min_gap - EPS:
            bad.append("第%d段前间隙 %.2fs < %.2fs" % (i + 1, gap, min_gap))
    if bad:
        return False, "；".join(bad[:3]) + (" 等%d处" % len(bad) if len(bad) > 3 else "")
    return True, "%d 段人声，最小间隙达标" % len(spans)


def check_timeline_continuous(timeline):
    scenes = timeline.get("scenes", [])
    if not scenes:
        return False, "时间轴没有场景"
    bad = []
    for i, sc in enumerate(scenes):
        dur = float(sc.get("scene_duration", 0.0))
        if dur <= 0:
            bad.append("第%d段时长 %.2fs 非法" % (i + 1, dur))
        if abs(float(sc.get("scene_end", 0.0)) - float(sc.get("scene_start", 0.0)) - dur) > 0.01:
            bad.append("第%d段 scene_start/end 与 scene_duration 不自洽" % (i + 1))
        if i > 0:
            gap = float(sc.get("scene_start", 0.0)) - float(scenes[i - 1].get("scene_end", 0.0))
            if abs(gap) > 0.01:
                bad.append("第%d段与第%d段之间有 %.2fs 断档/重叠" % (i + 1, i, gap))
    total = float(timeline.get("total_duration", 0.0))
    if abs(total - float(scenes[-1].get("scene_end", 0.0))) > 0.01:
        bad.append("total_duration %.2f 与末段 scene_end %.2f 不一致" % (total, scenes[-1].get("scene_end", 0.0)))
    if bad:
        return False, "；".join(bad[:3]) + (" 等%d处" % len(bad) if len(bad) > 3 else "")
    return True, "%d 镜连续，总时长 %.2fs" % (len(scenes), total)


def check_tofu(subtitle_path, extra):
    tofu = dict(TOFU_CHARS)
    for c in extra:
        tofu.setdefault(c, "自定义字符 %r" % c)
    hits = []
    with open(subtitle_path, encoding="utf-8", errors="replace") as f:
        for ln, line in enumerate(f, 1):
            for ch, desc in tofu.items():
                if ch in line:
                    hits.append("第%d行 %s" % (ln, desc))
    if hits:
        return False, "；".join(hits[:3]) + (" 等%d处" % len(hits) if len(hits) > 3 else "")
    return True, "字幕 %s 未检出乱码方块字符" % os.path.basename(subtitle_path)


def list_frames(frames_dir):
    files = sorted(f for f in os.listdir(frames_dir) if f.lower().endswith(FRAME_EXTS))
    return files


def check_frame_sequence(frames_dir, timeline, fps):
    """帧数、编号连续性、尺寸一致性"""
    files = list_frames(frames_dir)
    if len(files) < 2:
        return False, "帧数不足（%d）" % len(files), []
    total = float(timeline.get("total_duration", 0.0))
    expect = int(round(total * fps))
    bad = []
    if abs(len(files) - expect) > 1:
        bad.append("帧数 %d 与时长×fps %d 不符" % (len(files), expect))
    stems = [int(os.path.splitext(f)[0]) for f in files if os.path.splitext(f)[0].isdigit()]
    if len(stems) != len(files):
        bad.append("存在非数字编号的帧文件")
    elif stems != list(range(stems[0], stems[0] + len(stems))):
        bad.append("帧编号不连续（缺帧或乱序）")
    dims = set()
    for f in files[: min(len(files), 40)] + files[-min(len(files), 40):]:
        path = os.path.join(frames_dir, f)
        try:
            if f.lower().endswith(".png"):
                with open(path, "rb") as fh:
                    head = fh.read(33)
                w = int.from_bytes(head[16:20], "big")
                h = int.from_bytes(head[20:24], "big")
                dims.add((w, h))
            else:
                import subprocess
                import shutil
                ffmpeg = shutil.which("ffmpeg")
                if ffmpeg:
                    out = subprocess.run([ffmpeg, "-v", "error", "-i", path, "-frames:v", "1",
                                          "-f", "image2", "-vcodec", "png", "pipe:1"],
                                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                         check=True).stdout
                    w = int.from_bytes(out[16:20], "big")
                    h = int.from_bytes(out[20:24], "big")
                    dims.add((w, h))
        except Exception as e:
            bad.append("%s 读取失败：%s" % (f, e))
    if len(dims) > 1:
        bad.append("帧尺寸不一致：%s" % sorted(dims)[:4])
    if bad:
        return False, "；".join(bad[:3]), files
    return True, "%d 帧连续，尺寸 %s" % (len(files), sorted(dims)[0] if dims else "?"), files


def check_motion_alive(frames_dir, files, timeline, fps, transition_window, dead_threshold,
                       frame_width):
    """逐帧灰度差查死帧。转场窗口内（scene 边界 ±window）的帧不判。

    有 ffmpeg 时走流式解码（一次子进程吃完整个序列）；否则回退内置 PNG
    解码器逐帧读（仅支持 PNG 帧序列）。
    """
    if len(files) < 2:
        return True, "帧数不足，跳过"
    boundaries = [float(sc.get("scene_start", 0.0)) for sc in timeline.get("scenes", [])]
    boundaries += [float(sc.get("scene_end", 0.0)) for sc in timeline.get("scenes", [])]

    def in_transition(t):
        return any(abs(t - b) <= transition_window for b in boundaries)

    dead = []
    compared = 0
    hist = []        # 最近两帧 [(t, bytes), (t, bytes)]
    idx = 0
    try:
        stream = ml.stream_frame_gray(frames_dir, files, target_width=frame_width)
        for w, h, buf in stream:
            t = idx / float(fps)
            # 与两帧前（约 67ms）比对：振荡运动在单个极值点会有一两帧几乎
            # 不变，那是正常的；持续静止才会在两帧跨度上仍然为零。
            if len(hist) == 2:
                pt, pbuf = hist[0]
                if not in_transition(t) and not in_transition(pt):
                    diff = sum(abs(a - b) for a, b in zip(pbuf, buf)) / float(len(buf))
                    compared += 1
                    if diff < dead_threshold:
                        dead.append("%s@%.2fs(diff=%.2f)" % (files[idx], t, diff))
            hist.append((t, buf))
            if len(hist) > 2:
                hist.pop(0)
            idx += 1
        if idx != len(files):
            return False, "流式解码产出 %d 帧，目录里有 %d 帧，序列可能损坏" % (idx, len(files))
    except RuntimeError as e:
        # 无 ffmpeg：回退内置 PNG 解码器（仅 PNG）
        if not all(f.lower().endswith(".png") for f in files):
            return False, "无 ffmpeg 且帧序列含非 PNG，无法逐帧比对（%s）" % e
        step = max(1, int(round(1920 / float(frame_width))))
        prev_gray = None
        prev_t = None
        for i, fn in enumerate(files):
            try:
                _w, _h, gray = ml.read_png_gray(os.path.join(frames_dir, fn), step=step)
            except Exception as e2:
                print("  警告：%s 解码失败（%s）" % (fn, e2), file=sys.stderr)
                prev_gray = None
                continue
            t = i / float(fps)
            if prev_gray is not None and not in_transition(t) and not in_transition(prev_t):
                hh = min(len(prev_gray), len(gray))
                ww = min(len(prev_gray[0]), len(gray[0])) if hh else 0
                if ww:
                    diff = sum(abs(prev_gray[y][x] - gray[y][x])
                               for y in range(hh) for x in range(ww)) / float(hh * ww)
                    compared += 1
                    if diff < dead_threshold:
                        dead.append("%s@%.2fs(diff=%.2f)" % (fn, t, diff))
            prev_gray, prev_t = gray, t

    if dead:
        return False, "检出 %d 个疑似死帧：%s" % (len(dead), "、".join(dead[:3]))
    return True, "比对 %d 组相邻帧（%dpx 宽），无静止死帧" % (compared, frame_width)


def check_covers_timeline(audio_path, timeline):
    with wave.open(audio_path, "rb") as w:
        dur = w.getnframes() / float(w.getframerate())
    total = float(timeline.get("total_duration", 0.0))
    if dur + 0.5 < total:
        return False, "母带 %.2fs 短于时间轴 %.2fs" % (dur, total)
    return True, "母带 %.2fs ≥ 时间轴 %.2fs" % (dur, total)


# ---------------------------------------------------------------- 主流程

def main():
    ap = argparse.ArgumentParser(description="质量门禁：可机械执行的交付前检查")
    ap.add_argument("--timeline", required=True, help="timeline.json")
    ap.add_argument("--audio", required=True, help="母带 wav")
    ap.add_argument("--subtitle", help="字幕文件（srt/vtt/txt），启用 NO_TOFU_CHARS")
    ap.add_argument("--frames", help="帧序列目录，启用 FRAME_SEQUENCE 与 MOTION_ALIVE")
    ap.add_argument("--fps", type=int, default=30, help="帧率，默认 30")
    ap.add_argument("--max-peak", type=float, default=-1.0, help="真峰值上限 dBTP，默认 -1.0")
    ap.add_argument("--lufs", type=float, help="目标集成响度 LUFS，给出才启用 LOUDNESS_TARGET")
    ap.add_argument("--min-gap", type=float, default=0.3, help="相邻人声最小间隙秒，默认 0.3")
    ap.add_argument("--transition-window", type=float, default=0.3,
                    help="转场窗口秒，窗口内的帧不判死帧，默认 0.3")
    ap.add_argument("--dead-threshold", type=float, default=0.25,
                    help="两帧跨度平均灰度差（0~255）低于该值判死帧，默认 0.25。"
                         "标定：真死帧 diff≈0.00~0.05，有设计的微动 diff≈0.6~1.2，"
                         "阈值取两者之间；想更严就调高")
    ap.add_argument("--frame-width", type=int, default=480,
                    help="死帧比对的帧缩放宽度（高度按比例），默认 480，越大越慢")
    ap.add_argument("--extra-tofu", default="", help="追加的乱码字符，直接写在参数里")
    ap.add_argument("--strict", action="store_true", help="软性检查失败也返回非零退出码")
    args = ap.parse_args()

    with open(args.timeline, encoding="utf-8") as f:
        timeline = json.load(f)

    rep = Report()

    ok, detail = check_audio_decodable(args.audio)
    rep.add("AUDIO_DECODABLE", "硬", ok, detail)
    if not ok:
        rep.print_report()
        return 1

    ok, detail, _tp = check_true_peak(args.audio, args.max_peak)
    rep.add("TRUE_PEAK_CEILING", "硬", ok, detail)

    if args.lufs is not None:
        ok, detail, _lufs = check_loudness(args.audio, args.lufs)
        rep.add("LOUDNESS_TARGET", "硬", ok, detail)
    else:
        rep.add("LOUDNESS_TARGET", "硬", True, "未给 --lufs，跳过", skipped=True)

    ok, detail = check_speech_overlap(timeline, args.min_gap)
    rep.add("NO_SPEECH_OVERLAP", "硬", ok, detail)

    ok, detail = check_timeline_continuous(timeline)
    rep.add("TIMELINE_CONTINUOUS", "硬", ok, detail)

    if args.subtitle:
        ok, detail = check_tofu(args.subtitle, args.extra_tofu)
        rep.add("NO_TOFU_CHARS", "硬", ok, detail)
    else:
        rep.add("NO_TOFU_CHARS", "硬", True, "未提供 --subtitle，跳过", skipped=True)

    frame_files = []
    if args.frames:
        ok, detail, frame_files = check_frame_sequence(args.frames, timeline, args.fps)
        rep.add("FRAME_SEQUENCE", "硬", ok, detail)
        if ok:
            ok, detail = check_motion_alive(args.frames, frame_files, timeline, args.fps,
                                            args.transition_window, args.dead_threshold,
                                            args.frame_width)
            rep.add("MOTION_ALIVE", "软", ok, detail)
        else:
            rep.add("MOTION_ALIVE", "软", True, "帧序列不合规，跳过死帧检查", skipped=True)
    else:
        rep.add("FRAME_SEQUENCE", "硬", True, "未提供 --frames，跳过", skipped=True)
        rep.add("MOTION_ALIVE", "软", True, "未提供 --frames，跳过", skipped=True)

    ok, detail = check_covers_timeline(args.audio, timeline)
    rep.add("AUDIO_COVERS_TIMELINE", "软", ok, detail)

    hard_fail, soft_fail = rep.print_report()
    print()
    if hard_fail == 0 and (soft_fail == 0 or not args.strict):
        print("  门禁通过，可以交付。")
        return 0
    print("  门禁未通过：硬性失败 %d 项，软性失败 %d 项。" % (hard_fail, soft_fail))
    return 1


if __name__ == "__main__":
    sys.exit(main())
