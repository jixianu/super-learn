#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import fcntl
import hashlib
import os
import selectors
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path


def slugify(value: str, fallback: str = "video-source") -> str:
    value = value.strip().lower()
    value = re.sub(r"[^\w\s-]", "", value, flags=re.UNICODE)
    value = re.sub(r"[-\s]+", "-", value).strip("-_")
    return value or fallback


def yaml_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def collapse_blank_lines(text: str) -> str:
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + "\n"


def sanitize_path_component(value: str, fallback: str = "video-source") -> str:
    value = re.sub(r'[\\/:*?"<>|]', " ", value).strip()
    value = re.sub(r"\s+", " ", value)
    value = value.rstrip(".")
    return value or fallback


def detect_platform(url: str) -> str:
    lowered = url.lower()
    if "youtube.com" in lowered or "youtu.be" in lowered:
        return "youtube"
    if "bilibili.com" in lowered or "b23.tv" in lowered:
        return "bilibili"
    return "video"


def read_text(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8", "utf-8-sig", "gb18030", "big5", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def split_transcript_sentences(text: str) -> list[str]:
    segments: list[str] = []
    for raw_line in text.splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not line:
            continue
        parts = re.split(r"(?<=[。！？.!?])\s+|[;；]+", line)
        for part in parts:
            item = part.strip(" ,，。")
            if item:
                segments.append(item)
    if segments:
        return segments

    cleaned = re.sub(r"\s+", " ", text).strip()
    if not cleaned:
        return []
    return [chunk.strip(" ,，。") for chunk in re.split(r"(?<=[。！？.!?])\s+", cleaned) if chunk.strip()]


def sentence_score(line: str) -> int:
    score = 0
    length = len(line)
    if 18 <= length <= 120:
        score += 4
    elif length <= 10:
        score -= 5
    elif length > 180:
        score -= 2

    low_signal_patterns = (
        "大家好",
        "我是",
        "欢迎",
        "点赞",
        "订阅",
        "关注",
        "这一期",
        "今天的视频",
        "本期视频",
        "如果喜欢",
        "记得",
        "频道",
    )
    if any(pattern in line for pattern in low_signal_patterns):
        score -= 6

    if line.endswith(("朋友", "大家", "视频分享")):
        score -= 4

    high_signal_patterns = (
        "可以",
        "能够",
        "支持",
        "通过",
        "自动",
        "工作流",
        "代码",
        "审核",
        "任务",
        "集成",
        "触发",
        "Github",
        "Bilibili",
        "哔哩哔哩",
        "视频",
        "功能",
        "PR",
        "合并请求",
    )
    for pattern in high_signal_patterns:
        if pattern in line:
            score += 2

    if any(char.isdigit() for char in line):
        score -= 1
    if any(ch in line for ch in "，。：；"):
        score += 1
    if re.search(r"[A-Za-z]{3,}", line):
        score += 1

    return score


def summarize_transcript(text: str, limit: int = 5) -> list[str]:
    candidates = split_transcript_sentences(text)
    ranked = sorted(
        ((sentence_score(line), index, line) for index, line in enumerate(candidates)),
        key=lambda item: (-item[0], item[1]),
    )
    summary: list[str] = []
    seen: set[str] = set()
    for score, _, line in ranked:
        if score < 0:
            continue
        normalized = re.sub(r"\s+", " ", line)
        if normalized in seen:
            continue
        seen.add(normalized)
        summary.append(line)
        if len(summary) >= limit:
            break

    if summary:
        return [line[:137].rstrip() + "..." if len(line) > 140 else line for line in summary]

    fallback = candidates[:limit]
    return [line[:137].rstrip() + "..." if len(line) > 140 else line for line in fallback]


def infer_related_pages(title: str, source_url: str, transcript_text: str) -> list[str]:
    text = f"{title}\n{source_url}\n{transcript_text}".lower()
    related: list[str] = []

    def add(page: str) -> None:
        if page not in related:
            related.append(page)

    keyword_map = {
        "AI Coding": ["codex", "claude", "代码", "pr", "github", "审核", "review", "routine", "前端"],
        "Agent": ["agent", "代理", "codex", "claude", "automation", "自动化", "routine"],
        "MCP": ["mcp"],
        "提示工程": ["prompt", "提示词", "提示工程"],
        "个人 AI 知识库": ["wiki", "知识库", "obsidian"],
        "Wiki-first Knowledge Base": ["wiki-first", "知识库", "wiki"],
        "RAG": ["rag", "检索"],
        "AI 应用与落地案例": ["应用", "落地", "case", "案例", "实践"],
    }

    for page, keywords in keyword_map.items():
        if any(keyword in text for keyword in keywords):
            add(page)

    return related


def default_output_root(repo_root: Path, platform: str) -> Path:
    if platform == "youtube":
        return repo_root / "raw" / "youtube"
    if platform == "bilibili":
        return repo_root / "raw" / "bilibili"
    return repo_root / "raw" / "video"


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    index = 2
    while True:
        candidate = path.with_name(f"{path.name} {index}")
        if not candidate.exists():
            return candidate
        index += 1


def cleanup_run_dir(run_dir: Path, keep_files: set[str], keep_prefixes: tuple[str, ...] = ()) -> None:
    for path in run_dir.iterdir():
        if not path.is_file():
            continue
        if path.name in keep_files:
            continue
        if any(path.name.startswith(prefix) for prefix in keep_prefixes):
            continue
        path.unlink()


def move_work_dir(work_dir: Path, final_root: Path, title: str) -> Path:
    final_root.mkdir(parents=True, exist_ok=True)
    final_dir = unique_path(final_root / sanitize_path_component(title, fallback=work_dir.name))
    shutil.move(str(work_dir), str(final_dir))
    return final_dir


def lock_path(repo_root: Path, key: str) -> Path:
    lock_dir = repo_root / "tmp" / ".locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return lock_dir / f"video-{digest}.lock"


def run_and_stream(cmd: list[str], env: dict[str, str]) -> tuple[int, str]:
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
        env=env,
    )
    selector = selectors.DefaultSelector()
    assert process.stdout is not None
    assert process.stderr is not None
    selector.register(process.stdout, selectors.EVENT_READ, data=("stdout", sys.stdout.buffer))
    selector.register(process.stderr, selectors.EVENT_READ, data=("stderr", sys.stderr.buffer))
    stdout_chunks: list[bytes] = []

    try:
        while selector.get_map():
            for key, _ in selector.select():
                stream_name, target = key.data
                data = os.read(key.fileobj.fileno(), 4096)
                if not data:
                    selector.unregister(key.fileobj)
                    continue
                target.write(data)
                target.flush()
                if stream_name == "stdout":
                    stdout_chunks.append(data)
    finally:
        selector.close()
        process.wait()

    stdout_text = b"".join(stdout_chunks).decode("utf-8", errors="replace")
    return process.returncode, stdout_text


def write_transcript_markdown(
    title: str,
    source_url: str,
    transcript_text: str,
    output_path: Path,
    segment_texts: list[str] | None = None,
) -> None:
    content = [
        f"# {title}",
        "",
        f"- Source: <{source_url}>" if source_url else "- Source: ",
        "",
        "## Transcript",
        "",
    ]
    if segment_texts:
        for idx, segment_text in enumerate(segment_texts, start=1):
            content.extend(
                [
                    f"### Segment {idx}",
                    "",
                    segment_text.strip(),
                    "",
                ]
            )
    else:
        content.extend([transcript_text.strip(), ""])
    output_path.write_text("\n".join(content), encoding="utf-8")


def build_source_note(
    title: str,
    source_url: str,
    transcript_relpath: str,
    run_dir_relpath: str,
    transcript_text: str,
    metadata: dict,
    related_pages: list[str],
    platform: str,
) -> str:
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    bullets = summarize_transcript(transcript_text)
    core_points = "\n".join(f"- {line}" for line in bullets) if bullets else "- 待补充"

    facts = [
        f"- 转写目录：[{run_dir_relpath}]({run_dir_relpath})",
        f"- 文本稿：[{transcript_relpath}]({transcript_relpath})",
    ]
    if metadata.get("uploader"):
        facts.append(f"- 发布者：{metadata['uploader']}")
    if metadata.get("upload_date"):
        raw_date = str(metadata["upload_date"])
        if len(raw_date) == 8 and raw_date.isdigit():
            facts.append(f"- 发布日期：{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}")
    if metadata.get("duration"):
        facts.append(f"- 时长：{metadata['duration']} 秒")
    related_lines = [f"- [[{page}]]" for page in related_pages] or ["- 待补充"]

    return collapse_blank_lines(
        "\n".join(
            [
                "---",
                f"title: {yaml_quote(title)}",
                "type: source",
                "status: seed",
                f"created: {today}",
                f"updated: {today}",
                "tags:",
                "  - ai",
                f"  - {platform}",
                "sources: []",
                "---",
                "",
                f"# {title}",
                "",
                f"Source: <{source_url}>" if source_url else "Source: ",
                "",
                "## 核心观点",
                core_points,
                "",
                "## 关键事实",
                *facts,
                "",
                "## 方法 / 框架",
                "- 待补充",
                "",
                "## 可复用启发",
                "- 可基于转录稿继续沉淀到 `wiki/topics/` 或 `wiki/concepts/`。",
                "",
                "## 争议与局限",
                "- 当前内容来自自动语音转写，可能存在识别错误。",
                "- 关键判断仍需结合视频上下文与人工核对。",
                "",
                "## 关联页面",
                *related_lines,
                "",
            ]
        )
    )


def update_index_updated_date(index_text: str, today: str) -> str:
    return re.sub(r"(?m)^updated:\s*\d{4}-\d{2}-\d{2}$", f"updated: {today}", index_text, count=1)


def format_wiki_link(page_name: str, display_name: str | None = None) -> str:
    if display_name and display_name != page_name:
        return f"[[{page_name}|{display_name}]]"
    return f"[[{page_name}]]"


def append_source_to_index(index_path: Path, page_name: str, display_title: str, summary: str, today: str) -> None:
    index_text = index_path.read_text(encoding="utf-8")
    bullet = f"- {format_wiki_link(page_name, display_title)}: {summary}"
    if f"[[{page_name}]]" not in index_text and f"[[{page_name}|" not in index_text:
        marker = "\n近期新增（已清洗）：\n"
        if marker in index_text:
            index_text = index_text.replace(marker, f"{marker}{bullet}\n", 1)
        else:
            section_marker = "\n## Sources（资料摘要）\n"
            if section_marker in index_text:
                index_text = index_text.replace(section_marker, f"{section_marker}\n{bullet}\n", 1)
            else:
                index_text = index_text.rstrip() + f"\n\n## Sources\n\n{bullet}\n"
    index_text = update_index_updated_date(index_text, today)
    index_path.write_text(index_text, encoding="utf-8")


def append_ingest_log(log_path: Path, today: str, title: str, details: list[str]) -> None:
    entry_lines = [f"## [{today}] ingest | {title}", ""]
    entry_lines.extend(f"- {detail}" for detail in details)
    entry = "\n".join(entry_lines).rstrip() + "\n"

    log_text = log_path.read_text(encoding="utf-8") if log_path.exists() else "# Wiki Log\n\n"
    if entry in log_text:
        return
    if not log_text.endswith("\n"):
        log_text += "\n"
    log_text += "\n" + entry
    log_path.write_text(log_text, encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run video transcription and generate wiki-friendly artifacts.")
    parser.add_argument("--url", required=True, help="YouTube, Bilibili, or other video URL.")
    parser.add_argument("--output-root", type=Path, default=None, help="Root output directory.")
    parser.add_argument("--title", default="", help="Optional source-note title override.")
    parser.add_argument("--wiki-source-note", action="store_true", help="Also generate wiki/sources/<title>.md.")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd(), help="Repository root.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    script_path = Path(__file__).resolve().parent / "bilibili_whisper_transcribe.sh"
    work_dir: Path | None = None
    lock_file = lock_path(args.repo_root, args.url)
    platform = detect_platform(args.url)
    output_root = args.output_root or default_output_root(args.repo_root, platform)

    try:
        with lock_file.open("w", encoding="utf-8") as fp:
            fcntl.flock(fp, fcntl.LOCK_EX)
            fp.write(f"{datetime.now().isoformat()} {args.url}\n")
            fp.flush()

            env = os.environ.copy()
            env["WORK_ROOT"] = str(args.repo_root / "tmp")
            returncode, stdout_text = run_and_stream([str(script_path), args.url], env)
            if returncode != 0:
                return returncode

            stdout_lines = [line for line in stdout_text.strip().splitlines() if line.strip()]
            if not stdout_lines:
                print("error: transcription script did not report a work directory", file=sys.stderr)
                return 1
            work_dir = Path(stdout_lines[-1]).resolve()
            metadata_path = work_dir / "source.info.json"
            metadata: dict = {}
            if metadata_path.exists():
                try:
                    metadata = json.loads(read_text(metadata_path))
                except json.JSONDecodeError:
                    metadata = {}

            title = args.title or metadata.get("title") or work_dir.name
            run_dir = move_work_dir(work_dir, output_root, title)
            work_dir_name = work_dir.name
            work_dir = None
            transcript_path = run_dir / "transcript.txt"
            metadata_path = run_dir / "source.info.json"
            if not transcript_path.exists():
                print(f"error: transcript not found in {run_dir}", file=sys.stderr)
                return 1
            transcript_text = read_text(transcript_path)
            segment_texts = [
                read_text(path)
                for path in sorted(run_dir.glob("transcript.part*.txt"))
            ]
            source_url = metadata.get("webpage_url") or metadata.get("original_url") or args.url
            transcript_md_path = run_dir / "transcript.md"
            write_transcript_markdown(
                title,
                source_url,
                transcript_text,
                transcript_md_path,
                segment_texts=segment_texts or None,
            )

            print(f"Run directory: {run_dir}")
            print(f"Transcript: {transcript_md_path}")
            if metadata_path.exists():
                print(f"Metadata: {metadata_path}")

            if args.wiki_source_note:
                today = datetime.now().astimezone().strftime("%Y-%m-%d")
                wiki_sources_dir = args.repo_root / "wiki" / "sources"
                wiki_sources_dir.mkdir(parents=True, exist_ok=True)
                source_note_name = sanitize_path_component(title, fallback=work_dir_name)
                source_note_path = wiki_sources_dir / f"{source_note_name}.md"
                try:
                    transcript_relpath = transcript_md_path.relative_to(args.repo_root).as_posix()
                except ValueError:
                    transcript_relpath = transcript_md_path.as_posix()
                try:
                    run_dir_relpath = run_dir.relative_to(args.repo_root).as_posix()
                except ValueError:
                    run_dir_relpath = run_dir.as_posix()

                source_note = build_source_note(
                    title=title,
                    source_url=source_url,
                    transcript_relpath=transcript_relpath,
                    run_dir_relpath=run_dir_relpath,
                    transcript_text=transcript_text,
                    metadata=metadata,
                    related_pages=infer_related_pages(title, source_url, transcript_text),
                    platform=platform,
                )
                source_note_path.write_text(source_note, encoding="utf-8")
                summary_lines = summarize_transcript(transcript_text, limit=1)
                summary = summary_lines[0] if summary_lines else "自动下载并转写视频内容。"
                append_source_to_index(
                    args.repo_root / "wiki" / "index.md",
                    source_note_name,
                    title,
                    summary,
                    today,
                )
                append_ingest_log(
                    args.repo_root / "wiki" / "log.md",
                    today,
                    title,
                    [
                        f"使用 `video-wiki-ingest` 下载并转写内容到 `{run_dir_relpath}`。",
                        f"生成资料摘要页 `wiki/sources/{source_note_name}.md`。",
                        "长音频时按分段 wav 逐段转写，再合并为 `transcript.md`。",
                        "自动更新 `wiki/index.md` 与 `wiki/log.md`。",
                    ],
                )
                print(f"Source note: {source_note_path}")

            cleanup_run_dir(run_dir, keep_files={"source.info.json"}, keep_prefixes=("transcript",))

            return 0
    finally:
        if work_dir is not None and work_dir.exists():
            shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
