#!/usr/bin/env python3
"""声音时钟逆推 —— 旁白真实波形 → 声画时间轴

核心公式：
    T_scene = T_voice + T_pre(前留白 0.6s) + T_post(后留白 0.4s)

画面时长由旁白真实波形反推，而不是拍脑袋定。三种模式：

  offline   本地合成占位音频（零依赖，波形时长与字数成正比），用于跑通流水线与测试
  edge      调用 edge-tts 生成高质量人声（需 pip install edge-tts）
  existing  用你已有的 wav 文件，只做时长提取与时间轴生成

密钥一律从环境变量读取，本脚本不硬编码任何 key，用 existing 模式甚至不需要密钥。

用法：
  python3 scripts/tts_timeline.py narration.txt --mode offline --out timeline.json
  python3 scripts/tts_timeline.py narration.txt --mode edge --voice zh-CN-YunxiNeural
  python3 scripts/tts_timeline.py narration.txt --mode existing --audio-dir ./my_voice
"""

import argparse
import json
import math
import os
import shlex
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import media_lib as ml  # noqa: E402

PRE_PAD = 0.6   # 前留白：观众看清标题与 UI，BGM 起手，根除抢跑
POST_PAD = 0.4  # 后留白：纯净缝隙触发转场音效
PLACEHOLDER_RATE = 22050          # 离线占位音频采样率
CHARS_PER_SEC = 4.8               # 中文语速经验值：每秒 4.8 字
LEAD_SILENCE = 0.08               # 占位音频前置静音，用于验证 VAD 裁剪
TAIL_SILENCE = 0.12               # 占位音频尾部静音
DEFAULT_EDGE_VOICE = "zh-CN-YunxiNeural"
DEFAULT_EDGE_CMD = "edge-tts --voice {voice} --text {text} --write-media {out}"


# ---------------------------------------------------------------- 离线占位音频

def synth_voice(text, index, rate=PLACEHOLDER_RATE):
    """生成本地合成占位人声：时长与字数成正比，带音节包络，确定性输出。

    这是跑流水线用的机器人嗡鸣，不是可发布人声。成片请用 edge 或 existing 模式。
    """
    chars = sum(1 for c in text if not c.isspace())
    dur = 0.35 + max(1, chars) / float(CHARS_PER_SEC)
    n = int(dur * rate)
    rng = random_sequence(index)
    f0 = 105.0 + (index * 37) % 45          # 105~149Hz 基频，逐行错开
    syllable = 0.20                          # 每音节 200ms
    n_syll = max(1, int(round(dur / syllable)))

    out = [0.0] * n
    vib_phase = 0.0
    for i in range(n):
        t = i / float(rate)
        # 音节包络：每 syllable 秒一次 attack/decay
        sp = (t / syllable) % 1.0
        if sp < 0.25:
            env = sp / 0.25
        else:
            env = max(0.0, 1.0 - (sp - 0.25) / 0.75)
        env = env ** 1.5

        vib_phase += (2 * math.pi * 5.2) / rate   # 5.2Hz 颤音
        f = f0 * (1.0 + 0.012 * math.sin(vib_phase))

        v = 0.0
        for h, amp in ((1, 1.0), (2, 0.45), (3, 0.28), (4, 0.16), (5, 0.09), (6, 0.05)):
            v += amp * math.sin(2 * math.pi * f * h * t + h)
        v /= 2.03
        # 轻噪底，避免能量包络过于干净导致 VAD 边界失真
        v += (rng() - 0.5) * 0.004

        # 整体淡入淡出 + 头尾静音
        fade = min(1.0, t / 0.02, max(0.0, (dur - t) / 0.03))
        if t < LEAD_SILENCE or t > dur - TAIL_SILENCE:
            fade = 0.0
        out[i] = v * env * fade * 0.55
    return out


def random_sequence(seed):
    """确定性伪随机（线性同余），保证同一输入永远得到同一占位音频"""
    state = (seed * 1103515245 + 12345) & 0x7FFFFFFF

    def nxt():
        nonlocal state
        state = (state * 1103515245 + 12345) & 0x7FFFFFFF
        return state / 8388608.0 % 1.0
    return nxt


# ---------------------------------------------------------------- 外部 TTS

def run_edge_tts(lines, audio_dir, voice, tts_cmd):
    os.makedirs(audio_dir, exist_ok=True)
    for i, text in enumerate(lines, 1):
        out = os.path.join(audio_dir, "%03d.wav" % i)
        cmd = tts_cmd.replace("{text}", shlex.quote(text))
        cmd = cmd.replace("{voice}", voice).replace("{out}", shlex.quote(out))
        print("  [%d/%d] edge-tts → %s" % (i, len(lines), out))
        rc = subprocess.call(cmd, shell=True)
        if rc != 0 or not os.path.exists(out):
            raise SystemExit(
                "TTS 命令失败（退出码 %d）：%s\n"
                "请确认已 pip install edge-tts，或用 --tts-cmd 指定你自己的命令。" % (rc, cmd)
            )
    return len(lines)


# ---------------------------------------------------------------- 主流程

def read_lines(script_path):
    with open(script_path, encoding="utf-8") as f:
        lines = [l.strip() for l in f if l.strip() and not l.strip().startswith("#")]
    if not lines:
        raise SystemExit("旁白文本为空：%s" % script_path)
    return lines


def build_timeline(files, audio_dir, lines, pre, post, mode):
    scenes = []
    cursor = 0.0
    for i, fn in enumerate(files, 1):
        path = os.path.join(audio_dir, fn)
        rate, samples = ml.read_wav_mono(path)
        s_idx, e_idx, _peak = ml.vad_bounds(samples, rate)
        vd = max(0.0, (e_idx - s_idx) / float(rate))
        scene_dur = vd + pre + post
        scenes.append({
            "index": i,
            "text": lines[i - 1] if i - 1 < len(lines) else "",
            "audio": os.path.join(audio_dir, fn).replace(os.sep, "/"),
            "rate": rate,
            "voice_start": round(s_idx / float(rate), 3),
            "voice_end": round(e_idx / float(rate), 3),
            "voice_duration": round(vd, 3),
            "scene_start": round(cursor, 3),
            "scene_end": round(cursor + scene_dur, 3),
            "scene_duration": round(scene_dur, 3),
        })
        print("  %-12s 有声 %.2fs → 镜头 %.2fs" % (fn, vd, scene_dur))
        cursor += scene_dur

    return {
        "mode": mode,
        "pre_pad": pre,
        "post_pad": post,
        "scenes": scenes,
        "total_duration": round(cursor, 3),
    }


def main():
    ap = argparse.ArgumentParser(
        description="声音时钟逆推：旁白真实波形 → 声画时间轴",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n"
               "  python3 tts_timeline.py narration.txt --mode offline\n"
               "  python3 tts_timeline.py narration.txt --mode edge --voice zh-CN-YunxiNeural\n"
               "  python3 tts_timeline.py narration.txt --mode existing --audio-dir ./my_voice\n",
    )
    ap.add_argument("script", help="旁白文本文件，每行一句（# 开头的行忽略）")
    ap.add_argument("--mode", choices=("offline", "edge", "existing"), default="offline",
                    help="offline=本地合成占位音频；edge=edge-tts；existing=用已有 wav（默认 offline）")
    ap.add_argument("--voice", default=DEFAULT_EDGE_VOICE, help="edge 模式的音色")
    ap.add_argument("--tts-cmd", help="自定义 TTS 命令，占位符 {text} {voice} {out}，覆盖 edge 默认命令")
    ap.add_argument("--audio-dir", default="./voice",
                    help="音频输出目录（offline/edge）或输入目录（existing），默认 ./voice")
    ap.add_argument("--out", default="timeline.json", help="输出的时间轴 JSON")
    ap.add_argument("--md", help="同时输出可读的 Markdown 时间轴")
    ap.add_argument("--pre", type=float, default=PRE_PAD, help="前留白秒数，默认 %.1f" % PRE_PAD)
    ap.add_argument("--post", type=float, default=POST_PAD, help="后留白秒数，默认 %.1f" % POST_PAD)
    args = ap.parse_args()

    lines = read_lines(args.script)

    if args.mode == "existing":
        if not os.path.isdir(args.audio_dir):
            raise SystemExit("existing 模式的音频目录不存在：%s" % args.audio_dir)
        files = sorted(f for f in os.listdir(args.audio_dir) if f.lower().endswith(".wav"))
        if not files:
            raise SystemExit("音频目录里没有 wav：%s" % args.audio_dir)
        if len(files) != len(lines):
            print("  注意：%d 行文本 vs %d 个音频，多出的行 text 字段留空" % (len(lines), len(files)))
    else:
        os.makedirs(args.audio_dir, exist_ok=True)
        if args.mode == "edge":
            cmd = args.tts_cmd or DEFAULT_EDGE_CMD
            run_edge_tts(lines, args.audio_dir, args.voice, cmd)
        else:
            print("  离线模式：生成本地合成占位音频（机器人嗡鸣，仅用于跑通流水线/测试）")
            print("  要可发布人声，请改用 --mode edge 或 --mode existing")
            for i, text in enumerate(lines, 1):
                out = os.path.join(args.audio_dir, "%03d.wav" % i)
                ml.write_wav_mono(out, PLACEHOLDER_RATE, synth_voice(text, i))
                print("  [%d/%d] 合成 %s" % (i, len(lines), out))
        files = sorted(f for f in os.listdir(args.audio_dir) if f.lower().endswith(".wav"))

    print()
    print("  解析 %d 段旁白（mode=%s, pre=%.2fs, post=%.2fs）" % (len(files), args.mode, args.pre, args.post))
    print()

    data = build_timeline(files, args.audio_dir, lines, args.pre, args.post, args.mode)

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print()
    print("  总时长 %.2fs（%d 镜） 已写入 %s" % (data["total_duration"], len(files), args.out))

    if args.md:
        rows = [
            "# 声画时间轴",
            "",
            "| 镜头 | 文本 | 人声起止 | 有声时长 | 画面区间 | 画面时长 |",
            "|---|---|---|---|---|---|",
        ]
        for s in data["scenes"]:
            rows.append("| %d | %s | %.2f–%.2f | %.2fs | %.2f–%.2f | %.2fs |" % (
                s["index"], s["text"], s["voice_start"], s["voice_end"],
                s["voice_duration"], s["scene_start"], s["scene_end"], s["scene_duration"],
            ))
        rows += ["", "总计 %.2f 秒" % data["total_duration"], ""]
        with open(args.md, "w", encoding="utf-8") as f:
            f.write("\n".join(rows))
        print("  已写入 %s" % args.md)

    print()
    print("  下一步：按 scene_duration 渲染每个镜头，人声在镜头开始 %.2fs 后进入" % args.pre)


if __name__ == "__main__":
    main()
