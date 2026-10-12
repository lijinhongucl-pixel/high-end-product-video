#!/usr/bin/env python3
"""确定性帧渲染器 —— Playwright + HTML 模板，按时间轴逐帧截图

设计要点：
  1. 确定性优先。每个镜头的第 n 帧永远是 t = n/fps，模板的 __render(t) 是
     纯函数，同样 t 在任何机器上画出同一帧。不依赖真实时钟，不掉帧。
  2. 与模板的契约：URL 参数传 scene/visual/theme/w/h，渲染器逐帧调用
     window.__render(t)，见 templates/scenes.html 头部注释。
  3. 自适应并发：多个 Page 并行，按镜头聚合任务，同一镜头连续帧复用页面，
     导航开销摊薄。默认 worker 数 = clamp(cpu//2, 2, 6)。
  4. 白屏重试：截图体积 < --min-bytes 判定为白屏/渲染失败，自动重试，
     超过 --retries 次才报错。

依赖：playwright（pip install playwright），浏览器用 Chromium。
脚本本身零标准库之外依赖，但渲染必须有 Playwright，这是唯一例外。

用法：
  python3 scripts/render_frames.py --timeline timeline.json \
      --template templates/scenes.html --out frames --fps 30
  python3 scripts/render_frames.py ... --preset vertical --theme dark
  python3 scripts/render_frames.py ... --visuals visuals.txt --dry-run
"""

import argparse
import asyncio
import json
import math
import os
import sys
import time

PRESETS = {
    "landscape": (1920, 1080),
    "vertical": (1080, 1920),
    "square": (1080, 1080),
    "wide": (2560, 1080),
}
VISUAL_TYPES = ("title", "feature", "metric", "outro", "custom")
MAX_FRAMES = 20000  # 安全上限，防止时间轴写错导致渲染几十万帧


def load_visuals(path, n_scenes):
    """读 visual 描述。两种格式：
    1. 纯文本：每行一个类型（title/feature/metric/outro/custom）
    2. JSON 数组：[{"type": "metric", "metric": "200", "unit": "+", ...}]
    返回长度 n_scenes 的 dict 列表。
    """
    if not path:
        return [{"type": "title"}] * n_scenes
    with open(path, encoding="utf-8") as f:
        raw = f.read().strip()
    if raw.startswith("["):
        try:
            data = json.loads(raw)
        except ValueError as e:
            raise SystemExit("visuals JSON 解析失败：%s" % e)
        if not isinstance(data, list):
            raise SystemExit("visuals JSON 必须是数组")
    else:
        data = []
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            data.append({"type": line.split()[0].lower()})
    if len(data) != n_scenes:
        raise SystemExit("visuals 有 %d 条，时间轴有 %d 个镜头，数量必须一致"
                         % (len(data), n_scenes))
    out = []
    for i, d in enumerate(data):
        if not isinstance(d, dict):
            raise SystemExit("visuals 第 %d 条不是对象" % (i + 1))
        t = str(d.get("type", "title")).lower()
        if t not in VISUAL_TYPES:
            raise SystemExit("visuals 第 %d 条类型 %r 不支持，可选：%s"
                             % (i + 1, t, "/".join(VISUAL_TYPES)))
        d["type"] = t
        out.append(d)
    return out


def build_tasks(timeline, visuals, fps, ext):
    """展开成帧任务列表：[(帧号, 镜头下标, t, 视觉描述)]"""
    tasks = []
    frame_no = 0
    for i, sc in enumerate(timeline.get("scenes", [])):
        dur = float(sc.get("scene_duration", 0.0))
        n = max(1, int(round(dur * fps)))
        if frame_no + n > MAX_FRAMES:
            raise SystemExit("总帧数超过安全上限 %d，请检查时间轴（当前镜头 %d）"
                             % (MAX_FRAMES, i + 1))
        vis = dict(visuals[i]) if i < len(visuals) else {"type": "title"}
        vis.setdefault("text", sc.get("text", ""))
        vis["duration"] = dur
        vis["scene_index"] = sc.get("index", i + 1)
        for f in range(n):
            tasks.append({
                "frame": frame_no,
                "scene": i,
                "t": f / float(fps),
                "vis": vis,
                "ext": ext,
            })
            frame_no += 1
    return tasks, frame_no


def scene_url(template_path, vis, width, height, theme, seed):
    from urllib.parse import urlencode
    q = {
        "scene": vis.get("scene_index", 1),
        "visual": vis.get("type", "title"),
        "text": vis.get("text", ""),
        "duration": "%.3f" % float(vis.get("duration", 5.0)),
        "eyebrow": vis.get("eyebrow", ""),
        "caption": vis.get("caption", ""),
        "tags": "|".join(vis.get("tags", []) or []),
        "metric": vis.get("metric", ""),
        "unit": vis.get("unit", ""),
        "wordmark": vis.get("wordmark", ""),
        "tagline": vis.get("tagline", ""),
        "theme": theme,
        "w": width,
        "h": height,
        "seed": seed,
    }
    return "file://" + os.path.abspath(template_path) + "?" + urlencode(q)


async def wait_ready(page, timeout_ms):
    await page.wait_for_function("window.__READY__ === true", timeout=timeout_ms)
    try:
        await page.evaluate("document.fonts && document.fonts.ready")
    except Exception:
        pass


async def render_worker(name, browser, tasks, args, stats, total_frames, t_start):
    from playwright.async_api import TimeoutError as PWTimeout

    context = await browser.new_context(
        viewport={"width": args.width, "height": args.height},
        device_scale_factor=1,
    )
    page = await context.new_page()
    await page.add_init_script(
        "try{document.documentElement.style.setProperty('--x','1');}catch(e){}"
    )
    cur_scene = None
    done = 0
    try:
        while True:
            task = await args.queue.get()
            if task is None:
                args.queue.task_done()
                break
            ext = task.get("ext", args.format)
            # Playwright 的 type=jpeg 与磁盘扩展名 .jpg 对齐（ffmpeg 模式串只认一种）
            disk_ext = "jpg" if ext == "jpeg" else ext
            frame_path = os.path.join(args.out, "%s.%s" % (str(task["frame"]).zfill(6), disk_ext))
            for attempt in range(1, args.retries + 1):
                try:
                    if task["scene"] != cur_scene:
                        url = scene_url(args.template, task["vis"], args.width,
                                        args.height, args.theme, args.seed)
                        await page.goto(url, wait_until="load", timeout=30000)
                        await wait_ready(page, 15000)
                        cur_scene = task["scene"]
                    await page.evaluate("(t) => window.__render(t)", task["t"])
                    shot = {"path": frame_path, "type": ext, "animations": "disabled",
                            "timeout": 30000}
                    if ext == "jpeg":
                        shot["quality"] = args.quality
                    await page.screenshot(**shot)
                    size = os.path.getsize(frame_path) if os.path.exists(frame_path) else 0
                    if size < args.min_bytes:
                        raise IOError("截图体积 %d < %d，疑似白屏" % (size, args.min_bytes))
                    ok = True
                    break
                except (PWTimeout, Exception) as e:
                    stats["retries"] += 1
                    if attempt >= args.retries:
                        raise SystemExit(
                            "帧 %d 渲染失败（重试 %d 次）：%s"
                            % (task["frame"], args.retries, e))
                    cur_scene = None  # 强制重新导航
                    await asyncio.sleep(0.2 * attempt)
            done += 1
            stats["done"] += 1
            if stats["done"] % 15 == 0 or stats["done"] == total_frames:
                elapsed = time.time() - t_start
                fps_real = stats["done"] / elapsed if elapsed > 0 else 0
                eta = (total_frames - stats["done"]) / fps_real if fps_real > 0 else 0
                print("  [%s] %d/%d 帧，%.1f 帧/秒，预计剩余 %.0fs"
                      % (name, stats["done"], total_frames, fps_real, eta), flush=True)
            args.queue.task_done()
    finally:
        await context.close()


async def run_async(args, tasks, total_frames):
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        launch_kwargs = {"headless": True}
        if args.browser_path:
            launch_kwargs["executable_path"] = args.browser_path
        browser = await p.chromium.launch(**launch_kwargs)
        stats = {"done": 0, "retries": 0}
        args.queue = asyncio.Queue()
        for t in tasks:
            args.queue.put_nowait(t)
        workers = []
        n_workers = max(1, min(args.workers, len(tasks)))
        t_start = time.time()
        for i in range(n_workers):
            workers.append(asyncio.create_task(
                render_worker("w%d" % (i + 1), browser, tasks, args, stats,
                              total_frames, t_start)))
        # 每 worker 一个结束哨兵
        for _ in range(n_workers):
            args.queue.put_nowait(None)
        await asyncio.gather(*workers)
        await browser.close()
        return stats, time.time() - t_start


def main():
    ap = argparse.ArgumentParser(
        description="确定性帧渲染：HTML 模板 + 时间轴 → 帧序列",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--timeline", required=True, help="tts_timeline.py 生成的 timeline.json")
    ap.add_argument("--template", required=True, help="场景模板 HTML（默认 templates/scenes.html）")
    ap.add_argument("--out", default="./frames", help="帧输出目录，默认 ./frames")
    ap.add_argument("--fps", type=int, default=30, help="帧率，默认 30")
    ap.add_argument("--preset", choices=sorted(PRESETS), default="landscape", help="画幅预设")
    ap.add_argument("--width", type=int, help="自定义宽度（覆盖 preset）")
    ap.add_argument("--height", type=int, help="自定义高度（覆盖 preset）")
    ap.add_argument("--theme", choices=("editorial", "dark"), default="editorial", help="模板主题")
    ap.add_argument("--seed", type=int, default=7, help="模板随机种子（同一 seed 结果可复现）")
    ap.add_argument("--visuals", help="镜头视觉描述文件（每行一个类型，或 JSON 数组）")
    ap.add_argument("--workers", type=int, default=0,
                    help="并发 Page 数，0 = 自动（cpu//2，2~6）")
    ap.add_argument("--retries", type=int, default=3, help="单帧重试次数，默认 3")
    ap.add_argument("--min-bytes", type=int, default=8192,
                    help="截图体积下限，低于此判定白屏，默认 8192")
    ap.add_argument("--format", choices=("jpeg", "png"), default="jpeg",
                    help="帧格式，默认 jpeg（单帧 50ms/93KB，比 PNG 快 15 倍）。"
                         "注意：质量门禁的 MOTION_ALIVE 逐帧比对用 JPEG 时会转交 ffmpeg 解码")
    ap.add_argument("--quality", type=int, default=92, help="JPEG 帧质量，默认 92")
    ap.add_argument("--dry-run", action="store_true",
                    help="每个镜头只渲染 1 帧，用于快速验证模板与视觉描述")
    ap.add_argument("--browser-path", default=os.environ.get("HPV_BROWSER_PATH", ""),
                    help="自定义 Chromium/Chrome 可执行文件路径（默认用 Playwright 自带浏览器）")
    args = ap.parse_args()

    if args.fps <= 0 or args.fps > 120:
        raise SystemExit("--fps 需在 1~120 之间")
    w, h = PRESETS[args.preset]
    if args.width:
        w = args.width
    if args.height:
        h = args.height
    if args.width or args.height:
        if w <= 0 or h <= 0:
            raise SystemExit("宽高必须为正数")
        # 只给一边时按另一边推导，避免比例意外
        if not args.width:
            w = int(h * PRESETS[args.preset][0] / PRESETS[args.preset][1])
        if not args.height:
            h = int(w * PRESETS[args.preset][1] / PRESETS[args.preset][0])
    args.width, args.height = w, h

    if args.workers <= 0:
        cpu = os.cpu_count() or 4
        args.workers = max(2, min(6, cpu // 2))

    with open(args.timeline, encoding="utf-8") as f:
        timeline = json.load(f)
    scenes = timeline.get("scenes") or []
    if not scenes:
        raise SystemExit("时间轴里没有场景：%s" % args.timeline)

    visuals = load_visuals(args.visuals, len(scenes))
    tasks, total = build_tasks(timeline, visuals, args.fps, args.format)
    if args.dry_run:
        # 每个镜头留一帧，取镜头 45% 处（t=0 时入场动画还没开始，全是空白）
        seen = set()
        dry = []
        for t in tasks:
            if t["scene"] not in seen:
                seen.add(t["scene"])
                t["t"] = round(float(t["vis"]["duration"]) * 0.45, 4)
                dry.append(t)
        # 重新编号，保证帧号连续
        for i, t in enumerate(dry):
            t["frame"] = i
        tasks, total = dry, len(dry)
        print("  dry-run：每个镜头 1 帧（取 45%% 处），共 %d 帧" % total)

    os.makedirs(args.out, exist_ok=True)
    print("  渲染 %d 帧（%dx%d @ %dfps，%s，%d workers，主题 %s）"
          % (total, args.width, args.height, args.fps, args.format.upper(),
             args.workers, args.theme))

    stats, elapsed = asyncio.run(run_async(args, tasks, total))

    manifest = {
        "frames": total,
        "format": args.format,
        "fps": args.fps,
        "width": args.width,
        "height": args.height,
        "theme": args.theme,
        "template": os.path.abspath(args.template),
        "timeline": os.path.abspath(args.timeline),
        "duration": round(total / float(args.fps), 3),
        "retries": stats["retries"],
        "render_seconds": round(elapsed, 1),
    }
    with open(os.path.join(args.out, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print()
    print("  完成 %d 帧，耗时 %.1fs，重试 %d 次" % (total, elapsed, stats["retries"]))
    print("  帧目录：%s（manifest.json 已写入）" % args.out)
    print("  下一步：python3 scripts/assemble_video.py --frames %s --audio master.wav --out final.mp4" % args.out)


if __name__ == "__main__":
    main()
