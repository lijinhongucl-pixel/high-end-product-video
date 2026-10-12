#!/usr/bin/env python3
"""流水线回归：时间轴反推、混音响度、门禁正反例、PNG 解码、帧序列检查。

全部用合成数据，不依赖 Playwright / FFmpeg / 网络。
运行：python -m pytest tests/test_pipeline.py -v
"""

import json
import math
import os
import struct
import subprocess
import sys
import tempfile
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import loudness as ld          # noqa: E402
import media_lib as ml         # noqa: E402
import verify_quality as vq    # noqa: E402

SCRIPTS = os.path.join(os.path.dirname(__file__), "..", "scripts")
PY = sys.executable


def run_script(name, *args):
    return subprocess.run([PY, os.path.join(SCRIPTS, name)] + list(args),
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def sine_wav(path, rate, freq, amp, seconds):
    ml.write_wav_mono(path, rate, [amp * math.sin(2 * math.pi * freq * i / rate)
                                   for i in range(int(rate * seconds))])


def write_png(path, w, h, pix):
    raw = b"".join(b"\x00" + bytes(pix[y * w * 3:(y + 1) * w * 3]) for y in range(h))

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff))

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw))
           + chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(png)


# ---------------------------------------------------------------- 时间轴

def test_timeline_inversion_offline():
    """离线模式：旁白行数 = 镜头数，scene = voice + pre + post"""
    with tempfile.TemporaryDirectory() as d:
        narration = os.path.join(d, "narration.txt")
        with open(narration, "w", encoding="utf-8") as f:
            f.write("第一句旁白\n第二句旁白\n第三句旁白\n")
        out = os.path.join(d, "timeline.json")
        r = run_script("tts_timeline.py", narration, "--mode", "offline",
                       "--audio-dir", os.path.join(d, "voice"), "--out", out)
        assert r.returncode == 0, r.stdout.decode()
        tl = json.load(open(out, encoding="utf-8"))
        assert len(tl["scenes"]) == 3
        assert tl["pre_pad"] == 0.6 and tl["post_pad"] == 0.4
        for i, sc in enumerate(tl["scenes"]):
            expect = sc["voice_duration"] + 0.6 + 0.4
            assert abs(sc["scene_duration"] - expect) < 0.01
            assert sc["voice_duration"] > 0
            if i > 0:
                assert abs(sc["scene_start"] - tl["scenes"][i - 1]["scene_end"]) < 0.01
        assert abs(tl["total_duration"] - tl["scenes"][-1]["scene_end"]) < 0.01


def test_timeline_existing_mode():
    with tempfile.TemporaryDirectory() as d:
        vd = os.path.join(d, "voice")
        os.makedirs(vd)
        sine_wav(os.path.join(vd, "001.wav"), 44100, 300, 0.4, 2.0)
        sine_wav(os.path.join(vd, "002.wav"), 44100, 300, 0.4, 3.0)
        narration = os.path.join(d, "n.txt")
        open(narration, "w", encoding="utf-8").write("甲\n乙\n")
        out = os.path.join(d, "tl.json")
        r = run_script("tts_timeline.py", narration, "--mode", "existing",
                       "--audio-dir", vd, "--out", out)
        assert r.returncode == 0, r.stdout.decode()
        tl = json.load(open(out, encoding="utf-8"))
        # 2s/3s 正弦，VAD 应裁掉头尾静音，有声时长接近文件时长
        assert 1.7 < tl["scenes"][0]["voice_duration"] < 2.1
        assert 2.7 < tl["scenes"][1]["voice_duration"] < 3.1


# ---------------------------------------------------------------- 混音

def _make_timeline(d, n=2):
    vd = os.path.join(d, "voice")
    os.makedirs(vd, exist_ok=True)
    scenes = []
    cursor = 0.0
    for i in range(1, n + 1):
        sine_wav(os.path.join(vd, "%03d.wav" % i), 48000, 300, 0.3, 2.0)
        vd_dur = 2.0
        scenes.append({
            "index": i, "audio": "voice/%03d.wav" % i,
            "voice_start": 0.0, "voice_end": vd_dur, "voice_duration": vd_dur,
            "scene_start": round(cursor, 3),
            "scene_end": round(cursor + vd_dur + 1.0, 3),
            "scene_duration": round(vd_dur + 1.0, 3),
        })
        cursor += vd_dur + 1.0
    tl = {"mode": "test", "pre_pad": 0.6, "post_pad": 0.4,
          "scenes": scenes, "total_duration": round(cursor, 3)}
    path = os.path.join(d, "timeline.json")
    json.dump(tl, open(path, "w", encoding="utf-8"))
    return path


def test_audio_engine_lufs_mastering():
    with tempfile.TemporaryDirectory() as d:
        tl = _make_timeline(d)
        bgm = os.path.join(d, "bgm.wav")
        sine_wav(bgm, 44100, 110, 0.25, 3.0)
        master = os.path.join(d, "master.wav")
        r = run_script("audio_engine.py", "--timeline", tl, "--bgm", bgm,
                       "--audio-dir", os.path.join(d, "voice"), "--out", master)
        assert r.returncode == 0, r.stdout.decode()
        rate, samples = ml.read_wav_mono(master)
        m = ld.measure(samples, rate)
        assert abs(m["integrated_lufs"] - (-16.0)) <= 0.5, m
        assert m["true_peak_dbtp"] <= -0.95, m


def test_audio_engine_no_bgm():
    with tempfile.TemporaryDirectory() as d:
        tl = _make_timeline(d, n=1)
        master = os.path.join(d, "m.wav")
        r = run_script("audio_engine.py", "--timeline", tl, "--no-bgm",
                       "--audio-dir", os.path.join(d, "voice"), "--out", master)
        assert r.returncode == 0, r.stdout.decode()
        rate, samples = ml.read_wav_mono(master)
        assert ld.true_peak_dbtp(samples, rate) <= -0.95


def test_ducking_reduces_bgm_during_speech():
    """人声期间 BGM 增益应显著低于停顿期间"""
    with tempfile.TemporaryDirectory() as d:
        tl = _make_timeline(d, n=1)
        bgm = os.path.join(d, "bgm.wav")
        sine_wav(bgm, 48000, 110, 0.4, 6.0)
        master = os.path.join(d, "m.wav")
        r = run_script("audio_engine.py", "--timeline", tl, "--bgm", bgm,
                       "--audio-dir", os.path.join(d, "voice"), "--out", master)
        assert r.returncode == 0, r.stdout.decode()
        rate, mix = ml.read_wav_mono(master)
        speech = mix[int(1.2 * rate):int(2.0 * rate)]     # 人声段
        gap = mix[int(0.1 * rate):int(0.5 * rate)]        # 前留白段
        # 人声段能量应远高于纯 BGM 停顿段
        def rms(seg):
            return math.sqrt(sum(v * v for v in seg) / len(seg))
        assert rms(speech) > rms(gap) * 2


# ---------------------------------------------------------------- 门禁

def _good_timeline_dict():
    scenes = []
    cursor = 0.0
    for i in range(1, 3):
        dur = 3.0
        scenes.append({"index": i, "audio": "x.wav", "voice_duration": 2.0,
                       "scene_start": cursor, "scene_end": cursor + dur,
                       "scene_duration": dur})
        cursor += dur
    return {"pre_pad": 0.6, "post_pad": 0.4, "scenes": scenes,
            "total_duration": cursor}


def test_gate_hard_checks_pass_on_master():
    with tempfile.TemporaryDirectory() as d:
        tl = _make_timeline(d, n=2)
        master = os.path.join(d, "master.wav")
        r = run_script("audio_engine.py", "--timeline", tl, "--no-bgm",
                       "--audio-dir", os.path.join(d, "voice"), "--out", master)
        assert r.returncode == 0
        r = run_script("verify_quality.py", "--timeline", tl, "--audio", master,
                       "--lufs", "-16")
        assert r.returncode == 0, r.stdout.decode()
        assert "门禁通过" in r.stdout.decode()


def test_gate_catches_peak_clipping():
    with tempfile.TemporaryDirectory() as d:
        tl = _make_timeline(d, n=1)
        loud = os.path.join(d, "loud.wav")
        ml.write_wav_mono(loud, 48000,
                          [0.99 * math.sin(2 * math.pi * 440 * i / 48000)
                           for i in range(48000 * 3)])
        r = run_script("verify_quality.py", "--timeline", tl, "--audio", loud)
        assert r.returncode == 1
        assert "TRUE_PEAK_CEILING" in r.stdout.decode()
        assert "FAIL" in r.stdout.decode()


def test_gate_catches_speech_overlap():
    with tempfile.TemporaryDirectory() as d:
        tl = _make_timeline(d, n=2)
        data = json.load(open(tl, encoding="utf-8"))
        data["scenes"][1]["scene_start"] = data["scenes"][0]["scene_start"] + 1.0
        data["scenes"][1]["scene_end"] = data["scenes"][1]["scene_start"] + 3.0
        data["total_duration"] = data["scenes"][1]["scene_end"]
        bad = os.path.join(d, "bad.json")
        json.dump(data, open(bad, "w", encoding="utf-8"))
        master = os.path.join(d, "m.wav")
        r = run_script("audio_engine.py", "--timeline", tl, "--no-bgm",
                       "--audio-dir", os.path.join(d, "voice"), "--out", master)
        assert r.returncode == 0
        r = run_script("verify_quality.py", "--timeline", bad, "--audio", master)
        assert r.returncode == 1
        assert "NO_SPEECH_OVERLAP" in r.stdout.decode()


def test_gate_catches_timeline_gap():
    tl = _good_timeline_dict()
    tl["scenes"][1]["scene_start"] += 0.8
    tl["scenes"][1]["scene_end"] += 0.8
    ok, detail = vq.check_timeline_continuous(tl)
    assert not ok and "断档" in detail


def test_gate_catches_tofu_chars():
    with tempfile.TemporaryDirectory() as d:
        sub = os.path.join(d, "bad.srt")
        open(sub, "w", encoding="utf-8").write("1\n00:00:00,000 --> 00:00:01,000\n模型 ▢ 入口 \ufffd\n")
        ok, detail = vq.check_tofu(sub, "")
        assert not ok and "U+FFFD" in detail
        good = os.path.join(d, "good.srt")
        open(good, "w", encoding="utf-8").write("1\n00:00:00,000 --> 00:00:01,000\n模型入口\n")
        ok, detail = vq.check_tofu(good, "")
        assert ok


def test_gate_loudness_miss():
    with tempfile.TemporaryDirectory() as d:
        tl = _make_timeline(d, n=1)
        quiet = os.path.join(d, "quiet.wav")
        ml.write_wav_mono(quiet, 48000,
                          [0.001 * math.sin(2 * math.pi * 440 * i / 48000)
                           for i in range(48000 * 3)])
        r = run_script("verify_quality.py", "--timeline", tl, "--audio", quiet,
                       "--lufs", "-16")
        assert r.returncode == 1
        assert "LOUDNESS_TARGET" in r.stdout.decode()


# ---------------------------------------------------------------- 帧序列

def test_png_decoder_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "t.png")
        w = h = 24
        pix = bytearray()
        for y in range(h):
            for x in range(w):
                pix += bytes([(x * 10) % 256, (y * 10) % 256, 128])
        write_png(p, w, h, pix)
        gw, gh, gray = ml.read_png_gray(p)
        assert (gw, gh) == (w, h)
        # 灰度 = 0.299r + 0.587g + 0.114b，抽几个点核对
        assert abs(gray[0][0] - (0.299 * 0 + 0.587 * 0 + 0.114 * 128)) < 1
        assert abs(gray[1][2] - (0.299 * 20 + 0.587 * 10 + 0.114 * 128)) < 1


def test_frame_sequence_check():
    with tempfile.TemporaryDirectory() as d:
        fd = os.path.join(d, "frames")
        os.makedirs(fd)
        w = h = 32
        for i in range(10):
            pix = bytearray()
            for y in range(h):
                for x in range(w):
                    pix += bytes([(i * 20 + x) % 256, 100, 200])
            write_png(os.path.join(fd, "%06d.png" % i), w, h, pix)
        tl = {"scenes": [{"scene_start": 0.0, "scene_end": 1.0, "scene_duration": 1.0}],
              "total_duration": 1.0}
        ok, detail, files = vq.check_frame_sequence(fd, tl, 10)
        assert ok, detail
        assert len(files) == 10
        # MOTION_ALIVE：帧都在动，应通过（转场窗口 0 会全查）
        ok2, detail2 = vq.check_motion_alive(fd, files, tl, 10, 0.0, 0.25, 480)
        assert ok2, detail2


def test_motion_alive_catches_dead_frames():
    with tempfile.TemporaryDirectory() as d:
        fd = os.path.join(d, "frames")
        os.makedirs(fd)
        w = h = 32
        base = None
        for i in range(8):
            pix = bytearray()
            for y in range(h):
                for x in range(w):
                    if i in (4, 5, 6):     # 中间三帧完全相同 = 持续静止
                        pix += bytes([100, 100, 100])
                    else:
                        pix += bytes([(i * 30 + x) % 256, 60, 180])
            if base is None:
                base = bytes(pix)
            write_png(os.path.join(fd, "%06d.png" % i), w, h, pix)
        tl = {"scenes": [{"scene_start": 0.0, "scene_end": 0.8, "scene_duration": 0.8}],
              "total_duration": 0.8}
        files = sorted(os.listdir(fd))
        ok, detail = vq.check_motion_alive(fd, files, tl, 10, 0.0, 0.25, 480)
        assert not ok, "死帧检测失效"
        assert "死帧" in detail


def test_frame_sequence_catches_count_mismatch():
    with tempfile.TemporaryDirectory() as d:
        fd = os.path.join(d, "frames")
        os.makedirs(fd)
        for i in range(3):
            write_png(os.path.join(fd, "%06d.png" % i), 16, 16, bytes([100] * 16 * 16 * 3))
        tl = {"scenes": [{"scene_start": 0.0, "scene_end": 1.0, "scene_duration": 1.0}],
              "total_duration": 1.0}
        ok, detail, _files = vq.check_frame_sequence(fd, tl, 30)  # 应有 30 帧
        assert not ok and "帧数" in detail
