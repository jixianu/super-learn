---
name: video-wiki-ingest
description: 将 YouTube 或 Bilibili 视频下载到本地，提取音频并用 whisper-cli 转写，必要时再生成适合本仓库的 wiki 资料页。用于抓取视频 URL、保存到对应的 raw 目录，并把结果整理成转写稿和 source note。
---

# 视频入库到 Wiki

## 概览

这个 skill 用于仓库里的视频入库流程。它统一处理 YouTube 和 Bilibili：先下载媒体，统一处理音频，再进行转写，必要时写入 `wiki/sources/`。
下载和转写过程中使用仓库里的 `tmp/` 作为工作目录，完成后再把产物整理到平台对应的 `raw/` 目录。

## 流程

1. 先用 `command -v` 检查 `yt-dlp`、`ffmpeg` 和 `whisper-cli` 是否可用。
1. 使用仓库内置包装脚本下载并处理 URL。
1. 输出目录按视频平台自动落到 `raw/youtube/` 或 `raw/bilibili/`，避免和 wiki 页面混在一起。
1. 如果用户需要接入 wiki，就生成或更新对应的 `wiki/sources/` 页面，并记录入库日志。

## 主要脚本

### 仓库包装脚本

这是优先入口，负责把视频下载、转写、source note 生成和 wiki 回写串成一条链。

```bash
python3 skills/video-wiki-ingest/scripts/bilibili_to_wiki.py \
  --url "https://www.youtube.com/watch?v=..." \
  --output-root "raw/youtube" \
  --wiki-source-note
```

### 底层转写脚本

```bash
{baseDir}/scripts/bilibili_whisper_transcribe.sh <video-url> [output-dir]
```

`{baseDir}` 指包含当前 `SKILL.md` 的目录。

默认行为：
- 使用 `yt-dlp` 下载最佳可用媒体流
- 将媒体保存在仓库 `tmp/` 下的临时工作目录
- 提取为适合 Whisper 的单声道 16 kHz `wav`
- 运行 `whisper-cli`
- 长音频会先切成 40 分钟一段，再逐段转写；例如 1 小时 32 分的视频会变成 3 段
- 在 Whisper 输出旁写出 `transcript.md`

包装脚本输出：
- 在检测到的 `raw/` 根目录下生成按标题命名的目录
- `transcript.md` 以及其底层转写产物作为主要本地结果
- 可选生成 `wiki/sources/<title>.md`
- 可选更新 `wiki/index.md` 和 `wiki/log.md`

## 参数

支持的环境变量：

- `WHISPER_MODEL`：可选的模型路径覆盖，传给 `-m`
- `WHISPER_LANG`：默认 `auto`
- `WHISPER_BEST_OF`：默认 `1`，降低内存压力
- `WHISPER_BEAM_SIZE`：默认 `1`，降低内存压力
- `WHISPER_EXTRA_ARGS`：追加到 `whisper-cli` 的额外参数
- `YTDLP_EXTRA_ARGS`：追加到 `yt-dlp` 的额外参数
- `FFMPEG_EXTRA_ARGS`：追加到 `ffmpeg` 的额外参数
- `YTDLP_COOKIES_FROM_BROWSER`：浏览器 cookie 来源，例如 `chrome:Profile 1`

包装脚本参数：

- `--url`：必填的视频 URL
- `--output-root`：默认使用平台对应的 `raw/` 根目录
- `--title`：可选的 source note 标题覆盖
- `--wiki-source-note`：生成 `wiki/sources/<title>.md`
- `--repo-root`：仓库根目录，默认当前目录

## 备注

- 优先输出 `mp4`；`yt-dlp` 会自动合并视频和音轨。
- 保持平台对应的 `raw/` 根目录作为产物根目录，这样入库队列更容易检查。
- 仓库包装脚本会把中间文件保留在 `tmp/`，只有下载和转写成功后才把成品移动到最终 `raw/` 目录。
- 同一个 URL 重跑时会等待已有任务锁，不会启动第二次转写。
- 默认 whisper 解码参数刻意设得很轻（`best-of=1`、`beam-size=1`），避免大模型在 Metal 下 OOM。
- 转写运行时会根据音频时长显示实时进度条。
- 长音频会按分段 wav 逐段转写，最后合并成一个 `transcript.md`。
- 仓库包装脚本在 wiki 入库后会清理下载的媒体和音频，但保留 `transcript.md`、`transcript.txt`、`transcript.srt`、`transcript.json` 和分段转写文件。
- 启用 `--wiki-source-note` 时，脚本应在同一轮里更新 `wiki/index.md` 和 `wiki/log.md`。
- 如果 `~/.whisper/` 下没有可用模型，请显式设置 `WHISPER_MODEL`。
- 处理需要 cookie 或反爬参数的 Bilibili 页面时，把必要的 `yt-dlp` 参数通过 `YTDLP_EXTRA_ARGS` 传入。
- 如果你希望脚本根据 URL 自动选择 `raw/youtube/` 或 `raw/bilibili/`，就不要显式设置 `--output-root`。
