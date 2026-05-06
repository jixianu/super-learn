#!/usr/bin/env python3

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urldefrag, urlparse

try:
    from pypdf import PdfReader  # type: ignore
except ImportError:  # pragma: no cover - runtime dependency check
    PdfReader = None  # type: ignore[assignment]


@dataclass
class PageResult:
    page_no: int
    mode: str
    text: str


def sanitize_path_component(text: str, fallback: str = "pdf-source") -> str:
    cleaned = "".join(
        ch if ch.isalnum() or ch in {" ", "-", "_", ".", "(", ")", "【", "】", "·"} else "_"
        for ch in text.strip()
    )
    cleaned = " ".join(cleaned.split()).strip(" ._")
    return cleaned or fallback


def normalize_note_title(text: str) -> str:
    if not text:
        return "PDF 资料"
    title = text.replace("\u3000", " ").strip()
    while title.startswith("【") and "】" in title:
        title = title.split("】", 1)[1].strip(" -_")
    return title or "PDF 资料"


def path_text(path: Path, repo_root: Path) -> str:
    try:
        return path.relative_to(repo_root).as_posix()
    except ValueError:
        return path.as_posix()


def update_index_updated_date(index_text: str, today: str) -> str:
    import re

    return re.sub(r"(?m)^updated:\s*\d{4}-\d{2}-\d{2}$", f"updated: {today}", index_text, count=1)


def append_source_to_index(index_path: Path, title: str, summary: str, today: str) -> None:
    index_text = index_path.read_text(encoding="utf-8")
    bullet = f"- [[{title}]]: {summary}"
    if f"[[{title}]]" not in index_text:
        marker = "\n## Analyses\n"
        if marker in index_text:
            index_text = index_text.replace(marker, f"\n{bullet}\n{marker}", 1)
        else:
            index_text = index_text.rstrip() + f"\n\n## Sources\n\n{bullet}\n"
    index_text = update_index_updated_date(index_text, today)
    index_path.write_text(index_text, encoding="utf-8")


def append_ingest_log(log_path: Path, today: str, title: str, details: list[str]) -> None:
    entry_lines = [f"## [{today}] ingest | {title}", ""]
    entry_lines.extend(f"- {detail}" for detail in details)
    entry = "\n".join(entry_lines).rstrip() + "\n"
    log_text = log_path.read_text(encoding="utf-8") if log_path.exists() else "# Wiki Log\n\n"
    if f"## [{today}] ingest | {title}" in log_text:
        return
    if not log_text.endswith("\n"):
        log_text += "\n"
    log_text += "\n" + entry
    log_path.write_text(log_text, encoding="utf-8")


def download_pdf(url: str, pdf_path: Path, repo_root: Path) -> None:
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["curl", "-L", "--fail", "--silent", "--show-error", url, "-o", str(pdf_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=repo_root,
    )
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "PDF 下载失败"
        raise RuntimeError(message)


def validate_pdf_magic(pdf_path: Path) -> None:
    with pdf_path.open("rb") as fp:
        head = fp.read(8)
    if not head.startswith(b"%PDF-"):
        raise RuntimeError("输入文件看起来不是有效 PDF（magic bytes 不匹配）")


def copy_file_stream(src: Path, dst: Path, chunk_size: int = 1024 * 1024) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    with src.open("rb") as src_fp, dst.open("wb") as dst_fp:
        while True:
            chunk = src_fp.read(chunk_size)
            if not chunk:
                break
            dst_fp.write(chunk)


def extract_page_text(reader: PdfReader, page_no: int) -> str:
    try:
        page = reader.pages[page_no - 1]
        text = page.extract_text() or ""
    except Exception:
        return ""
    lines = [line.strip() for line in text.replace("\r", "\n").split("\n")]
    return "\n".join(line for line in lines if line).strip()


def process_pdf(
    pdf_path: Path,
    repo_root: Path,
    title: str,
    url: str,
    wiki_source_note: bool,
) -> tuple[Path, Path, Path]:
    validate_pdf_magic(pdf_path)
    if PdfReader is None:
        raise RuntimeError("缺少 pypdf 依赖，请先安装 pypdf")

    reader = PdfReader(str(pdf_path))
    if getattr(reader, "is_encrypted", False):
        try:
            reader.decrypt("")
        except Exception:
            pass

    page_results: list[PageResult] = []
    text_count = 0
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    output_dir = pdf_path.parent
    transcript_md = output_dir / "transcript.md"
    transcript_txt = output_dir / "transcript.txt"
    index_path = output_dir / "index.md"

    total_pages = len(reader.pages)
    for page_no in range(1, total_pages + 1):
        text = extract_page_text(reader, page_no)
        if not text:
            raise RuntimeError(f"当前 pdf-skill 仅支持文字版 PDF，第 {page_no} 页没有可抽取的文本层")
        page_results.append(PageResult(page_no=page_no, mode="pypdf", text=text))
        text_count += 1

    transcript_lines: list[str] = [
        f"# {title}",
        "",
        f"- Source: <{url}>",
                f"- PDF: [{pdf_path.name}]({pdf_path.name})",
                f"- Extraction: pypdf only",
                f"- Pages: {total_pages}",
                f"- Text pages: {text_count}",
        "",
        "## Transcript",
        "",
    ]
    transcript_txt_lines: list[str] = []

    for page in page_results:
        transcript_lines.extend(
            [
                f"## 第 {page.page_no:04d} 页 [{page.mode}]",
                "",
                page.text or "- （空白）",
                "",
            ]
        )
        transcript_txt_lines.append(f"[第 {page.page_no:04d} 页 | {page.mode}]")
        transcript_txt_lines.append(page.text or "")
        transcript_txt_lines.append("")

    transcript_md.write_text("\n".join(transcript_lines).rstrip() + "\n", encoding="utf-8")
    transcript_txt.write_text("\n".join(transcript_txt_lines).rstrip() + "\n", encoding="utf-8")

    raw_index = "\n".join(
        [
            f"# {title}",
            "",
            f"- 来源：<{url}>",
            f"- 文件：[source.pdf](source.pdf)",
            f"- 正文：[transcript.md](transcript.md)",
            f"- 解析方式：pypdf only",
            f"- 总页数：{total_pages}",
            f"- 文本页：{text_count}",
            "",
            "## 说明",
            "",
            "- 文本层优先使用 pypdf。",
            "- 当前版本仅支持文字版 PDF，不做 OCR 补页。",
            "- 相关页面：`wiki/sources/` 中的 source note。",
            "",
        ]
    )
    index_path.write_text(raw_index, encoding="utf-8")

    source_note_path = repo_root / "wiki" / "sources" / f"{title}.md"
    source_note_path.parent.mkdir(parents=True, exist_ok=True)
    if wiki_source_note:
        raw_relpath = path_text(index_path, repo_root)
        pdf_relpath = path_text(pdf_path, repo_root)
        transcript_relpath = path_text(transcript_md, repo_root)
        source_note = "\n".join(
            [
                "---",
                f'title: "{title}"',
                "type: source",
                "status: seed",
                f"created: {today}",
                f"updated: {today}",
                "tags:",
                "  - ai",
                "  - pdf",
                "sources: []",
                "---",
                "",
                f"# {title}",
                "",
                f"Source: <{url}>",
                "",
                "## 核心观点",
                "- 本资料已完成 PDF 正文抽取，详见 `transcript.md`。",
                "- 当前版本仅支持文字版 PDF，不做 OCR 补页。",
                "",
                "## 关键事实",
                f"- 资料目录：[{raw_relpath}]({raw_relpath})",
                f"- PDF 文件：[{pdf_relpath}]({pdf_relpath})",
                f"- 正文转写：[{transcript_relpath}]({transcript_relpath})",
                f"- 总页数：{total_pages}",
                f"- 文本页：{text_count}",
                "",
                "## 方法 / 框架",
                "- 文本层优先走 pypdf。",
                "- 当前版本不处理扫描版或图片版 PDF。",
                "",
                "## 可复用启发",
                "- 同一份 PDF 可以同时保留原件、转写和知识库摘要，方便追溯。",
                "",
                "## 争议与局限",
                "- 当前摘要是机械式结构化记录，还没有做语义压缩。",
                "",
                "## 关联页面",
                "- 待补充",
                "",
            ]
        )
        source_note_path.write_text(source_note, encoding="utf-8")
        append_source_to_index(repo_root / "wiki" / "index.md", title, "已完成 PDF 抽取，见 transcript.md。", today)
        append_ingest_log(
            repo_root / "wiki" / "log.md",
            today,
            title,
            [
                f"下载/解析 PDF 并保存到 `{pdf_relpath}`。",
                f"生成正文转写 `{transcript_relpath}`。",
                f"生成资料摘要页 `wiki/sources/{title}.md`。",
                f"抽取方式：pypdf {text_count} 页。",
                "自动更新 `wiki/index.md` 与 `wiki/log.md`。",
            ],
        )

    return index_path, transcript_md, source_note_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Convert a PDF into raw transcript and wiki source note.")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd(), help="Repository root.")
    parser.add_argument("--pdf-path", type=Path, help="Existing PDF path. If omitted, download from --url.")
    parser.add_argument("--url", default="", help="Source URL for the PDF.")
    parser.add_argument("--title", default="", help="Title for output files.")
    parser.add_argument("--output-dir", type=Path, default=None, help="Output directory when downloading.")
    parser.add_argument("--wiki-source-note", dest="wiki_source_note", action="store_true", help="Write wiki/sources note and update wiki index/log.")
    parser.add_argument(
        "--no-wiki-source-note",
        dest="wiki_source_note",
        action="store_false",
        help="Do not write wiki/sources note or update wiki index/log.",
    )
    parser.set_defaults(wiki_source_note=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo_root = args.repo_root.resolve()
    url = args.url.strip()

    if args.pdf_path:
        pdf_path = args.pdf_path.resolve()
        title = normalize_note_title(args.title or pdf_path.stem)
        output_dir = (args.output_dir or (pdf_path.parent / sanitize_path_component(title))).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        if pdf_path.parent != output_dir:
            target_pdf = output_dir / "source.pdf"
            if not target_pdf.exists():
                copy_file_stream(pdf_path, target_pdf)
            pdf_path = target_pdf
    else:
        if not url:
            print("缺少 --pdf-path 或 --url", file=sys.stderr)
            return 2
        parsed = urlparse(urldefrag(url)[0])
        source_stem = Path(parsed.path).stem or "PDF 资料"
        title = normalize_note_title(args.title or source_stem)
        output_dir = (args.output_dir or (repo_root / "raw" / "pdf" / sanitize_path_component(title))).resolve()
        pdf_path = output_dir / "source.pdf"
        download_pdf(url, pdf_path, repo_root)

    title = normalize_note_title(args.title or output_dir.name)
    if not url:
        url = f"file://{pdf_path}"

    try:
        index_path, transcript_path, source_note_path = process_pdf(
            pdf_path=pdf_path,
            repo_root=repo_root,
            title=title,
            url=url,
            wiki_source_note=args.wiki_source_note,
        )
    except Exception as exc:  # noqa: BLE001 - explicit CLI failure surface
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"Raw index: {index_path}")
    print(f"Transcript: {transcript_path}")
    print(f"Source note: {source_note_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
