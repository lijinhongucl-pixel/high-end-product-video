#!/usr/bin/env python3
"""共享媒体原语 —— WAV 读写、能量包络、VAD、峰值归一、线性重采样、PNG 灰度解码

tts_timeline.py / audio_engine.py / verify_quality.py 三个脚本共用本模块。
只用 Python 标准库，Python 3.8+ 可跑，零第三方依赖。
"""

import math
import os
import struct
import wave
import zlib

FRAME = 0.02  # 能量包络分帧长度 20ms，兼顾人声分辨率与计算量

SUPPORTED_WIDTHS = (1, 2)    # 8bit / 16bit PCM
RATE_RANGE = (8000, 48000)   # 合法采样率闭区间


# ---------------------------------------------------------------- WAV 读写

def read_wav_mono(path):
    """读 WAV 并混成单声道。返回 (采样率, float 列表[-1, 1])"""
    with wave.open(path, "rb") as w:
        nch = w.getnchannels()
        width = w.getsampwidth()
        rate = w.getframerate()
        raw = w.readframes(w.getnframes())

    if width not in SUPPORTED_WIDTHS:
        raise ValueError("只支持 8/16 位 PCM WAV，收到 %d 位：%s" % (width * 8, path))

    if width == 2:
        ints = struct.unpack("<%dh" % (len(raw) // 2), raw)
        floats = [s / 32768.0 for s in ints]
    else:
        ints = struct.unpack("<%dB" % len(raw), raw)
        floats = [(s - 128) / 128.0 for s in ints]

    if nch > 1:
        mono = []
        for i in range(0, len(floats) - nch + 1, nch):
            mono.append(sum(floats[i:i + nch]) / nch)
        floats = mono
    return rate, floats


def write_wav_mono(path, rate, samples):
    """写 16bit PCM 单声道 WAV，超范围样本硬截断到 [-1, 1]"""
    ints = []
    for s in samples:
        v = -1.0 if s < -1.0 else (1.0 if s > 1.0 else s)
        ints.append(int(round(v * 32767)))
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(rate))
        w.writeframes(struct.pack("<%dh" % len(ints), *ints))


# ---------------------------------------------------------------- 包络 / VAD

def rms_envelope(samples, rate, frame=FRAME):
    """按帧算 RMS 包络，返回每帧一个能量值"""
    size = max(1, int(round(rate * frame)))
    env = []
    for i in range(0, len(samples) - size + 1, size):
        chunk = samples[i:i + size]
        env.append(math.sqrt(sum(s * s for s in chunk) / size))
    return env


def vad_bounds(samples, rate, threshold_ratio=0.05):
    """RMS 能量法找真实发声区间。返回 (start_idx, end_idx, peak_env)

    start_idx/end_idx 是样本下标；peak_env 为全局帧能量峰值（供阈值判断）。
    全静音时返回 (0, len(samples), 0.0)。
    """
    env = rms_envelope(samples, rate)
    if not env:
        return 0, len(samples), 0.0
    peak = max(env)
    if peak <= 0.0:
        return 0, len(samples), 0.0

    thresh = peak * threshold_ratio
    start_i = next((i for i, v in enumerate(env) if v >= thresh), 0)
    end_i = next((i for i in range(len(env) - 1, -1, -1) if env[i] >= thresh), len(env) - 1)

    size = max(1, int(round(rate * FRAME)))
    return start_i * size, min(len(samples), (end_i + 1) * size), peak


def speech_mask(env, threshold_ratio=0.05):
    """把能量包络转成布尔发声掩码"""
    if not env:
        return []
    peak = max(env)
    if peak <= 0.0:
        return [False] * len(env)
    thresh = peak * threshold_ratio
    return [v >= thresh for v in env]


# ---------------------------------------------------------------- 峰值 / 归一

def peak_linear(samples):
    """最大绝对样本值"""
    return max((abs(s) for s in samples), default=0.0)


def dbfs(x):
    """线性幅度 → dBFS。x <= 0 时返回 -inf"""
    if x <= 0.0:
        return float("-inf")
    return 20.0 * math.log10(x)


def normalize_peak(samples, target_dbfs=-1.0):
    """峰值归一化到 target_dbfs。返回 (新列表, 实际达到的 dBFS)"""
    peak = peak_linear(samples)
    if peak <= 0.0:
        return list(samples), float("-inf")
    gain = (10.0 ** (target_dbfs / 20.0)) / peak
    out = [s * gain for s in samples]
    return out, dbfs(peak_linear(out))


# ---------------------------------------------------------------- 重采样

def resample_linear(samples, src_rate, dst_rate):
    """线性插值重采样。质量一般，仅用于占位音频与 BGM 对齐，不做出版级处理"""
    if src_rate == dst_rate or not samples:
        return list(samples)
    ratio = dst_rate / float(src_rate)
    n_out = int(round(len(samples) * ratio))
    if n_out <= 0:
        return []
    out = []
    for i in range(n_out):
        pos = i / ratio
        i0 = int(pos)
        if i0 >= len(samples) - 1:
            out.append(samples[-1])
            continue
        frac = pos - i0
        out.append(samples[i0] * (1.0 - frac) + samples[i0 + 1] * frac)
    return out


# ---------------------------------------------------------------- PNG 灰度解码

def _paeth(a, b, c):
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def read_png_gray(path, step=1):
    """最小 PNG 解码器：取 8bit 非隔行 PNG，转成灰度二维列表。

    返回 (width, height, gray)，gray[y][x] 为 0..255 浮点。
    step > 1 时按步长抽样像素，用于大图降载。
    不支持隔行（Adam7）与 16bit，遇到就报错而不是静默出错。
    """
    with open(path, "rb") as f:
        data = f.read()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("不是 PNG 文件：%s" % path)

    pos = 8
    idat = []
    plte = None
    width = height = depth = ctype = interlace = None
    while pos + 8 <= len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        ctag = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        pos += 12 + length  # length + tag + body + crc
        if ctag == b"IHDR":
            width, height, depth, ctype, _comp, _filt, interlace = struct.unpack(">IIBBBBB", body)
        elif ctag == b"PLTE":
            plte = [tuple(body[i:i + 3]) for i in range(0, len(body), 3)]
        elif ctag == b"IDAT":
            idat.append(body)
        elif ctag == b"IEND":
            break

    if depth != 8:
        raise ValueError("只支持 8bit PNG，收到 %dbit：%s" % (depth, path))
    if interlace:
        raise ValueError("不支持隔行 PNG：%s" % path)
    if width is None or height is None or width <= 0 or height <= 0:
        raise ValueError("PNG 头信息缺失或非法：%s" % path)

    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(ctype)
    if channels is None:
        raise ValueError("不支持的 PNG 颜色类型 %d：%s" % (ctype, path))

    raw = zlib.decompress(b"".join(idat))
    stride = width * channels
    expected = (stride + 1) * height
    if len(raw) < expected:
        raise ValueError("PNG 数据不完整（%d < %d）：%s" % (len(raw), expected, path))

    # 逐扫描线反滤波
    recon = bytearray(stride * height)
    prev = bytearray(stride)
    bpp = channels
    for y in range(height):
        base = y * (stride + 1)
        ftype = raw[base]
        line = bytearray(raw[base + 1:base + 1 + stride])
        if ftype == 0:
            pass
        elif ftype == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif ftype == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ftype == 3:
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif ftype == 4:
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                upleft = prev[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + _paeth(left, prev[i], upleft)) & 0xFF
        else:
            raise ValueError("非法 PNG 滤波器 %d：%s" % (ftype, path))
        recon[y * stride:(y + 1) * stride] = line
        prev = line

    # 转灰度（Rec.601 luma），按 step 抽样
    gray = []
    for y in range(0, height, step):
        row = []
        base = y * stride
        for x in range(0, width, step):
            off = base + x * channels
            if ctype == 3:  # 调色板索引
                r, g, b = plte[recon[off]]
                row.append(0.299 * r + 0.587 * g + 0.114 * b)
            elif ctype == 0:  # 灰度
                row.append(float(recon[off]))
            elif ctype == 4:  # 灰度+alpha，按白底合成
                v, a = recon[off], recon[off + 1]
                row.append(v * (a / 255.0) + 255.0 * (1.0 - a / 255.0))
            else:  # RGB / RGBA
                r, g, b = recon[off], recon[off + 1], recon[off + 2]
                a = recon[off + 3] if channels == 4 else 255
                lum = 0.299 * r + 0.587 * g + 0.114 * b
                row.append(lum * (a / 255.0) + 255.0 * (1.0 - a / 255.0))
        gray.append(row)
    return width, height, gray


def path_size(path):
    return os.path.getsize(path) if os.path.exists(path) else 0


def frame_dims(path):
    """帧图尺寸。PNG 读 IHDR；其他格式用 ffmpeg 转一张 PNG 再读。"""
    if path.lower().endswith(".png"):
        with open(path, "rb") as f:
            head = f.read(24)
        return int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")
    import shutil
    import subprocess
    import tempfile
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("读取 %s 尺寸需要 ffmpeg" % path)
    out = subprocess.run(
        [ffmpeg, "-v", "error", "-i", path, "-frames:v", "1",
         "-f", "image2", "-vcodec", "png", "pipe:1"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True,
    ).stdout
    return int.from_bytes(out[16:20], "big"), int.from_bytes(out[20:24], "big")


def stream_frame_gray(frames_dir, files, target_width=480):
    """把整个帧序列一次性解码成灰度 rawvideo 流，逐帧产出 (w, h, bytes)。

    一次 ffmpeg 子进程吃下全部帧（C 解码 + scale 滤波），比每帧起一个
    ffmpeg 快两个数量级。没有 ffmpeg 时抛 RuntimeError，调用方回退到
    内置 PNG 解码器逐帧读。
    """
    import shutil
    import subprocess
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("流式解码帧序列需要 ffmpeg")
    ext = os.path.splitext(files[0])[1].lstrip(".").lower()
    ext = "jpg" if ext == "jpeg" else ext
    start = os.path.splitext(files[0])[0]
    start_number = int(start) if start.isdigit() else 0
    w0, h0 = frame_dims(os.path.join(frames_dir, files[0]))
    tw = max(2, min(target_width, w0))
    th = max(2, int(round(h0 * tw / float(w0))))
    tw -= tw % 2
    th -= th % 2
    pattern = os.path.join(frames_dir, "%06d." + ext)
    cmd = [ffmpeg, "-v", "error",
           "-start_number", str(start_number),
           "-i", pattern,
           "-vf", "scale=%d:%d" % (tw, th),
           "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    fsz = tw * th
    try:
        while True:
            buf = proc.stdout.read(fsz)
            if not buf or len(buf) < fsz:
                break
            yield tw, th, buf
    finally:
        try:
            proc.stdout.close()
        except Exception:
            pass
        proc.wait()
