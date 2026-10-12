---
name: high-end-product-video
slug: high-end-product-video
version: 2.1.0
displayName: 高端产品宣传视频 · 声画同步短视频流水线
summary: 用旁白真实波形反推画面时长，根除抢拍与空转。一条脚本化流水线从旁白直达成片：确定性帧渲染、侧链闪避混音、BS.1770-4 响度母带（-16 LUFS / -1.0 dBTP）、九项可执行质量门禁，交付前硬性失败即阻断。
description: 科技产品宣传短视频制作工作流。以声音时钟逆推为核心，用 TTS 实际波形反推画面时长，消除抢拍与空转；含动量连续性视觉规范、确定性帧渲染、侧链闪避混音、BS.1770-4 响度母带、可机械执行的质量门禁。当需要制作产品宣传片、发布会特性视频、需要声画精确对齐的短视频时使用。
tags: [ai, video, product-video, tts, loudness, lufs, bs1770, playwright, ffmpeg, skill, agent]
license: MIT
---

# 科技产品宣传短视频工作流

核心主张只有一条：**画面时长不能拍脑袋定，必须由旁白的真实波形反推。**

绝大多数短视频的抢拍、空转、人声被 BGM 盖住，根源都是顺序反了——先定画面再拿音频硬凑。本工作流把顺序倒过来，并用四个脚本把整条流水线固化下来，每个环节都有可度量的质量基线。

---

## 一、五个阶段与对应脚本

| 阶段 | 做什么 | 用什么 |
|---|---|---|
| 1. 时间轴 | 生成/提取旁白，用真实波形算出每个镜头的画面时长 | `scripts/tts_timeline.py` |
| 2. 渲染 | 按时间轴驱动 HTML 模板逐帧截图，动量规范全部内置 | `scripts/render_frames.py`（需 Playwright）|
| 3. 混音 | 人声铺轨 + BGM 侧链闪避 + 响度母带处理 | `scripts/audio_engine.py` |
| 4. 合成 | 帧序列 + 母带 → H.264 成片 | `scripts/assemble_video.py`（需 FFmpeg）|
| 5. 门禁 | 交付前拦截硬伤，全绿才许交付 | `scripts/verify_quality.py` |

`media_lib.py`（WAV/PNG/灰度解码等共享原语）和 `loudness.py`（BS.1770-4 响度与真峰值）是被依赖的库，不单独运行。

---

## 二、声音时钟逆推（本工作流的核心）

### 为什么

先定画面时长再塞音频，必然出现两种翻车：话没说完画面已经切走，或者画面还在播但话已经说完空转两秒。

正确顺序：生成旁白 → 提取真实波形起止点 → 反推画面时长。

$$T_{\text{scene}} = T_{\text{voice}} + T_{\text{pre-padding}} + T_{\text{post-padding}}$$

- $T_{\text{pre-padding}} = 0.6\text{s}$：让观众看清标题和 UI，BGM 铺底起手，**根除抢跑**
- $T_{\text{post-padding}} = 0.4\text{s}$：留出纯净缝隙触发转场音效，不压迫人声

### 怎么做

```bash
# 1. 准备旁白文本，每行一句（examples/narration.txt 有样例）
# 2. 生成音频并算时间轴（离线模式，不需要任何 API Key）
python3 scripts/tts_timeline.py examples/narration.txt --mode offline --out timeline.json

# 3. 看一眼时间轴对不对
cat timeline.json
```

输出时间轴包含每个镜头的 `voice_start/voice_end/voice_duration`（VAD 提取）与 `scene_start/scene_end/scene_duration`（反推结果）。渲染时每个镜头按 `scene_duration` 截帧，人声在镜头开始 0.6 秒后进入。

### 语音引擎

| 模式 | 说明 | 依赖 |
|---|---|---|
| `offline`（默认）| 本地合成占位音频（机器人嗡鸣，波形时长与字数成正比），用于跑通流水线与测试 | 无 |
| `edge` | edge-tts 高质量语音 | `pip install edge-tts` |
| `existing` | 用你已有的 wav 文件，只算时间轴 | 无 |

**密钥一律从环境变量读取，脚本里不出现任何明文 key。** 用 `existing` 模式接你自己的 TTS 产物时，也不需要把密钥交给本脚本。

```bash
python3 scripts/tts_timeline.py narration.txt --mode edge --voice zh-CN-YunxiNeural
python3 scripts/tts_timeline.py narration.txt --mode existing --audio-dir ./my_voice
```

---

## 三、渲染：确定性帧序列

```bash
python3 scripts/render_frames.py \
  --timeline timeline.json \
  --template templates/scenes.html \
  --visuals examples/visuals.json \
  --out frames --fps 30
```

三个设计要点：

1. **确定性优先**。每帧时间戳就是 `t = n/fps`，模板的 `__render(t)` 是纯函数——同样 t 在任何机器上画出同一帧。不依赖真实时钟，不会掉帧，可复现。
2. **契约简单**。模板从 URL 参数拿场景数据，暴露 `window.__render(t)`，渲染器逐帧调用。改造模板只需要守这个契约（详见 `references/design-system.md` 第八节）。
3. **自适应并发**。多个 Page 并行，按镜头聚合任务摊薄导航开销；白屏（截图体积过小）自动重试。

默认模板内置四种镜头类型（`--visuals` 指定）：`title` 标题卡、`feature` 特性卡、`metric` 数据卡、`outro` 落版。主题 `--theme editorial|dark`，画幅 `--preset landscape|vertical|square|wide` 或自定义宽高。

```bash
# 快速验证模板与视觉描述：每个镜头只渲 1 帧
python3 scripts/render_frames.py ... --dry-run
# 竖屏 9:16
python3 scripts/render_frames.py ... --preset vertical
```

**帧格式默认 JPEG q92**：单帧 50ms/93KB，比 PNG 快 15 倍。成片是 4500kbps H.264，JPEG q92 中间帧对它完全透明。要像素级无损就加 `--format png`（慢 15 倍，门禁的死帧检查无需转码）。

---

## 四、视觉规范：动量连续性

画面任何一帧都不允许完全静止。主体进场用 Apple 标准流体曲线 `easeOutExpo`（0.9s），停稳后进入漂移巡航态。**原始公式的 Scale 项会无限增长，必须改成有界振荡**，而且要叠双频率：

```javascript
// 错误：t 增大时 scale 会一直变大，长镜头下元素会飞出画面
// scale = baseScale + t * 0.012

// 正确：双不可通约频率叠加 + clamp。单正弦在极值点速度为零，
// 逐帧检测会抓到"几乎静止"的帧；叠一个频率后合速度永不为零。
const drift  = Math.sin(t * 0.8) * 0.013 + Math.sin(t * 1.37) * 0.008;
const driftY = clamp(Math.cos(t * 1.2) * 5 + Math.sin(t * 0.93) * 4, -10, 10);
```

其余规范（景深视差、字体排版、质感三件套、性能基线）见 `references/design-system.md`，默认模板全部内置，改造模板时对照修改。

---

## 五、声学：侧链闪避混音 + 响度母带

```bash
python3 scripts/audio_engine.py \
  --timeline timeline.json \
  --bgm ./bgm.wav \
  --out master.wav
```

脚本做的事：

1. 按时间轴把各段人声铺到正确位置（镜头起点 + 0.6s）
2. 计算人声能量包络，生成 BGM 闪避增益曲线：人声期间衰减到 **18%**，停顿后 **250ms** 内平滑恢复到 **35%**
3. 母带处理（默认 `--lufs-mode lufs`）：K 加权测量集成响度 → 静态增益归一到 **-16 LUFS** → 前瞻限幅把真峰值压到 **-1.0 dBTP** → 复测补偿，最多 4 轮收敛

响度标准（全文统一，不再前后打架）：

- 集成响度 **-16 LUFS**（±1 LU 容差）
- 真实峰值上限 **-1.0 dBTP**（4x 过采样测量）
- 采样率 **48kHz**

要旧行为就加 `--lufs-mode peak --target -1.0`（峰值归一）。

### 关于 BGM

脚本不自带 BGM，也不联网下载。你需要自备一个无版权风险的音频文件。没有 BGM 时可以加 `--no-bgm`，只输出人声母带。

---

## 六、合成

```bash
python3 scripts/assemble_video.py --frames frames --audio master.wav --out final.mp4 --verify
```

编码参数：`libx264 -crf 16 -maxrate 4500k -bufsize 9000k -preset slow -pix_fmt yuv420p -profile:v high -level 4.1 -movflags +faststart`，音频 `aac 192k 48kHz`。`-t` 以帧序列时长为准。

`--verify` 会把成片音轨解码回 WAV，跑一遍 BS.1770-4 复测（AAC 通常让响度掉 0.1~0.3 LU，在容差内算达标）。**不要跳过**：它抓的是「母带达标、成片却漂了」的静默失败。

---

## 七、质量门禁

交付前必须跑，任一硬性检查失败即阻断：

```bash
python3 scripts/verify_quality.py \
  --timeline timeline.json --audio master.wav \
  --frames frames --fps 30 --lufs -16 --strict
```

| 检查项 | 判定标准 | 级别 |
|---|---|---|
| `AUDIO_DECODABLE` | WAV 可读、时长 > 0、采样率/位宽合法 | 硬 |
| `TRUE_PEAK_CEILING` | 真峰值（4x 过采样）<= -1.0 dBTP | 硬 |
| `LOUDNESS_TARGET` | 集成响度（门控）命中目标 ±1 LU | 硬（需 `--lufs`）|
| `NO_SPEECH_OVERLAP` | 相邻人声间隙 >= 0.3s | 硬 |
| `TIMELINE_CONTINUOUS` | 时间轴无断档、无重叠、总时长自洽 | 硬 |
| `NO_TOFU_CHARS` | 字幕不含乱码方块字符 | 硬（需 `--subtitle`）|
| `FRAME_SEQUENCE` | 帧数/编号连续性/尺寸一致 | 硬（需 `--frames`）|
| `MOTION_ALIVE` | 两帧跨度灰度差 >= 阈值（排除转场窗口）| 软（需 `--frames`）|
| `AUDIO_COVERS_TIMELINE` | 母带不短于时间轴总时长 | 软 |

**这些是可机械执行的检查，替代原版里「严禁偷懒」「绝对责任」这类口号。** 约束写进脚本才会真正生效，写在文档里只会被跳过。

### 已修正的三个原版缺陷

1. `NO_TOFU_CHARS` 原版写作「不得含 '▢', '', '?'」，其中**空字符串 `''` 会让检查 100% 误报**。现在用明确的乱码字符集：`\uFFFD`（替换字符）、`\u25A1`、`\u25A0`（方块）等，不再用空串。
2. 原版同时出现「True Peak -1.0 dBFS」和「门禁 Max Peak <= -0.5 dB」、「4.5+ Mbps」和「>= 3500 kbps」两组互相矛盾的数字。现在全文统一为 **-1.0 dBTP** 和 **4500 kbps**。
3. 原版只有口号没有门禁。现在九项检查全部可执行，且 `MOTION_ALIVE` 用两帧跨度比对（单帧比对会把振荡极值点的正常微动误判成死帧，实测 356 → 0 误报）。

---

## 八、执行 SOP

1. **对齐方向** — 问清受众、平台、画幅、核心记忆点，再动工
2. **写旁白** — 按「大白话拆解」写，别堆术语；每行一句
3. **跑时间轴** — `tts_timeline.py`，拿到真实波形时长
4. **配视觉** — 写 `visuals.json`（每镜头的类型与文案），`--dry-run` 验证
5. **渲染帧序列** — `render_frames.py`，JPEG 默认
6. **混音** — `audio_engine.py` 生成 -16 LUFS / -1.0 dBTP 母带
7. **合成** — `assemble_video.py --verify`
8. **过门禁** — `verify_quality.py --strict`，全绿才交付
9. **交付** — 成片；如有可能附上交互演示页，形成「看视频 → 玩交互页 → 转化」闭环

---

## 九、能力边界（说清楚，别吹）

- 本工作流**不生成视频素材**：画面来自你写的 HTML 模板 + Playwright 截帧，不是文生视频。要 AI 生成画面请另配工具，把结果当帧序列喂给 `assemble_video.py`
- 标称能力是**声画精确对齐 + 可度量交付基线**（-16 LUFS / -1.0 dBTP / 4500kbps / 无死帧），不是「Apple 级」「广播级」。Playwright 截帧很难稳定跑满 60fps，离广播级有距离
- 脚本不联网、不内置密钥、不自带 BGM 和字体；唯一的外部依赖是 Playwright（渲染）与 FFmpeg（合成），都需要你自己装
