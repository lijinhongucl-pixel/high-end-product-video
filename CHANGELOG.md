# Changelog

本项目遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [2.2.0] - 2026-10-13

### 变更（通用化）

- 适用面从「科技产品宣传」扩展到**一切旁白驱动的短视频**：产品宣传（任意品类）、知识讲解、教程演示、品牌故事、活动回顾、个人 IP 口播。新增「开工前先对齐四件事」与「镜头类型与题材搭配」两节，按题材给旁白结构与镜头搭配建议
- `examples/` 样例从星图 AstraFlow 换成精品咖啡品牌宣传片（`SLOW COFFEE`），证明流程不限题材
- frontmatter 的 `displayName`/`summary`/`description` 重写，触发词扩充（做宣传片/产品视频/讲解视频/教程视频/短视频制作/product video/promotional video），pyproject 的 description 与 keywords 同步
- TTS 接入方式通用化：除 offline/edge/existing 三模式外，明确 `--tts-cmd` 可接任意第三方 TTS（MiniMax/Azure/OpenAI），密钥只存在于用户自己的环境变量

### 修复

- 安装目录（`~/.workbuddy/skills/high-end-product-video`）里的旧版 SKILL.md 仍带着硬编码 MiniMax Key 与无界动量公式，本版整体替换清除

## [2.1.0] - 2026-10-13

从企业微信流传的 SKILL 草稿重构为可运行的 GitHub 项目。方法论保留，工程全部重写。

### 新增

- `scripts/render_frames.py`：确定性帧渲染器。`t = n/fps` 纯函数渲染，Playwright 自适应并发、白屏重试、`--dry-run` 快速验证。默认 JPEG q92 中间帧（单帧 50ms，比 PNG 快 15 倍）
- `scripts/assemble_video.py`：FFmpeg 合成。capped CRF（crf 16 + maxrate 4500k）、yuv420p/high/4.1、faststart、AAC 192k；`--verify` 解码成片音轨复测响度
- `scripts/loudness.py`：ITU-R BS.1770-4 实现。K 加权（任意采样率，48kHz 与标准公布系数误差 < 1e-12）、门控集成响度、4x 过采样真峰值、前瞻限幅器（5ms 延迟 + 余量搜索）
- `scripts/media_lib.py`：共享原语。WAV 读写、RMS 包络、VAD、重采样、纯标准库 PNG 解码器、ffmpeg 流式帧解码
- `templates/scenes.html`：默认场景模板。title/feature/metric/outro 四类镜头，editorial/dark 双主题，动量连续性（双频有界振荡）、景深视差、胶片噪波、暗角全部内置
- `references/design-system.md`、`references/mixing-notes.md`：模板改造与混音实现的依据文档
- `tests/`：27 项回归，含 ITU 系数对照、参考响度标定、门禁正反例、死帧检测

### 修复（原版缺陷）

- **P0 安全**：删除硬编码的 MiniMax API Key。密钥一律从环境变量读取；注意删除代码不等于吊销，已泄露的 key 必须在控制台吊销
- 门禁指标前后矛盾：「True Peak -1.0 dBFS」与「Max Peak <= -0.5 dB」、「4.5+ Mbps」与「>= 3500 kbps」两组数字打架，统一为 **-1.0 dBTP** 与 **4500 kbps**
- `NO_TOFU_CHARS` 原写作「不得含 '▢', '', '?'」，空字符串 `''` 让检查 100% 误报；改为明确字符集（U+FFFD、U+25A1、U+25A0 等）
- 动量公式 `scale = baseScale + t * 0.012` 无界，长镜头元素飞出画面；改为双频有界振荡 + clamp
- 「严禁偷懒」类口号替换为九项可机械执行的门禁检查

### 变更

- `tts_timeline.py`：三模式（offline/edge/existing），`--mode` 参数化；输出字段扩充（voice_start/end、scene_start/end）
- `audio_engine.py`：默认改为响度母带（-16 LUFS / -1.0 dBTP），旧峰值归一保留为 `--lufs-mode peak`；输出采样率 48kHz
- `verify_quality.py`：新增 `TRUE_PEAK_CEILING`、`LOUDNESS_TARGET`、`FRAME_SEQUENCE`；`MOTION_ALIVE` 改两帧跨度比对（单帧比对会把振荡极值点的正常微动误判成死帧）

## [2.0.0] - 2026-10-11

原 SKILL 草稿的文档版：删除硬编码 key、统一指标、修空字符串 bug、口号改为检查表。无任何可执行脚本。
