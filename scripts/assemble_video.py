#!/usr/bin/env python3
"""视频合成 —— 帧序列 + 母带 → H.264 成片（FFmpeg 封装）

编码参数（与 SKILL.md 的质量基线一致，改这里不如改文档再同步改这里）：
  -c:v libx264 -crf 16 -maxrate 4500k -bufsize 9000k   capped CRF：画质优先，码率封顶
  -pix_fmt yuv420p -profile:v high -level 4.1          兼容性基线（手机/网页/剪辑软件）
  -movflags +faststart                                  moov 前置，网页即点即播
  -c:a aac -b:a 192k -ar 48000                          AAC 192k，48kHz 与母带对齐
  -t <帧数/fps>                                         以帧序列时长为准，音频多不退少

--verify 会用 ffmpeg 把成片音轨解码回 WAV，跑一遍 BS.1770-4 响度与真峰值复测，
防止 AAC 有损编码把交付指标带偏（通常会掉 0.1~0.3 LU，在容差内即合格）。

依赖：ffmpeg（必须），帧序列来自 render_frames.py，母带来自 audio_engine.py。

用法：
  python3 scripts/assemble_video.py --frames frames --audio master.wav --out final.mp4
  python3 scripts/assemble_video.py --frames frames --audio master.wav --out final.mp4 --verify
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import loudness as ld  # noqa: E402
import media_lib as ml  # noqa: E402

FRAME_EXTS = {"jpeg": "jpg", "jpg": "jpg", "png": "png"}


def find_ffmpeg():
    ff = shutil.which("ffmpeg")
    if not ff:
        raise SystemExit("找不到 ffmpeg，请先安装（brew install ffmpeg / apt install ffmpeg）。")
    return ff


def list_frames(frames_dir):
    files = sorted(f for f in os.listdir(frames_dir)
                   if f.lower().endswith((".png", ".jpg", ".jpeg")))
    return files


def main():
    ap = argparse.ArgumentParser(description="帧序列 + 母带 → H.264 成片")
    ap.add_argument("--frames", required=True, help="render_frames.py 输出的帧目录")
    ap.add_argument("--audio", required=True, help="audio_engine.py 输出的母带 wav")
    ap.add_argument("--out", default="final.mp4", help="输出文件，默认 final.mp4")
    ap.add_argument("--fps", type=int, default=30, help="帧率，默认 30")
    ap.add_argument("--crf", type=int, default=16, help="x264 CRF，默认 16")
    ap.add_argument("--maxrate", default="4500k", help="码率上限，默认 4500k")
    ap.add_argument("--preset", default="slow", help="x264 preset，默认 slow")
    ap.add_argument("--audio-bitrate", default="192k", help="AAC 码率，默认 192k")
    ap.add_argument("--lufs", type=float, default=-16.0, help="复验目标响度 LUFS，默认 -16")
    ap.add_argument("--tp", type=float, default=-1.0, help="复验真峰值上限 dBTP，默认 -1.0")
    ap.add_argument("--verify", action="store_true", help="成片后解码音轨复测响度/真峰值")
    args = ap.parse_args()

    ffmpeg = find_ffmpeg()
    if not os.path.isdir(args.frames):
        raise SystemExit("帧目录不存在：%s" % args.frames)
    files = list_frames(args.frames)
    if len(files) < 2:
        raise SystemExit("帧目录里帧数不足：%s（%d 个）" % (args.frames, len(files)))
    if not os.path.exists(args.audio):
        raise SystemExit("母带不存在：%s" % args.audio)

    ext = os.path.splitext(files[0])[1].lstrip(".").lower()
    if ext not in FRAME_EXTS:
        raise SystemExit("不支持的帧格式：%s" % ext)
    # 帧号从 0 开始、6 位补零是 render_frames.py 的约定
    first_stem = os.path.splitext(files[0])[0]
    pattern = os.path.join(args.frames, "%06d." + FRAME_EXTS[ext])
    start_number = int(first_stem) if first_stem.isdigit() else 0
    duration = len(files) / float(args.fps)

    cmd = [
        ffmpeg, "-y",
        "-framerate", str(args.fps),
        "-start_number", str(start_number),
        "-i", pattern,
        "-i", args.audio,
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "libx264",
        "-crf", str(args.crf),
        "-maxrate", args.maxrate,
        "-bufsize", str(int(float(args.maxrate.replace("k", "")) * 2)) + "k",
        "-preset", args.preset,
        "-pix_fmt", "yuv420p",
        "-profile:v", "high",
        "-level", "4.1",
        "-movflags", "+faststart",
        "-c:a", "aac",
        "-b:a", args.audio_bitrate,
        "-ar", "48000",
        "-t", "%.3f" % duration,
        args.out,
    ]
    print("  合成 %d 帧 @ %dfps（%.2fs）→ %s" % (len(files), args.fps, duration, args.out))
    print("  编码：libx264 crf%s maxrate%s preset%s + AAC %s"
          % (args.crf, args.maxrate, args.preset, args.audio_bitrate))
    # ffmpeg 的输出直接继承：实时进度对用户有用，也避免管道缓冲把进程挂住
    proc = subprocess.run(cmd)
    if proc.returncode != 0:
        raise SystemExit("ffmpeg 合成失败（退出码 %d）" % proc.returncode)
    size_mb = os.path.getsize(args.out) / 1048576.0
    print("  完成：%s（%.1f MB，%.0f kbps 等效）"
          % (args.out, size_mb, size_mb * 8192 / duration))

    if args.verify:
        print()
        print("  复验成片音轨（AAC 解码 → BS.1770-4 复测）")
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
            tmp_wav = tf.name
        try:
            dec = subprocess.run(
                [ffmpeg, "-y", "-v", "error", "-i", args.out, "-vn",
                 "-acodec", "pcm_s16le", "-ar", "48000", "-ac", "1", tmp_wav],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            if dec.returncode != 0:
                print("  警告：音轨解码失败，跳过复验", file=sys.stderr)
            else:
                rate, samples = ml.read_wav_mono(tmp_wav)
                m = ld.measure(samples, rate)
                lufs, tp = m["integrated_lufs"], m["true_peak_dbtp"]
                ok_lufs = lufs != float("-inf") and abs(lufs - args.lufs) <= 1.0
                ok_tp = tp != float("-inf") and tp <= args.tp + 0.05
                print("    集成响度 %.2f LUFS（目标 %.1f）%s"
                      % (lufs, args.lufs, "达标" if ok_lufs else "不达标"))
                print("    真峰值 %.2f dBTP（上限 %.1f）%s"
                      % (tp, args.tp, "达标" if ok_tp else "不达标"))
                if not (ok_lufs and ok_tp):
                    print("  复验未通过：AAC 编码或母带本身偏离交付基线，请回查 audio_engine。",
                          file=sys.stderr)
                    return 1
        finally:
            if os.path.exists(tmp_wav):
                os.unlink(tmp_wav)

    print()
    print("  下一步：python3 scripts/verify_quality.py --timeline timeline.json --audio %s "
          "--frames %s --fps %d --lufs %.1f --strict" % (args.audio, args.frames, args.fps, args.lufs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
