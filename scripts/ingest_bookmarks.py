#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import unicodedata
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urldefrag, urlparse


BOOKMARK_LINK_PATTERN = re.compile(r"- \[(?P<label>.+?)\]\((?P<url>https?://[^)\s]+)\)")
JUNK_LABEL_PATTERN = re.compile(r"已读|值得推荐的优质文章", flags=re.IGNORECASE)

THEME_BY_PART = {
    1: "提示工程",
    2: "AI 应用与落地案例",
    3: "Agent",
    4: "Agent",
    5: "AI Coding",
    6: "AI Coding",
    7: "AI Coding",
    8: "AI Coding",
    9: "RAG / MCP",
    10: "Skills / Agent / AI Coding",
}

STATUS_LABELS = {
    "success": "成功",
    "blocked": "受限",
    "error": "报错",
    "unsupported": "暂不支持",
    "pending": "待处理",
}

WECHAT_FOOTER_NOISE_PATTERNS = (
    "视频小程序赞",
    "轻点两下取消赞在看",
    "轻点两下取消在看",
)

WECHAT_HARD_BLOCK_PATTERNS = (
    "Refreshing too often",
    "环境异常",
    "参数错误",
    "该内容已被发布者删除",
)

FETCH_PARTIAL_PATTERNS = (
    "SubscribeSign in",
    "Subscribe Sign in",
    "Skip to content",
    "404: NOT_FOUND",
    "Vercel Security Checkpoint",
    "Just a moment",
)

RAW_NOISE_PATTERNS = (
    "在小说阅读器读本章",
    "在小说阅读器中沉浸阅读",
    "分享留言收藏听过",
    "预览时标签不可点",
)

GENERIC_SHELL_PATTERNS = (
    "# 微信公众平台",
    "# 404 NOT_FOUND",
    "# 403 Forbidden",
    "# File not found",
    "# 没有权限访问 - 飞书云文档",
    "# Just a moment",
    "当前仅完成 PDF 原文下载",
)

THEME_PAGE_NAMES = {
    "AI Coding": "AI Coding",
    "AI 应用与落地案例": "AI 应用与落地案例",
    "Agent": "Agent",
    "RAG / MCP": "RAG 与 MCP",
    "Skills / Agent / AI Coding": "Skills Agent AI Coding",
    "提示工程": "提示工程",
}


@dataclass
class BookmarkEntry:
    source_file: str
    part: int
    theme: str
    label: str
    url: str


@dataclass
class ManifestItem:
    source_file: str
    theme: str
    label: str
    url: str
    domain: str
    status: str
    raw_path: str = ""
    source_note: str = ""
    fetch_method: str = ""
    message: str = ""
    updated: str = ""


@dataclass
class ReviewBucket:
    title: str
    description: str
    items: list[ManifestItem]


@dataclass
class RawNoiseItem:
    path: str
    label: str
    reasons: list[str]
    theme: str = ""
    url: str = ""


@dataclass
class VerifiedState:
    bucket: str
    raw_exists: bool
    source_exists: bool
    raw_path: str
    source_note: str
    stale_marked: bool = False


def item_is_deferred_short(item: ManifestItem) -> bool:
    msg = (item.message or "").lower()
    return "defer:short" in msg or "ignore:short" in msg


def sanitize_path_component(value: str, fallback: str = "item") -> str:
    value = re.sub(r'[\\/:*?"<>|]', " ", value).strip()
    value = re.sub(r"\s+", " ", value)
    value = value.rstrip(".")
    return value or fallback


def normalize_title_for_match(value: str) -> str:
    value = unicodedata.normalize("NFKC", value)
    value = re.sub(r"\s+", " ", value).strip()
    value = re.sub(r"^(#+\s*)", "", value).strip()
    value = re.sub(r"\s+[1-9]\d*$", "", value).strip()
    value = re.sub(r"[`'\"“”‘’（）()【】\\[\\]{}<>《》:：,，。.!！?？;；·•|/\\\\_-]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip().lower()
    return value


def guess_theme_from_url(url: str) -> str:
    host = urlparse(url).netloc.lower()
    if "mp.weixin.qq.com" in host:
        return "weixin"
    if "juejin.cn" in host or "aicoding.juejin.cn" in host:
        return "juejin"
    if "github.com" in host:
        return "github"
    if any(domain in host for domain in ("anthropic.com", "openai.com", "modelcontextprotocol.io", "docs.anthropic.com")):
        return "official"
    return "web"


def build_raw_file_output(repo_root: Path, item: ManifestItem) -> Path:
    bucket = guess_theme_from_url(item.url)
    filename = sanitize_path_component(item.label, fallback="download") + ".md"
    return repo_root / "raw" / bucket / filename


def pick_nonconflicting_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    for i in range(2, 50):
        candidate = parent / f"{stem} {i}{suffix}"
        if not candidate.exists():
            return candidate
    return parent / f"{stem} {datetime.now().astimezone().strftime('%H%M%S')}{suffix}"


def is_video_item(item: ManifestItem) -> bool:
    kind = detect_kind(item.url)
    if kind == "video":
        return True
    raw_path = item.raw_path or ""
    return any(token in raw_path for token in ("/raw/bilibili/", "/raw/live/", "/raw/youtube/"))


def classify_video_bucket(item: ManifestItem) -> str:
    raw_path = item.raw_path or ""
    if "/raw/live/" in raw_path:
        return "live"
    if "/raw/bilibili/" in raw_path:
        return "bilibili"
    if "/raw/youtube/" in raw_path:
        return "youtube"
    host = urlparse(item.url).netloc.lower()
    if "bilibili.com" in host:
        return "bilibili"
    if "youtube.com" in host or "youtu.be" in host:
        return "youtube"
    return "video"


def find_best_manifest_match_for_title(
    title: str,
    manifest: dict[str, ManifestItem],
) -> ManifestItem | None:
    import difflib

    needle = normalize_title_for_match(title)
    if not needle:
        return None

    best: tuple[float, ManifestItem | None] = (0.0, None)
    for item in manifest.values():
        hay = normalize_title_for_match(item.label)
        if not hay:
            continue
        if needle == hay:
            return item
        if needle in hay or hay in needle:
            score = 0.95
        else:
            score = difflib.SequenceMatcher(a=needle, b=hay).ratio()
        if score > best[0]:
            best = (score, item)

    return best[1] if best[0] >= 0.84 else None


def archive_downloads_into_raw(repo_root: Path, manifest: dict[str, ManifestItem]) -> tuple[int, int]:
    download_root = repo_root / "raw" / "download"
    if not download_root.exists():
        return (0, 0)
    manifest_by_url = build_manifest_url_index(manifest)
    moved = 0
    matched = 0
    now = datetime.now().astimezone().isoformat(timespec="seconds")

    for path in sorted(download_root.rglob("*.md")):
        if not path.is_file():
            continue
        url = normalize_source_url(extract_source_url_from_raw(path))
        item = manifest_by_url.get(url) if url else None
        if item is None:
            title = extract_title_from_markdown(path)
            item = find_best_manifest_match_for_title(title, manifest)
        if item is None:
            # Unmatched: keep it in download/ for now.
            continue

        target = build_raw_file_output(repo_root, item)
        target.parent.mkdir(parents=True, exist_ok=True)
        target = pick_nonconflicting_path(target)
        shutil.move(str(path), str(target))

        item.raw_path = str(target.resolve())
        if raw_looks_complete(read_path_text(item.raw_path)):
            item.status = "success"
            if not item.fetch_method:
                item.fetch_method = "manual"
        item.message = "手动下载到 raw/download，已按标题归档并绑定 raw_path。"
        item.updated = now

        moved += 1
        matched += 1

    return (moved, matched)


def markdown_link(label: str, path_text: str) -> str:
    return f"[{label}]({path_text})" if path_text else label


def status_label(status: str) -> str:
    return STATUS_LABELS.get(status, status)


def theme_page_name(theme: str) -> str:
    return THEME_PAGE_NAMES.get(theme, sanitize_path_component(theme, fallback="未分类"))


def load_entries(paths: list[Path]) -> list[BookmarkEntry]:
    entries: list[BookmarkEntry] = []
    seen_urls: set[str] = set()
    for path in paths:
        text = path.read_text(encoding="utf-8")
        part_match = re.search(r"part(\d+)\.md$", path.name)
        part = int(part_match.group(1)) if part_match else 0
        theme = THEME_BY_PART.get(part, "未分类")
        for match in BOOKMARK_LINK_PATTERN.finditer(text):
            label = re.sub(r"\s+", " ", match.group("label")).strip()
            url = match.group("url").strip()
            if JUNK_LABEL_PATTERN.search(label):
                continue
            if url in seen_urls:
                continue
            seen_urls.add(url)
            entries.append(
                BookmarkEntry(
                    source_file=path.as_posix(),
                    part=part,
                    theme=theme,
                    label=label,
                    url=url,
                )
            )
    return entries


def load_manifest(path: Path) -> dict[str, ManifestItem]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    result: dict[str, ManifestItem] = {}
    for url, item in payload.items():
        result[url] = ManifestItem(**item)
    return result


def save_manifest(path: Path, manifest: dict[str, ManifestItem]) -> None:
    serializable = {url: asdict(item) for url, item in sorted(manifest.items())}
    path.write_text(json.dumps(serializable, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def ensure_manifest_item(entry: BookmarkEntry, manifest: dict[str, ManifestItem]) -> ManifestItem:
    item = manifest.get(entry.url)
    domain = urlparse(entry.url).netloc.lower()
    if item is None:
        item = ManifestItem(
            source_file=entry.source_file,
            theme=entry.theme,
            label=entry.label,
            url=entry.url,
            domain=domain,
            status="pending",
        )
        manifest[entry.url] = item
    else:
        item.source_file = entry.source_file
        item.theme = entry.theme
        item.label = entry.label
        item.domain = domain
    return item


def detect_kind(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    if host in {"www.youtube.com", "youtube.com", "youtu.be", "www.bilibili.com", "bilibili.com"}:
        return "video"
    if host == "arxiv.org" and path.startswith("/pdf/"):
        return "pdf"
    if path.endswith(".pdf"):
        return "pdf"
    return "web"


def normalize_note_title(value: str, fallback: str = "未命名资料") -> str:
    value = re.sub(r"\s+", " ", value).strip()
    value = re.sub(r'[\\/:*?"<>|]', " ", value)
    value = re.sub(r"\s+", " ", value).strip().rstrip(".")
    if len(value) > 120:
        value = value[:120].rstrip()
    return value or fallback


def entry_priority(entry: BookmarkEntry) -> tuple[int, int, str]:
    host = urlparse(entry.url).netloc.lower()
    priority = 9
    if any(domain in host for domain in ("anthropic.com", "openai.com", "modelcontextprotocol.io", "docs.anthropic.com")):
        priority = 0
    elif "github.com" in host:
        priority = 1
    elif any(domain in host for domain in ("juejin.cn", "aicoding.juejin.cn", "substack.com", "blog.google")):
        priority = 2
    elif any(domain in host for domain in ("medium.com", "oschina.net", "aliyun.com", "cloud.tencent.com")):
        priority = 3
    elif "mp.weixin.qq.com" in host:
        priority = 8
    return (priority, entry.part, entry.label)


def build_raw_output(repo_root: Path, entry: BookmarkEntry) -> Path:
    host = urlparse(entry.url).netloc.lower()
    label = sanitize_path_component(entry.label, fallback="bookmark")
    if "mp.weixin.qq.com" in host:
        return repo_root / "raw" / "weixin" / label
    if "juejin.cn" in host or "aicoding.juejin.cn" in host:
        return repo_root / "raw" / "juejin" / label
    if "github.com" in host:
        return repo_root / "raw" / "github" / label
    if "anthropic.com" in host or "openai.com" in host or "modelcontextprotocol.io" in host:
        return repo_root / "raw" / "official" / label
    return repo_root / "raw" / "web" / label


def github_raw_url_candidates(url: str) -> list[str]:
    normalized = normalize_source_url(url)
    parsed = urlparse(normalized)
    if parsed.netloc.lower() != "github.com":
        return []
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 2:
        return []
    owner, repo = parts[0], parts[1]

    # File URLs: /<owner>/<repo>/blob/<branch>/<path...>
    if len(parts) >= 5 and parts[2] == "blob":
        branch = parts[3]
        file_path = "/".join(parts[4:])
        return [f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{file_path}"]

    # Directory URLs: /<owner>/<repo>/tree/<branch>/<path...> -> try README in that branch root.
    if len(parts) >= 4 and parts[2] == "tree":
        branch = parts[3]
        return [
            f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/README.md",
            f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/README.zh.md",
        ]

    # Repo URLs: /<owner>/<repo> (optionally with query like ?tab=readme-ov-file)
    if len(parts) == 2:
        return [
            f"https://raw.githubusercontent.com/{owner}/{repo}/main/README.md",
            f"https://raw.githubusercontent.com/{owner}/{repo}/main/README.zh.md",
            f"https://raw.githubusercontent.com/{owner}/{repo}/master/README.md",
            f"https://raw.githubusercontent.com/{owner}/{repo}/master/README.zh.md",
        ]

    return []


def path_text(path: Path, repo_root: Path) -> str:
    try:
        return path.relative_to(repo_root).as_posix()
    except ValueError:
        return path.as_posix()


def extract_source_url_from_raw(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    match = re.search(r"(?mi)^- Source: <([^>]+)>", text)
    if not match:
        match = re.search(r'(?mi)^source:\s*"?([^"\n]+)"?\s*$', text)
    return match.group(1).strip() if match else ""


def normalize_source_url(url: str) -> str:
    if not url:
        return ""
    normalized = urldefrag(url.strip())[0].rstrip("/")
    parsed = urlparse(normalized)
    if not parsed.scheme or not parsed.netloc:
        return normalized
    query_parts = []
    for part in parsed.query.split("&"):
        if not part:
            continue
        key = part.split("=", 1)[0].lower()
        if key == "poc_token":
            continue
        query_parts.append(part)
    query = "&".join(sorted(query_parts))
    return parsed._replace(query=query, fragment="").geturl()


def iter_raw_markdown_files(repo_root: Path) -> list[Path]:
    raw_root = repo_root / "raw"
    files: list[Path] = []
    for path in raw_root.rglob("*.md"):
        if "bookmarks-ingest" in path.parts:
            continue
        files.append(path)
    return files


def extract_title_from_markdown(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return path.stem

    head = text[:6000]
    if head.startswith("---\n"):
        end = head.find("\n---", 4)
        if end != -1:
            fm = head[4:end]
            match = re.search(r'(?mi)^title:\s*"?([^"\n]+)"?\s*$', fm)
            if match:
                return match.group(1).strip()

    match = re.search(r"(?m)^#\s+(.+?)\s*$", head)
    if match:
        return match.group(1).strip()

    return path.stem


def build_raw_title_index(repo_root: Path) -> dict[str, list[Path]]:
    index: dict[str, list[Path]] = {}
    for path in iter_raw_markdown_files(repo_root):
        title = extract_title_from_markdown(path)
        key = normalize_title_for_match(title)
        if not key:
            continue
        index.setdefault(key, []).append(path)
    return index


def build_manifest_url_index(manifest: dict[str, ManifestItem]) -> dict[str, ManifestItem]:
    result: dict[str, ManifestItem] = {}
    for item in manifest.values():
        result[normalize_source_url(item.url)] = item
    return result


def build_raw_source_index(repo_root: Path) -> dict[str, list[Path]]:
    index: dict[str, list[Path]] = {}
    for path in iter_raw_markdown_files(repo_root):
        source_url = normalize_source_url(extract_source_url_from_raw(path))
        if not source_url:
            continue
        index.setdefault(source_url, []).append(path)
    return index


def guess_raw_paths_for_item(repo_root: Path, item: ManifestItem) -> list[Path]:
    bucket = guess_theme_from_url(item.url)
    label = sanitize_path_component(item.label, fallback="download")
    candidates: list[Path] = []
    candidates.append(repo_root / "raw" / bucket / label / "index.md")
    candidates.append(repo_root / "raw" / bucket / f"{label}.md")
    # Try numbered suffix variants (common when duplicate downloads happened)
    for i in range(2, 8):
        candidates.append(repo_root / "raw" / bucket / f"{label} {i}" / "index.md")
        candidates.append(repo_root / "raw" / bucket / f"{label} {i}.md")
    return candidates


def path_variants_for_repair(raw_path: str) -> list[Path]:
    if not raw_path:
        return []
    path = Path(raw_path)
    variants: list[Path] = []
    variants.append(path)
    # index.md <-> sibling .md
    if path.name == "index.md":
        variants.append(path.parent.with_suffix(".md"))
    elif path.suffix.lower() == ".md":
        variants.append(path.with_suffix("") / "index.md")

    # Remove trailing " N" from directory or file name.
    suffix_pattern = re.compile(r"^(?P<base>.+?) (?P<num>[1-9]\d*)$")
    if path.name == "index.md":
        parent = path.parent
        match = suffix_pattern.match(parent.name)
        if match:
            variants.append(parent.parent / match.group("base") / "index.md")
            variants.append(parent.parent / f"{match.group('base')}.md")
    else:
        match = suffix_pattern.match(path.stem)
        if match:
            variants.append(path.with_name(match.group("base") + path.suffix))
            variants.append(path.parent / match.group("base") / "index.md")

    # Deduplicate while preserving order
    seen: set[str] = set()
    out: list[Path] = []
    for v in variants:
        key = str(v)
        if key in seen:
            continue
        seen.add(key)
        out.append(v)
    return out


def repair_missing_raw_paths(manifest: dict[str, ManifestItem], repo_root: Path) -> int:
    raw_source_index = build_raw_source_index(repo_root)
    raw_title_index = build_raw_title_index(repo_root)
    repaired = 0
    now = datetime.now().astimezone().isoformat(timespec="seconds")

    for item in manifest.values():
        if item.status != "success":
            continue
        if is_video_item(item):
            continue
        if item.raw_path and Path(item.raw_path).exists():
            continue

        # 1) Repair by path variants (rename, index.md<->file.md, remove suffix numbers)
        for candidate in path_variants_for_repair(item.raw_path):
            if candidate.exists() and candidate.is_file():
                item.raw_path = str(candidate.resolve())
                item.message = "修复 raw_path：检测到文件路径漂移/重命名。"
                item.updated = now
                repaired += 1
                break
        else:
            # 2) Repair by URL match (preferred when present)
            candidates = raw_source_index.get(normalize_source_url(item.url), [])
            best = choose_best_raw_candidate(candidates)
            if best and best.exists():
                item.raw_path = str(best.resolve())
                item.message = "修复 raw_path：按 Source URL 重新绑定。"
                item.updated = now
                repaired += 1
                continue

            # 3) Repair by title match (manual downloads without Source)
            key = normalize_title_for_match(item.label)
            best = choose_best_raw_candidate(raw_title_index.get(key, []))
            if best and best.exists():
                item.raw_path = str(best.resolve())
                item.message = "修复 raw_path：按标题匹配重新绑定。"
                item.updated = now
                repaired += 1
                continue

            # 4) Repair by conventional guessed paths
            for candidate in guess_raw_paths_for_item(repo_root, item):
                if candidate.exists() and candidate.is_file():
                    item.raw_path = str(candidate.resolve())
                    item.message = "修复 raw_path：按目录约定推断并命中。"
                    item.updated = now
                    repaired += 1
                    break

    return repaired


def choose_best_raw_candidate(paths: list[Path]) -> Path | None:
    if not paths:
        return None
    scored = sorted(
        paths,
        key=lambda path: (
            0 if raw_looks_complete(read_path_text(str(path))) else 1,
            0 if path.name != "index.md" else 1,
            len(str(path)),
        ),
    )
    return scored[0]


def resolve_manifest_raw_path(
    item: ManifestItem,
    raw_source_index: dict[str, list[Path]],
) -> tuple[Path | None, bool]:
    current_path = Path(item.raw_path) if item.raw_path else None
    candidates = raw_source_index.get(normalize_source_url(item.url), [])
    current_exists = bool(current_path and current_path.exists())
    current_complete = raw_looks_complete(read_path_text(str(current_path))) if current_exists else False
    best = choose_best_raw_candidate(candidates)
    if current_exists and (not best or str(best) == str(current_path)):
        return current_path, False
    if not best:
        return current_path, False
    best_complete = raw_looks_complete(read_path_text(str(best)))
    if current_exists and current_complete and not best_complete:
        return current_path, False

    updated = item.raw_path != str(best)
    item.raw_path = str(best)
    return best, updated


def correct_blocked_items_with_recovered_raw(manifest: dict[str, ManifestItem], repo_root: Path) -> int:
    raw_source_index = build_raw_source_index(repo_root)
    raw_title_index = build_raw_title_index(repo_root)
    updated = 0
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    for item in manifest.values():
        if item.status != "blocked":
            continue
        resolved, _ = resolve_manifest_raw_path(item, raw_source_index)
        resolved_text = read_path_text(str(resolved)) if resolved and resolved.exists() else ""
        if not raw_looks_complete(resolved_text):
            # Fallback for manual downloads without Source URL metadata.
            key = normalize_title_for_match(item.label)
            best = choose_best_raw_candidate(raw_title_index.get(key, []))
            if not best or not raw_looks_complete(read_path_text(str(best))):
                continue
            item.raw_path = str(best.resolve())
        item.status = "success"
        if not item.fetch_method:
            item.fetch_method = "manual"
        if not item.message or "命中平台限制页" in item.message:
            item.message = "发现可用正文 raw，已从 blocked 更正为 success。"
        item.updated = now
        updated += 1
    return updated


def refresh_source_note_markers(source_note_path: Path, today: str) -> bool:
    if not source_note_path.exists() or not source_note_path.is_file():
        return False
    try:
        text = source_note_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False

    if not text.startswith("---\n"):
        return False
    end = text.find("\n---", 4)
    if end == -1:
        return False
    fm = text[4:end].splitlines()
    changed = False
    new_fm: list[str] = []
    for line in fm:
        stripped = line.strip()
        if stripped in {"- fetch-partial", "- wechat-noise"}:
            changed = True
            continue
        if re.match(r"^updated:\s*\d{4}-\d{2}-\d{2}\s*$", stripped):
            new_fm.append(f"updated: {today}")
            changed = True
            continue
        new_fm.append(line)

    if not any(line.strip().startswith("updated:") for line in new_fm):
        new_fm.insert(0, f"updated: {today}")
        changed = True

    if not changed:
        return False

    new_text = "---\n" + "\n".join(new_fm).rstrip() + "\n---" + text[end + 4 :]
    source_note_path.write_text(new_text, encoding="utf-8")
    return True


def refetch_github_raw(manifest: dict[str, ManifestItem], repo_root: Path, limit: int = 0) -> int:
    def infer_title_from_markdown_text(markdown: str) -> str:
        for raw_line in markdown.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith("#"):
                return line.lstrip("#").strip()
            if len(line) <= 120:
                return line
        return ""

    def build_raw_markdown_page(*, title: str, source_url: str, body: str) -> str:
        saved_at = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
        parts = [f"# {title or 'Untitled Page'}", ""]
        if source_url:
            parts.append(f"- Source: <{source_url}>")
        parts.append(f"- Saved: {saved_at}")
        parts.extend(["", "---", ""])
        parts.append(body.strip().rstrip() if body.strip() else "")
        return "\n".join(parts).rstrip() + "\n"

    targets: list[ManifestItem] = []
    for item in manifest.values():
        if item.status != "success":
            continue
        if is_video_item(item):
            continue
        if "github.com" not in (item.domain or ""):
            continue
        if not item.raw_path:
            continue
        raw_path = Path(item.raw_path)
        if not raw_path.exists():
            continue
        # Allow both directory-style (index.md) and legacy file-style (.md).
        if raw_path.name != "index.md" and raw_path.suffix.lower() != ".md":
            continue
        targets.append(item)

    targets.sort(key=lambda it: (it.theme, it.label))
    if limit > 0:
        targets = targets[:limit]

    updated = 0
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    for item in targets:
        candidates = github_raw_url_candidates(item.url)
        if not candidates:
            continue
        raw_path = Path(item.raw_path)

        # Prefer refetching into the raw path referenced by the existing source note (if any),
        # because that's what your wiki pages are most likely pointing at.
        preferred_index_md: Path | None = None
        if item.source_note:
            note_path = Path(item.source_note)
            if note_path.exists() and note_path.is_file():
                try:
                    note_text = note_path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    note_text = ""
                for match in re.finditer(r"\((raw/[^)]+)\)", note_text):
                    rel = match.group(1)
                    if not rel.endswith("index.md"):
                        continue
                    candidate = repo_root / rel
                    if candidate.exists() and candidate.is_file():
                        preferred_index_md = candidate
                        break

        if preferred_index_md:
            raw_path = preferred_index_md
            item.raw_path = str(preferred_index_md.resolve())

        if raw_path.name == "index.md":
            output_dir = raw_path.parent
            for candidate_url in candidates:
                cmd = [
                    "python3",
                    "skills/url-to-markdown/scripts/url_to_markdown.py",
                    "--url",
                    candidate_url,
                    "--source-url",
                    item.url,
                    "--output",
                    str(output_dir),
                ]
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    cwd=repo_root,
                )
                if result.returncode != 0:
                    continue
                item.fetch_method = "github-raw"
                item.message = f"Refetched GitHub raw: {candidate_url}"
                item.updated = now
                updated += 1
                break
        else:
            # Legacy file-style raw: overwrite the file directly with the fetched markdown.
            for candidate_url in candidates:
                result = subprocess.run(
                    ["curl", "-L", "--fail", "--silent", "--show-error", candidate_url],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    cwd=repo_root,
                )
                if result.returncode != 0:
                    continue
                body = result.stdout.strip()
                if not body:
                    continue
                title = normalize_note_title(infer_title_from_markdown_text(body) or item.label)
                raw_path.write_text(build_raw_markdown_page(title=title, source_url=item.url, body=body), encoding="utf-8")
                item.fetch_method = "github-raw"
                item.message = f"Refetched GitHub raw (direct file): {candidate_url}"
                item.updated = now
                updated += 1
                break

    return updated


def extract_raw_headings(raw_text: str, limit: int = 8) -> list[str]:
    headings: list[str] = []
    for line in raw_text.splitlines():
        if not line.startswith("#"):
            continue
        title = re.sub(r"^#+\s*", "", line).strip()
        if not title or title in {"微信公众平台"}:
            continue
        if any(token in title for token in ("javascript:void", "目标", "要求", "输出")):
            continue
        if len(title) > 90:
            title = title[:90].rstrip() + "..."
        if title not in headings:
            headings.append(title)
        if len(headings) >= limit:
            break
    return headings


def extract_raw_paragraphs(raw_text: str, limit: int = 2) -> list[str]:
    # Strip common YAML frontmatter to avoid summarizing metadata.
    text = raw_text
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1:
            text = text[end + 4 :]

    parts: list[str] = []
    buf: list[str] = []
    skip_prefixes = (
        "Source:",
        "- Source:",
        "- Saved:",
        "- HTML:",
        "- Markdown:",
        "- Text:",
        "Saved:",
        "HTML:",
        "Markdown:",
        "Text:",
    )
    for line in text.splitlines():
        if not line.strip():
            if buf:
                paragraph = " ".join(s.strip() for s in buf if s.strip())
                paragraph = re.sub(r"\s+", " ", paragraph).strip()
                if 60 <= len(paragraph) <= 360 and not paragraph.startswith("!"):
                    parts.append(paragraph)
                buf = []
                if len(parts) >= limit:
                    break
            continue
        if line.lstrip().startswith(("![", "[![")):
            continue
        if line.startswith("#"):
            continue
        if line.strip().startswith(skip_prefixes):
            continue
        stripped = line.strip()
        # One-line metadata blobs from some clippers.
        if stripped.lower().startswith("--- title:") and "source:" in stripped.lower():
            continue
        if ("source:" in stripped.lower() and "http" in stripped.lower()) and ("saved:" in stripped.lower() or "html:" in stripped.lower()):
            continue
        if "javascript:void" in stripped:
            continue
        if re.match(r"(?i)^(title|type|status|created|updated|tags|sources):\s*", line.strip()):
            continue
        if line.strip() == "---":
            continue
        buf.append(line)
    return parts


def infer_keywords(raw_text: str) -> list[str]:
    keywords = []
    candidates = [
        ("RAG", ("rag", "检索增强", "检索 增强")),
        ("Chunking", ("chunk", "分块", "切分")),
        ("Embedding", ("embedding", "向量", "嵌入")),
        ("Vector DB", ("向量数据库", "vector", "milvus", "faiss", "pgvector", "pinecone")),
        ("Rerank", ("rerank", "重排")),
        ("Prompt", ("prompt", "提示词", "上下文")),
        ("Agent", ("agent", "智能体")),
        ("LangChain/LangGraph", ("langchain", "langgraph")),
        ("MCP", ("mcp", "model context protocol")),
        ("Eval/测试", ("eval", "评测", "测试", "指标")),
    ]
    hay = raw_text.lower()
    for label, pats in candidates:
        if any(p.lower() in hay for p in pats):
            keywords.append(label)
    return keywords


def replace_section(text: str, header: str, new_lines: list[str]) -> str:
    pattern = re.compile(rf"(?s)(^## {re.escape(header)}\n\n)(.*?)(\n## |\Z)", re.MULTILINE)
    match = pattern.search(text)
    if not match:
        return text
    before, _, after = match.group(1), match.group(2), match.group(3)
    body = "\n".join(new_lines).rstrip() + "\n"
    return text[: match.start()] + before + body + after + text[match.end() :]


def enrich_source_note(source_note_path: Path, raw_text: str, today: str) -> bool:
    if not source_note_path.exists() or not source_note_path.is_file():
        return False
    try:
        text = source_note_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False

    headings = extract_raw_headings(raw_text, limit=6)
    paragraphs = extract_raw_paragraphs(raw_text, limit=2)
    keywords = infer_keywords(raw_text)

    core_lines: list[str] = []
    if headings:
        display = [h for h in headings[:3]]
        core_lines.append(f"- 覆盖主题：{' / '.join(display)}。")
    if keywords:
        core_lines.append(f"- 关键词：{', '.join(keywords[:8])}。")
    if paragraphs:
        core_lines.append(f"- 摘要：{paragraphs[0]}")
    if len(paragraphs) > 1:
        core_lines.append(f"- 补充：{paragraphs[1]}")
    if not core_lines:
        core_lines = ["- （自动提取失败）建议人工补充核心观点。"]

    method_lines = ["- " + k for k in (keywords[:6] or ["待补充"])]
    reuse_lines = []
    if "RAG" in keywords:
        reuse_lines.append("- 可复用流程：切分/嵌入/检索/重排/生成，先把每一步的输入输出固定住再调参。")
    reuse_lines.append("- 把文中方法落地前，先用 5-10 个真实问题做小样本回归集，避免“看似有效”。")
    limit_lines = [
        "- 文章为经验总结，落地效果依赖数据质量、分块策略与评测集。",
        "- 若缺少可复现的对比/指标，建议补充最小评测闭环后再形成结论。",
    ]

    updated = text
    updated = replace_section(updated, "核心观点", core_lines)
    updated = replace_section(updated, "方法 / 框架", method_lines)
    updated = replace_section(updated, "可复用启发", reuse_lines)
    updated = replace_section(updated, "争议与局限", limit_lines)

    if updated != text:
        # Also update frontmatter updated date if present.
        if updated.startswith("---\n"):
            updated = re.sub(r"(?m)^updated:\s*\d{4}-\d{2}-\d{2}\s*$", f"updated: {today}", updated, count=1)
        source_note_path.write_text(updated, encoding="utf-8")
        return True
    return False


def refresh_and_enrich_source_notes(
    manifest: dict[str, ManifestItem],
    repo_root: Path,
    refresh: bool,
    enrich_limit: int,
) -> tuple[int, int]:
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    raw_source_index = build_raw_source_index(repo_root)
    refreshed = 0
    enriched = 0

    candidates: list[tuple[int, int, str, ManifestItem, str]] = []
    theme_priority = {"AI Coding": 0, "RAG / MCP": 1, "Agent": 2, "提示工程": 3}

    for item in manifest.values():
        if not item.source_note:
            continue
        note_path = Path(item.source_note)
        if not note_path.exists():
            continue
        resolved, _ = resolve_manifest_raw_path(item, raw_source_index)
        if not resolved or not resolved.exists():
            continue
        raw_text = read_path_text(str(resolved))
        if not raw_looks_complete(raw_text):
            continue
        note_text = read_path_text(item.source_note)
        has_stale_marker = source_note_has_any_marker(item.source_note, ("fetch-partial", "wechat-noise"))
        has_placeholder = "当前抓取正文混入微信壳层交互文案" in note_text
        needs_enrich_fix = (
            "覆盖主题：" in note_text
            and any(token in note_text for token in ("- Source:", "--- title:", "javascript:void"))
        )
        if not (has_stale_marker or has_placeholder or needs_enrich_fix):
            continue

        if refresh:
            if refresh_source_note_markers(note_path, today):
                refreshed += 1
        candidates.append((theme_priority.get(item.theme, 9), -len(raw_text), item.label, item, raw_text))

    candidates.sort(key=lambda row: (row[0], row[1], row[2]))
    for _, _, _, item, raw_text in candidates[: max(enrich_limit, 0)]:
        if enrich_source_note(Path(item.source_note), raw_text, today):
            enriched += 1

    return refreshed, enriched


def replace_source_note_path_reference(source_note_path: str, old_dir: Path, new_dir: Path, repo_root: Path) -> None:
    if not source_note_path:
        return
    path = Path(source_note_path)
    if not path.exists() or not path.is_file():
        return
    old_rel = path_text(old_dir, repo_root)
    new_rel = path_text(new_dir, repo_root)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    updated = text.replace(old_rel, new_rel)
    if updated != text:
        path.write_text(updated, encoding="utf-8")


def repoint_manifest_raw_path(manifest: dict[str, ManifestItem], old_dir: Path, new_dir: Path, repo_root: Path) -> None:
    old_index = old_dir.resolve() / "index.md"
    new_index = new_dir.resolve() / "index.md"
    touched_notes: set[str] = set()
    for item in manifest.values():
        if item.raw_path and Path(item.raw_path).resolve() == old_index:
            item.raw_path = str(new_index)
        if item.source_note and item.source_note not in touched_notes:
            replace_source_note_path_reference(item.source_note, old_dir, new_dir, repo_root)
            touched_notes.add(item.source_note)


def canonicalize_numbered_raw_dirs(repo_root: Path, manifest: dict[str, ManifestItem]) -> list[str]:
    raw_root = repo_root / "raw"
    candidate_roots = [path for path in raw_root.iterdir() if path.is_dir() and path.name != "bookmarks-ingest"]
    suffix_pattern = re.compile(r"^(?P<base>.+?) (?P<num>[1-9]\d*)$")
    actions: list[str] = []

    for root in sorted(candidate_roots):
        grouped: dict[str, list[Path]] = {}
        for child in root.iterdir():
            if not child.is_dir():
                continue
            match = suffix_pattern.match(child.name)
            if not match:
                continue
            grouped.setdefault(match.group("base"), []).append(child)

        for base_name, numbered_dirs in sorted(grouped.items()):
            numbered_dirs.sort(key=lambda path: int(suffix_pattern.match(path.name).group("num")))
            canonical_dir = root / base_name

            if not canonical_dir.exists():
                source_dir = numbered_dirs.pop(0)
                source_dir.rename(canonical_dir)
                repoint_manifest_raw_path(manifest, source_dir, canonical_dir, repo_root)
                actions.append(f"rename:{path_text(source_dir, repo_root)}->{path_text(canonical_dir, repo_root)}")

            canonical_url = normalize_source_url(extract_source_url_from_raw(canonical_dir / "index.md"))
            for duplicate_dir in list(numbered_dirs):
                duplicate_url = normalize_source_url(extract_source_url_from_raw(duplicate_dir / "index.md"))
                if canonical_url and duplicate_url and canonical_url == duplicate_url:
                    repoint_manifest_raw_path(manifest, duplicate_dir, canonical_dir, repo_root)
                    subprocess.run(["rm", "-rf", str(duplicate_dir)], check=False, cwd=repo_root)
                    actions.append(f"delete:{path_text(duplicate_dir, repo_root)}")

    return actions


def extract_output_path(stdout: str, prefix: str) -> str:
    for line in stdout.splitlines():
        if line.startswith(prefix):
            return line.split(":", 1)[1].strip()
    return ""


def infer_fetch_method(kind: str, stdout: str, raw_path: str) -> str:
    if kind == "video":
        return "youtube-whisper"
    if kind == "pdf":
        return "pdf"
    if "source.tavily.json" in stdout:
        return "tavily"
    if raw_path:
        index_path = Path(raw_path)
        output_dir = index_path.parent if index_path.name == "index.md" else index_path
        tavily_json = output_dir / "source.tavily.json"
        if tavily_json.exists() and tavily_json.stat().st_size > 0:
            return "tavily"
        source_html = output_dir / "source.html"
        if source_html.exists() and source_html.stat().st_size > 0:
            return "agent-browser/curl"
    return "未知"


def run_entry(entry: BookmarkEntry, repo_root: Path, prefer_tavily: bool = False) -> tuple[str, str, str, str]:
    kind = detect_kind(entry.url)
    if kind == "pdf":
        return run_pdf_entry(entry, repo_root)

    if kind == "video":
        cmd = [
            "python3",
            "skills/video-wiki-ingest/scripts/bilibili_to_wiki.py",
            "--url",
            entry.url,
            "--wiki-source-note",
            "--repo-root",
            str(repo_root),
        ]
    else:
        output_dir = build_raw_output(repo_root, entry)
        base_cmd = [
            "python3",
            "skills/url-to-markdown/scripts/url_to_markdown.py",
            "--output",
            str(output_dir),
            "--wiki-source-note",
            "--repo-root",
            str(repo_root),
        ]

        # GitHub: prefer fetching raw README/blob to avoid page chrome like "Skip to content".
        host = urlparse(entry.url).netloc.lower()
        if host == "github.com":
            candidates = github_raw_url_candidates(entry.url)
            for candidate_url in candidates:
                cmd = base_cmd + ["--url", candidate_url, "--source-url", entry.url]
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    cwd=repo_root,
                )
                if result.returncode == 0:
                    combined = "\n".join(
                        part for part in [result.stdout.strip(), result.stderr.strip()] if part
                    ).strip()
                    raw_path = extract_output_path(result.stdout, "Markdown")
                    source_note = extract_output_path(result.stdout, "Source note")
                    return ("success", raw_path, source_note, combined or "ok")

        # Default: normal webpage fetch. We keep --use-title-dir for non-GitHub domains.
        cmd = base_cmd + ["--url", entry.url, "--use-title-dir"]
        if prefer_tavily:
            cmd.append("--prefer-tavily")

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=repo_root,
    )
    combined = "\n".join(part for part in [result.stdout.strip(), result.stderr.strip()] if part).strip()
    if result.returncode != 0:
        return ("error", "", "", combined or "命令执行失败")

    if "Skipped wiki ingest: blocked page detected." in combined:
        raw_path = extract_output_path(result.stdout, "Markdown")
        return ("blocked", raw_path, "", "命中平台限制页，仅保留 raw 索引目录")

    if kind == "video":
        raw_path = extract_output_path(result.stdout, "Run directory")
        source_note = extract_output_path(result.stdout, "Source note")
    else:
        raw_path = extract_output_path(result.stdout, "Markdown")
        source_note = extract_output_path(result.stdout, "Source note")

    return ("success", raw_path, source_note, combined or "ok")


def run_pdf_entry(entry: BookmarkEntry, repo_root: Path) -> tuple[str, str, str, str]:
    title = normalize_note_title(entry.label.replace(".pdf", "").replace(" · 语雀", ""))
    output_dir = repo_root / "raw" / "pdf" / sanitize_path_component(title, fallback="pdf-source")
    output_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = output_dir / "source.pdf"

    result = subprocess.run(
        ["curl", "-L", "--fail", "--silent", "--show-error", entry.url, "-o", str(pdf_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=repo_root,
    )
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "PDF 下载失败"
        return ("error", "", "", message)

    result = subprocess.run(
        [
            "python3",
            "skills/pdf-wiki-ingest/scripts/pdf_to_wiki.py",
            "--repo-root",
            str(repo_root),
            "--pdf-path",
            str(pdf_path),
            "--url",
            entry.url,
            "--title",
            title,
            "--wiki-source-note",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=repo_root,
    )
    combined = "\n".join(part for part in [result.stdout.strip(), result.stderr.strip()] if part).strip()
    if result.returncode != 0:
        return ("error", "", "", combined or "命令执行失败")

    raw_path = extract_output_path(result.stdout, "Raw index")
    source_note = extract_output_path(result.stdout, "Source note")
    return ("success", raw_path, source_note, combined or "ok")


def write_progress_index(
    path: Path,
    manifest: dict[str, ManifestItem],
    repo_root: Path,
    bookmark_files: list[Path],
) -> None:
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    counts = Counter(item.status for item in manifest.values())
    theme_counter = Counter(item.theme for item in manifest.values())
    raw_source_index = build_raw_source_index(repo_root)
    verified_states = build_verified_states(manifest, raw_source_index)
    verified_counter = Counter(state.bucket for state in verified_states.values())
    stale_marked_count = sum(1 for state in verified_states.values() if state.bucket == "verified" and state.stale_marked)
    themes_dir = path.parent / "themes"

    lines = [
        "# 书签入库进度",
        "",
        f"- 更新时间：{today}",
        f"- 书签文件数：{len(bookmark_files)}",
        f"- 链接总数：{len(manifest)}",
        f"- 成功：{counts.get('success', 0)}",
        f"- 受限：{counts.get('blocked', 0)}",
        f"- 报错：{counts.get('error', 0)}",
        f"- 暂不支持：{counts.get('unsupported', 0)}",
        f"- 待处理：{counts.get('pending', 0)}",
        f"- 已验证可用：{verified_counter.get('verified', 0)}",
        f"- 成功记录但 raw 缺失：{verified_counter.get('missing_raw', 0)}",
        f"- 成功记录但正文偏弱：{verified_counter.get('weak_content', 0)}",
        f"- 原文已恢复但 source note 标记过期：{stale_marked_count}",
        "",
        "## 主题导航",
        "",
    ]

    for theme in sorted(theme_counter):
        theme_path = path_text(themes_dir / f"{theme_page_name(theme)}.md", repo_root)
        lines.append(f"- [{theme}]({theme_path})")

    ranked_themes = sorted(
        theme_counter,
        key=lambda theme: (
            -sum(1 for item in manifest.values() if item.theme == theme and item.status == "success"),
            theme,
        ),
    )

    lines.extend(["", "## 推荐优先消化", ""])
    for theme in ranked_themes[:4]:
        usable_count = sum(1 for item in manifest.values() if item.theme == theme and verified_states[item.url].bucket == "verified")
        issue_count = sum(
            1
            for item in manifest.values()
            if item.theme == theme and verified_states[item.url].bucket in {"blocked", "error", "missing_raw", "weak_content"}
        )
        lines.append(f"- {theme}: 已验证可用 {usable_count}，待处理 {issue_count}")

    lines.extend(["", "## 主题分布", ""])
    for theme, count in sorted(theme_counter.items()):
        lines.append(f"- {theme}: {count}")

    grouped: dict[str, list[ManifestItem]] = {}
    for item in manifest.values():
        grouped.setdefault(item.theme, []).append(item)

    for theme in sorted(grouped):
        counts_by_status = Counter(item.status for item in grouped[theme])
        verified_by_bucket = Counter(verified_states[item.url].bucket for item in grouped[theme])
        theme_path = path_text(themes_dir / f"{theme_page_name(theme)}.md", repo_root)
        lines.extend(["", f"## {theme}", ""])
        lines.append(f"- 主题页：[{theme}]({theme_path})")
        lines.append(
            f"- 统计：成功 {counts_by_status.get('success', 0)} / 受限 {counts_by_status.get('blocked', 0)} / 报错 {counts_by_status.get('error', 0)} / 暂不支持 {counts_by_status.get('unsupported', 0)} / 待处理 {counts_by_status.get('pending', 0)}"
        )
        lines.append(
            f"- 校验：已验证可用 {verified_by_bucket.get('verified', 0)} / raw 缺失 {verified_by_bucket.get('missing_raw', 0)} / 正文偏弱 {verified_by_bucket.get('weak_content', 0)}"
        )

    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def write_theme_indexes(manifest_dir: Path, manifest: dict[str, ManifestItem], repo_root: Path) -> None:
    themes_dir = manifest_dir / "themes"
    themes_dir.mkdir(parents=True, exist_ok=True)
    raw_source_index = build_raw_source_index(repo_root)
    verified_states = build_verified_states(manifest, raw_source_index)

    grouped: dict[str, list[ManifestItem]] = {}
    for item in manifest.values():
        grouped.setdefault(item.theme, []).append(item)

    for theme, items in grouped.items():
        path = themes_dir / f"{theme_page_name(theme)}.md"
        counts = Counter(item.status for item in items)
        verified_by_bucket = Counter(verified_states[item.url].bucket for item in items)
        stale_marked_items = [
            item for item in items if verified_states[item.url].bucket == "verified" and verified_states[item.url].stale_marked
        ]
        lines = [
            f"# {theme}",
            "",
            f"- 链接总数：{len(items)}",
            f"- 成功：{counts.get('success', 0)}",
            f"- 受限：{counts.get('blocked', 0)}",
            f"- 报错：{counts.get('error', 0)}",
            f"- 暂不支持：{counts.get('unsupported', 0)}",
            f"- 待处理：{counts.get('pending', 0)}",
            f"- 已验证可用：{verified_by_bucket.get('verified', 0)}",
            f"- 成功记录但 raw 缺失：{verified_by_bucket.get('missing_raw', 0)}",
            f"- 成功记录但正文偏弱：{verified_by_bucket.get('weak_content', 0)}",
            f"- source note 标记过期：{len(stale_marked_items)}",
            "",
            f"- 返回：[书签入库进度]({path_text(manifest_dir / 'index.md', repo_root)})",
            "",
        ]

        verified_items = sorted([item for item in items if verified_states[item.url].bucket == "verified"], key=lambda item: item.label)
        lines.extend(["", "## 已验证可用", ""])
        if not verified_items:
            lines.append("- 无")
        else:
            for item in verified_items:
                raw = markdown_link("原文", path_text(Path(item.raw_path), repo_root)) if item.raw_path else "原文"
                source = markdown_link("知识库", path_text(Path(item.source_note), repo_root)) if item.source_note else "知识库"
                suffix = " | source note 待回刷" if verified_states[item.url].stale_marked else ""
                lines.append(f"- [{item.label}]({item.url}) | {item.domain} | {raw} | {source}{suffix}")

        for title, bucket in (
            ("成功记录但 raw 缺失", "missing_raw"),
            ("成功记录但正文偏弱", "weak_content"),
        ):
            bucket_items = sorted([item for item in items if verified_states[item.url].bucket == bucket], key=lambda item: item.label)
            lines.extend(["", f"## {title}", ""])
            if not bucket_items:
                lines.append("- 无")
                continue
            for item in bucket_items:
                raw_text = f" | 原文：`{path_text(Path(item.raw_path), repo_root)}`" if item.raw_path else ""
                source_text = f" | 知识库：`{path_text(Path(item.source_note), repo_root)}`" if item.source_note else ""
                lines.append(f"- [{item.label}]({item.url}) | {item.domain}{raw_text}{source_text}")

        for status in ("blocked", "error", "unsupported", "pending"):
            status_items = sorted([item for item in items if item.status == status], key=lambda item: item.label)
            lines.extend(["", f"## {status_label(status)}", ""])
            if not status_items:
                lines.append("- 无")
                continue
            for item in status_items:
                raw = markdown_link("原文", path_text(Path(item.raw_path), repo_root)) if item.raw_path else "原文"
                source = (
                    markdown_link("知识库", path_text(Path(item.source_note), repo_root))
                    if item.source_note
                    else "知识库"
                )
                method = item.fetch_method or "未知"
                message = (
                    f" | {compact_message(item.message, 80)}"
                    if status in {"blocked", "error", "unsupported"} and item.message
                    else ""
                )
                lines.append(
                    f"- [{item.label}]({item.url}) | {item.domain} | {method} | {raw} | {source}{message}"
                )

        path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def compact_message(text: str, limit: int = 600) -> str:
    text = re.sub(r"\x1b\[[0-9;]*m", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def summarize_by_theme(items: list[ManifestItem]) -> str:
    if not items:
        return "无"
    counter = Counter(item.theme for item in items)
    return " / ".join(f"{theme} {count}" for theme, count in sorted(counter.items()))


def read_path_text(path_str: str) -> str:
    if not path_str:
        return ""
    path = Path(path_str)
    if not path.exists() or not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def source_note_has_marker(path_str: str, marker: str) -> bool:
    text = read_path_text(path_str)
    if not text:
        return False
    return marker in text


def source_note_has_any_marker(path_str: str, markers: tuple[str, ...]) -> bool:
    text = read_path_text(path_str)
    if not text:
        return False
    return any(marker in text for marker in markers)


def detect_wechat_hard_block_reasons(text: str) -> list[str]:
    reasons: list[str] = []
    length = max(len(text), 1)
    header = text[:400]
    is_generic_wechat_shell = "# 微信公众平台" in header and "Source: <https://mp.weixin.qq.com/>" in header
    for pattern in WECHAT_HARD_BLOCK_PATTERNS:
        position = text.find(pattern)
        if position == -1:
            continue
        # These markers should only count as hard blocks when the page is
        # mostly shell content, or when they appear very early in the page.
        if pattern == "参数错误":
            if is_generic_wechat_shell and (length < 2500 or position / length < 0.2):
                reasons.append(pattern)
            continue
        if length < 2500 or position / length < 0.2:
            reasons.append(pattern)
    return reasons


def is_generic_shell_page(text: str) -> bool:
    header = text[:500]
    return any(pattern in header for pattern in GENERIC_SHELL_PATTERNS)


def item_has_wechat_noise(item: ManifestItem, raw_path: Path | None = None) -> bool:
    if detect_wechat_hard_block_reasons(item.message):
        return True

    raw_text = read_path_text(str(raw_path)) if raw_path else read_path_text(item.raw_path)
    if not raw_text:
        return False

    if detect_wechat_hard_block_reasons(raw_text):
        return True

    # 微信页尾常会附带“赞 / 在看”交互文案。只有当这类噪音出现在正文前段，
    # 或页面整体极短、基本没有正文时，才将其视为异常页。
    for pattern in WECHAT_FOOTER_NOISE_PATTERNS:
        position = raw_text.find(pattern)
        if position == -1:
            continue
        if len(raw_text) < 2500:
            return True
        if position / max(len(raw_text), 1) < 0.85:
            return True
    return False


def item_is_fetch_partial(item: ManifestItem) -> bool:
    haystacks = [item.message, read_path_text(item.raw_path)]
    return any(pattern in haystack for haystack in haystacks for pattern in FETCH_PARTIAL_PATTERNS if haystack)


def raw_looks_complete(text: str) -> bool:
    if not text:
        return False
    if is_generic_shell_page(text):
        return False
    if detect_wechat_hard_block_reasons(text):
        return False
    # Some "partial" markers are high precision (checkpoint/404), but others are common
    # page chrome (e.g. GitHub "Skip to content") and can appear in otherwise complete pages.
    hard_partial = ("404: NOT_FOUND", "Vercel Security Checkpoint", "Just a moment")
    if any(pattern in text for pattern in hard_partial):
        return False
    soft_partial = ("SubscribeSign in", "Subscribe Sign in", "Skip to content")
    if any(pattern in text for pattern in soft_partial) and len(text) < 6000:
        return False

    line_count = text[:5000].count("\n")
    if len(text) >= 1800 and line_count >= 25:
        return True
    if len(text) >= 900 and line_count >= 18 and any(token in text for token in ("\n## ", "\n- ", "```")):
        return True
    return False


def verify_item(item: ManifestItem, raw_source_index: dict[str, list[Path]]) -> VerifiedState:
    resolved_raw_path, _ = resolve_manifest_raw_path(item, raw_source_index)
    raw_exists = bool(resolved_raw_path) and resolved_raw_path.exists()
    source_exists = bool(item.source_note) and Path(item.source_note).exists()
    stale_marked = source_note_has_any_marker(item.source_note, ("fetch-partial", "wechat-noise"))

    if item.status in {"error", "blocked", "unsupported", "pending"}:
        return VerifiedState(
            bucket=item.status,
            raw_exists=raw_exists,
            source_exists=source_exists,
            raw_path=str(resolved_raw_path) if resolved_raw_path else item.raw_path,
            source_note=item.source_note,
            stale_marked=stale_marked,
        )

    if not raw_exists:
        return VerifiedState(
            bucket="missing_raw",
            raw_exists=False,
            source_exists=source_exists,
            raw_path=item.raw_path,
            source_note=item.source_note,
            stale_marked=stale_marked,
        )

    raw_text = read_path_text(str(resolved_raw_path))
    if raw_looks_complete(raw_text):
        return VerifiedState(
            bucket="verified",
            raw_exists=True,
            source_exists=source_exists,
            raw_path=str(resolved_raw_path),
            source_note=item.source_note,
            stale_marked=stale_marked,
        )

    if item_is_deferred_short(item):
        return VerifiedState(
            bucket="deferred_short",
            raw_exists=True,
            source_exists=source_exists,
            raw_path=str(resolved_raw_path),
            source_note=item.source_note,
            stale_marked=stale_marked,
        )

    return VerifiedState(
        bucket="weak_content",
        raw_exists=True,
        source_exists=source_exists,
        raw_path=str(resolved_raw_path),
        source_note=item.source_note,
        stale_marked=stale_marked,
    )


def build_verified_states(
    manifest: dict[str, ManifestItem],
    raw_source_index: dict[str, list[Path]],
) -> dict[str, VerifiedState]:
    return {item.url: verify_item(item, raw_source_index) for item in manifest.values()}


def classify_raw_noise(text: str) -> list[str]:
    reasons: list[str] = []
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    reasons.extend(detect_wechat_hard_block_reasons(text))
    # Avoid substring false-positives (e.g. “去阅读项目代码”). Only match line-level boilerplate.
    for pattern in RAW_NOISE_PATTERNS:
        if pattern in lines:
            reasons.append(pattern)
    if any(line.startswith("点击上方蓝字关注") for line in lines):
        reasons.append("点击上方蓝字关注*")
    for pattern in WECHAT_FOOTER_NOISE_PATTERNS:
        position = text.find(pattern)
        if position == -1:
            continue
        if len(text) < 2500:
            reasons.append(pattern)
            continue
        if position / max(len(text), 1) < 0.85:
            reasons.append(pattern)
    return list(dict.fromkeys(reasons))


def scan_raw_noise(repo_root: Path, manifest: dict[str, ManifestItem]) -> list[RawNoiseItem]:
    raw_to_manifest: dict[str, ManifestItem] = {}
    scan_paths: list[Path] = []
    for item in manifest.values():
        if not item.raw_path:
            continue
        path = Path(item.raw_path)
        if not path.exists() or not path.is_file():
            continue
        resolved = str(path.resolve())
        raw_to_manifest[resolved] = item
        scan_paths.append(path)
    findings: list[RawNoiseItem] = []
    for path in scan_paths:
        text = read_path_text(str(path))
        if not text:
            continue
        reasons = classify_raw_noise(text)
        if not reasons:
            continue
        manifest_item = raw_to_manifest.get(str(path.resolve()))
        findings.append(
            RawNoiseItem(
                path=path_text(path, repo_root),
                label=manifest_item.label if manifest_item else (path.parent.name if path.name == "index.md" else path.stem),
                reasons=reasons,
                theme=manifest_item.theme if manifest_item else "",
                url=manifest_item.url if manifest_item else "",
            )
        )
    findings.sort(key=lambda item: (item.theme or "未分类", item.label, item.path))
    return findings


def split_raw_noise_items(items: list[RawNoiseItem]) -> tuple[list[RawNoiseItem], list[RawNoiseItem]]:
    hard: list[RawNoiseItem] = []
    soft: list[RawNoiseItem] = []
    for item in items:
        if any(reason in WECHAT_HARD_BLOCK_PATTERNS for reason in item.reasons):
            hard.append(item)
        else:
            soft.append(item)
    return hard, soft


def write_raw_noise_report(path: Path, items: list[RawNoiseItem]) -> None:
    hard, soft = split_raw_noise_items(items)
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    lines: list[str] = [
        "# raw 聒噪全量清单",
        "",
        f"- 更新时间：{today}",
        f"- 总数：{len(items)}",
        f"- 强异常页：{len(hard)}",
        f"- 正文可用但混有壳层：{len(soft)}",
        "- 回到复核清单：[review.md](review.md)",
        "",
        "- 说明：这个清单来自遍历 `raw/`（排除 `raw/bookmarks-ingest/`）的结果，用于定位原始抓取中的壳层/异常文案。",
        "",
        "## 强异常页",
        "",
    ]
    if not hard:
        lines.append("- 无")
    else:
        for item in hard:
            theme_text = f" | {item.theme}" if item.theme else ""
            url_text = f" | <{item.url}>" if item.url else ""
            lines.append(f"- {item.label}{theme_text}{url_text}")
            lines.append(f"  原文：`{item.path}`")
            lines.append(f"  命中：`{', '.join(item.reasons)}`")

    lines.extend(["", "## 正文可用但混有壳层", ""])
    if not soft:
        lines.append("- 无")
    else:
        for item in soft:
            theme_text = f" | {item.theme}" if item.theme else ""
            url_text = f" | <{item.url}>" if item.url else ""
            lines.append(f"- {item.label}{theme_text}{url_text}")
            lines.append(f"  原文：`{item.path}`")
            lines.append(f"  命中：`{', '.join(item.reasons)}`")

    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def build_extra_review_buckets(
    manifest: dict[str, ManifestItem],
    raw_source_index: dict[str, list[Path]],
) -> list[ReviewBucket]:
    verified_states = build_verified_states(manifest, raw_source_index)
    pdf_items = sorted(
        [
            item
            for item in manifest.values()
            if verified_states[item.url].bucket in {"weak_content", "missing_raw"}
            and detect_kind(item.url) == "pdf"
            and not is_video_item(item)
        ],
        key=lambda item: (item.theme, item.label),
    )
    video_items = sorted(
        [
            item
            for item in manifest.values()
            if verified_states[item.url].bucket in {"weak_content", "missing_raw"} and is_video_item(item)
        ],
        key=lambda item: (classify_video_bucket(item), item.theme, item.label),
    )
    partial_items = sorted(
        [
            item
            for item in manifest.values()
            if verified_states[item.url].bucket == "weak_content"
            and not is_video_item(item)
            and detect_kind(item.url) != "pdf"
        ],
        key=lambda item: (item.theme, item.label),
    )
    missing_raw_items = sorted(
        [
            item
            for item in manifest.values()
            if verified_states[item.url].bucket == "missing_raw"
            and not is_video_item(item)
            and detect_kind(item.url) != "pdf"
        ],
        key=lambda item: (item.theme, item.label),
    )
    stale_marked_items = sorted(
        [
            item
            for item in manifest.values()
            if verified_states[item.url].bucket == "verified"
            and verified_states[item.url].stale_marked
            and not is_video_item(item)
            and detect_kind(item.url) != "pdf"
        ],
        key=lambda item: (item.theme, item.label),
    )
    wechat_noise_items = sorted(
        [
            item
            for item in manifest.values()
            if "mp.weixin.qq.com" in item.domain
            and item_has_wechat_noise(item, Path(verified_states[item.url].raw_path) if verified_states[item.url].raw_path else None)
        ],
        key=lambda item: (item.theme, item.label),
    )
    return [
        ReviewBucket(
            title="视频（暂不处理）",
            description="live/bilibili/youtube 等视频来源，暂不纳入半抓取补全队列。",
            items=video_items,
        ),
        ReviewBucket(
            title="PDF（暂不处理）",
            description="PDF 来源先不处理（后续走统一的 PDF 解析/入库链路）。",
            items=pdf_items,
        ),
        ReviewBucket(
            title="成功记录但 raw 缺失",
            description="manifest 记为 success，但 raw 目录中已找不到对应 index.md，当前无法从 raw 复核真伪。",
            items=missing_raw_items,
        ),
        ReviewBucket(
            title="半抓取待补抓",
            description="当前能找到 raw，但正文仍偏弱，常见于订阅层、目录页、壳层页或仅保留下载占位。",
            items=partial_items,
        ),
        ReviewBucket(
            title="原文已恢复但 source note 标记过期",
            description="raw 正文已经可用，但 source note 仍残留 fetch-partial / wechat-noise 等旧标记，说明知识库摘要没有跟上。",
            items=stale_marked_items,
        ),
        ReviewBucket(
            title="微信壳层噪音待清理",
            description="页面命中微信交互壳层、风控壳层或异常页文案，不适合直接作为有效知识来源使用。",
            items=wechat_noise_items,
        ),
    ]


def write_review_report(path: Path, manifest: dict[str, ManifestItem], repo_root: Path) -> None:
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    issue_statuses = ("error", "unsupported", "blocked")
    grouped: dict[str, list[ManifestItem]] = {
        status: sorted(
            [item for item in manifest.values() if item.status == status],
            key=lambda item: (item.theme, item.label),
        )
        for status in issue_statuses
    }
    raw_source_index = build_raw_source_index(repo_root)
    extra_buckets = build_extra_review_buckets(manifest, raw_source_index)
    raw_noise_items = scan_raw_noise(repo_root, manifest)
    raw_noise_hard, raw_noise_soft = split_raw_noise_items(raw_noise_items)

    partial_bucket = next((bucket for bucket in extra_buckets if bucket.title == "半抓取待补抓"), None)
    video_bucket = next((bucket for bucket in extra_buckets if bucket.title == "视频（暂不处理）"), None)
    pdf_bucket = next((bucket for bucket in extra_buckets if bucket.title == "PDF（暂不处理）"), None)
    missing_raw_bucket = next((bucket for bucket in extra_buckets if bucket.title == "成功记录但 raw 缺失"), None)
    stale_marked_bucket = next((bucket for bucket in extra_buckets if bucket.title == "原文已恢复但 source note 标记过期"), None)
    wechat_bucket = next((bucket for bucket in extra_buckets if bucket.title == "微信壳层噪音待清理"), None)

    lines = [
        "# 书签入库复核清单",
        "",
        f"- 更新时间：{today}",
        f"- 报错：{len(grouped['error'])}",
        f"- 受限：{len(grouped['blocked'])}",
        f"- 成功记录但 raw 缺失：{len(missing_raw_bucket.items) if missing_raw_bucket else 0}",
        f"- 半抓取待补抓：{len(partial_bucket.items) if partial_bucket else 0}",
        f"- 视频（暂不处理）：{len(video_bucket.items) if video_bucket else 0}",
        f"- PDF（暂不处理）：{len(pdf_bucket.items) if pdf_bucket else 0}",
        f"- 原文已恢复但 source note 标记过期：{len(stale_marked_bucket.items) if stale_marked_bucket else 0}",
        f"- 微信壳层噪音待清理：{len(wechat_bucket.items) if wechat_bucket else 0}",
        f"- raw 强异常页：{len(raw_noise_hard)}",
        f"- raw 正文可用但混有壳层：{len(raw_noise_soft)}",
        "",
        "## 校验结论",
        "",
        f"- 真正需要处理的抓取异常主要是 `报错 + 受限 = {len(grouped['error']) + len(grouped['blocked'])}` 条。",
        f"- `成功记录但 raw 缺失` 有 `{len(missing_raw_bucket.items) if missing_raw_bucket else 0}` 条，说明 manifest 的 success 不能直接当真，必须回到 raw 校验。",
        f"- `半抓取待补抓` 是 raw 存在但正文仍不足的条目，共 `{len(partial_bucket.items) if partial_bucket else 0}` 条。",
        f"- `视频（暂不处理）` 共 `{len(video_bucket.items) if video_bucket else 0}` 条，已从半抓取队列剥离。",
        f"- `PDF（暂不处理）` 共 `{len(pdf_bucket.items) if pdf_bucket else 0}` 条，暂不纳入补抓队列。",
        f"- `原文已恢复但 source note 标记过期` 有 `{len(stale_marked_bucket.items) if stale_marked_bucket else 0}` 条，说明 source note 需要回刷。",
        f"- 当前抓取异常主题分布：{summarize_by_theme(grouped['error'] + grouped['blocked'])}。",
    ]

    for status in ("error", "blocked"):
        items = grouped[status]
        lines.extend(["", f"## {status_label(status)}", ""])
        if not items:
            lines.append("- 无")
            continue
        lines.append(f"- 主题分布：{summarize_by_theme(items)}")
        lines.append("")
        for item in items:
            raw_text = f" | 原文：`{path_text(Path(item.raw_path), repo_root)}`" if item.raw_path else ""
            lines.append(
                f"- `{item.theme}` | [{item.label}](<{item.url}>) | `{item.domain}` | `{item.fetch_method or '未知'}`{raw_text}"
            )
            lines.append(f"  说明：{compact_message(item.message or '(empty)', 180)}")

    lines.extend(["", "## raw 缺失（manifest 记 success，但本地只有 404/占位壳）", ""])
    if not missing_raw_bucket or not missing_raw_bucket.items:
        lines.append("- 无")
    else:
        lines.extend(
            [
                "处理原则：",
                "1. 如果你能手动拿到正文：把 Markdown 放到 `raw/download/`，再跑 `python3 scripts/ingest_bookmarks.py --archive-downloads` 归档并回写 manifest。",
                "2. 如果当前 raw 明确是 404/Not Found 占位：直接删这条索引（别再占坑）。",
                "",
            ]
        )
        strong_delete_markers = {
            "wechat:deleted",
            "access:denied",
            "github:404",
            "message:404",
            "label:404",
            "raw_path:404",
            "feishu:denied",
        }
        for item in sorted(missing_raw_bucket.items, key=missing_raw_priority):
            raw_text = f"raw：`{path_text(Path(item.raw_path), repo_root)}`" if item.raw_path else "raw：`missing`"
            source_text = f" | 知识库：`{path_text(Path(item.source_note), repo_root)}`" if item.source_note else ""
            lines.append(f"- `{item.theme}` | [{item.label}](<{item.url}>) | {raw_text}{source_text}")
            reasons = deletion_reasons_for_missing_raw(item)
            if any(r in strong_delete_markers for r in reasons):
                reason_text = ", ".join(reasons) if reasons else "local-evidence"
                lines.append(f"  建议：删除（本地证据：`{reason_text}`）")
            else:
                lines.append("  建议：补回（手动下载到 `raw/download/` 后归档）")

    lines.extend(["", "## 半抓取待补抓", ""])
    lines.append("当前 raw 仍偏弱，只抓到页面壳层、订阅层、目录、占位或短内容，正文不足以支撑稳定摘要。")
    lines.append("")
    if not partial_bucket or not partial_bucket.items:
        lines.append("- 无")
    else:
        lines.append(f"- 主题分布：{summarize_by_theme(partial_bucket.items)}")
        lines.append("")
        for item in partial_bucket.items:
            raw_text = f" | 原文：`{path_text(Path(item.raw_path), repo_root)}`" if item.raw_path else ""
            source_text = f" | 知识库：`{path_text(Path(item.source_note), repo_root)}`" if item.source_note else ""
            lines.append(f"- `{item.theme}` | [{item.label}](<{item.url}>)" + raw_text + source_text)

    lines.extend(["", "## 原文已恢复但 source note 标记过期", ""])
    if not stale_marked_bucket or not stale_marked_bucket.items:
        lines.append("- 无")
    else:
        lines.append(f"- 主题分布：{summarize_by_theme(stale_marked_bucket.items)}")
        lines.append("")
        for item in stale_marked_bucket.items:
            raw_text = f" | 原文：`{path_text(Path(item.raw_path), repo_root)}`" if item.raw_path else ""
            source_text = f" | 知识库：`{path_text(Path(item.source_note), repo_root)}`" if item.source_note else ""
            lines.append(f"- `{item.theme}` | [{item.label}](<{item.url}>)" + raw_text + source_text)

    lines.extend(["", "## 噪音说明", ""])
    lines.append(
        f"- `微信壳层噪音待清理`：{len(wechat_bucket.items) if wechat_bucket else 0} 条，主题分布为 {summarize_by_theme(wechat_bucket.items if wechat_bucket else [])}。"
    )
    lines.append(f"- `raw 强异常页`：{len(raw_noise_hard)} 条。")
    lines.append(f"- `raw 正文可用但混有壳层`：{len(raw_noise_soft)} 条。")
    if raw_noise_hard:
        lines.extend(["", "## raw 强异常页", ""])
        for item in raw_noise_hard:
            theme_text = f" | `{item.theme}`" if item.theme else ""
            url_text = f" | <{item.url}>" if item.url else ""
            lines.append(f"- `{item.label}`{theme_text}{url_text}")
            lines.append(f"  原文：`{item.path}`")
            lines.append(f"  命中：`{', '.join(item.reasons)}`")
    else:
        lines.extend(["", "## raw 强异常页", "", "- 无"])
    if raw_noise_soft:
        lines.extend(["", "## raw 正文可用但混有壳层", ""])
        for item in raw_noise_soft:
            theme_text = f" | `{item.theme}`" if item.theme else ""
            url_text = f" | <{item.url}>" if item.url else ""
            lines.append(f"- `{item.label}`{theme_text}{url_text}")
            lines.append(f"  原文：`{item.path}`")
            lines.append(f"  命中：`{', '.join(item.reasons)}`")
    else:
        lines.extend(["", "## raw 正文可用但混有壳层", "", "- 无"])

    lines.extend(["", "## 视频（暂不处理）", ""])
    if not video_bucket or not video_bucket.items:
        lines.append("- 无")
    else:
        lines.append(f"- 总量：{len(video_bucket.items)}")
        lines.append("")
        for item in video_bucket.items:
            raw_text = f" | 原文：`{path_text(Path(item.raw_path), repo_root)}`" if item.raw_path else ""
            lines.append(f"- `{item.theme}` | [{item.label}](<{item.url}>)" + raw_text)

    lines.extend(["", "## PDF（暂不处理）", ""])
    if not pdf_bucket or not pdf_bucket.items:
        lines.append("- 无")
    else:
        lines.append(f"- 总量：{len(pdf_bucket.items)}")
        lines.append("")
        for item in pdf_bucket.items:
            raw_text = f" | 原文：`{path_text(Path(item.raw_path), repo_root)}`" if item.raw_path else ""
            source_text = f" | 知识库：`{path_text(Path(item.source_note), repo_root)}`" if item.source_note else ""
            lines.append(f"- `{item.theme}` | [{item.label}](<{item.url}>)" + raw_text + source_text)

    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def update_index_updated_date(index_text: str, today: str) -> str:
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Batch ingest bookmark Markdown files into raw/wiki.")
    parser.add_argument(
        "inputs",
        nargs="*",
        type=Path,
        help="Bookmark markdown files. Defaults to raw/bookmarks_articles_2026_4_18_part*.md",
    )
    parser.add_argument("--repo-root", type=Path, default=Path.cwd(), help="Repository root.")
    parser.add_argument("--limit", type=int, default=0, help="Process at most N links in this run.")
    parser.add_argument("--theme", default="", help="Only process a specific theme.")
    parser.add_argument("--domains", default="", help="Comma-separated domain keywords to include.")
    parser.add_argument("--retry-errors", action="store_true", help="Retry items in error/blocked state.")
    parser.add_argument(
        "--archive-downloads",
        action="store_true",
        help="Archive manually downloaded markdown files from raw/download/ into raw/*/ and update manifest.",
    )
    parser.add_argument(
        "--refresh-source-notes",
        action="store_true",
        help="Refresh wiki/sources notes: remove stale fetch-partial/wechat-noise markers when raw is verified complete.",
    )
    parser.add_argument(
        "--enrich-source-notes",
        type=int,
        default=0,
        help="Enrich up to N recovered wiki/sources notes with lightweight auto-extracted summary (does not touch raw).",
    )
    parser.add_argument(
        "--write-missing-raw-recover",
        action="store_true",
        help="Write a prioritized recovery todo list for items whose manifest says success but raw_path is missing.",
    )
    parser.add_argument(
        "--write-missing-raw-delete-candidates",
        action="store_true",
        help="Write deletion candidates for missing-raw items based on local evidence (404/denied/deleted placeholders).",
    )
    parser.add_argument(
        "--redownload-missing-raw",
        action="store_true",
        help="Re-download items whose manifest says success but raw_path is missing, using url-to-markdown (with Tavily fallback).",
    )
    parser.add_argument(
        "--tavily-fill-weak-content",
        action="store_true",
        help="Re-download 'weak content' items using url-to-markdown with --prefer-tavily to try to get fuller markdown.",
    )
    parser.add_argument(
        "--tavily-fill-weak-content-selective",
        action="store_true",
        help="Same as --tavily-fill-weak-content, but only retries weak items that are not already fetched via tavily and are not pdf/video.",
    )
    parser.add_argument(
        "--prefer-non-wechat",
        action="store_true",
        help="Prioritize non-WeChat domains first to improve first-pass success rate.",
    )
    parser.add_argument(
        "--refetch-github-raw",
        action="store_true",
        help="Refetch GitHub items using raw/README URLs to avoid GitHub chrome noise (updates raw + manifest only).",
    )
    return parser.parse_args()


def missing_raw_items(manifest: dict[str, ManifestItem]) -> list[ManifestItem]:
    items: list[ManifestItem] = []
    for item in manifest.values():
        if item.status != "success":
            continue
        if not item.raw_path or Path(item.raw_path).exists():
            continue
        items.append(item)
    items.sort(key=lambda it: (it.theme, it.domain, it.label))
    return items


def missing_raw_priority(item: ManifestItem) -> tuple[int, str, str]:
    host = (item.domain or urlparse(item.url).netloc).lower()
    priority = 9
    if any(domain in host for domain in ("anthropic.com", "openai.com", "modelcontextprotocol.io", "docs.anthropic.com")):
        priority = 0
    elif "github.com" in host:
        priority = 1
    elif any(domain in host for domain in ("juejin.cn", "aicoding.juejin.cn", "substack.com", "blog.google")):
        priority = 2
    elif any(domain in host for domain in ("medium.com", "oschina.net", "aliyun.com", "cloud.tencent.com")):
        priority = 3
    elif "mp.weixin.qq.com" in host:
        priority = 8
    return (priority, item.theme, item.label)


def write_missing_raw_recover_plan(path: Path, manifest: dict[str, ManifestItem], repo_root: Path) -> None:
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    items = missing_raw_items(manifest)
    items_sorted = sorted(items, key=missing_raw_priority)
    video_items = [it for it in items_sorted if is_video_item(it)]
    non_video_items = [it for it in items_sorted if not is_video_item(it)]

    lines: list[str] = [
        "# raw 缺失补回清单",
        "",
        f"- 更新时间：{today}",
        f"- 总量：{len(items_sorted)}",
        f"- 非视频：{len(non_video_items)}",
        f"- 视频（暂不处理）：{len(video_items)}",
        "",
        "## 使用方式",
        "",
        "- 你把缺失条目的原文手动下载成 Markdown，放到 `raw/download/`（不要求包含 Source）。",
        "- 然后运行：`python3 scripts/ingest_bookmarks.py --archive-downloads`，系统会自动归档到 `raw/weixin|web|github|juejin|official` 并更新 manifest/raw_path。",
        "- 归档完再运行：`python3 scripts/ingest_bookmarks.py` 复核 `review.md` 是否下降。",
        "",
        "## Top 30（优先补回）",
        "",
    ]

    for item in non_video_items[:30]:
        lines.append(f"- `{item.theme}` | [{item.label}](<{item.url}>) | 预期 raw：`{path_text(Path(item.raw_path), repo_root)}`")

    if len(non_video_items) > 30:
        lines.extend(["", "## 全量（非视频）", ""])
        current_theme = None
        for item in non_video_items:
            if item.theme != current_theme:
                current_theme = item.theme
                lines.append(f"- 主题：`{current_theme}`（{sum(1 for it in non_video_items if it.theme==current_theme)}）")
            lines.append(f"- [{item.label}](<{item.url}>) | 预期 raw：`{path_text(Path(item.raw_path), repo_root)}`")

    if video_items:
        buckets: dict[str, list[ManifestItem]] = {}
        for item in video_items:
            buckets.setdefault(classify_video_bucket(item), []).append(item)
        lines.extend(["", "## 视频（暂不处理）", ""])
        for bucket, bucket_items in sorted(buckets.items()):
            lines.append(f"- `{bucket}`：{len(bucket_items)}")
            for item in bucket_items:
                lines.append(f"- `{item.theme}` | [{item.label}](<{item.url}>) | 预期 raw：`{path_text(Path(item.raw_path), repo_root)}`")

    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def deletion_reasons_for_missing_raw(item: ManifestItem) -> list[str]:
    reasons: list[str] = []
    message = (item.message or "").lower()
    label = (item.label or "").lower()
    url = (item.url or "").lower()
    raw_path = (item.raw_path or "").lower()

    if any(token in label for token in ("page not found", "file not found")):
        reasons.append("label:404")
    if any(token in message for token in ("page not found", "file not found", "404")):
        reasons.append("message:404")
    if any(token in raw_path for token in ("page not found", "file not found", "not found", "404", "not_found")):
        reasons.append("raw_path:404")
    if "没有权限" in (item.message or "") or "没有权限" in (item.label or ""):
        reasons.append("access:denied")
    if "没有权限访问" in raw_path or "forbidden" in message or "403" in message:
        reasons.append("access:denied")
    if "该内容已被发布者删除" in (item.message or ""):
        reasons.append("wechat:deleted")
    if "mp.weixin.qq.com" in url and any(token in (item.message or "") for token in ("环境异常", "参数错误")):
        reasons.append("wechat:block")
    if "github.com" in url and ("blob/" in url or "/tree/" in url):
        # GitHub file paths move frequently; only mark as weak candidate.
        reasons.append("github:path_may_move")
    if "github.com" in url and any(token in message for token in ("not found", "404")):
        reasons.append("github:404")
    if "feishu.cn" in url and any(token in message for token in ("没有权限", "forbidden", "403", "no access")):
        reasons.append("feishu:denied")
    return list(dict.fromkeys(reasons))


def write_missing_raw_delete_candidates(path: Path, manifest: dict[str, ManifestItem], repo_root: Path) -> None:
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    items = missing_raw_items(manifest)
    scored: list[tuple[int, ManifestItem, list[str]]] = []
    for item in items:
        reasons = deletion_reasons_for_missing_raw(item)
        score = 0
        if any(r in reasons for r in ("wechat:deleted", "access:denied", "github:404", "message:404", "label:404", "raw_path:404", "feishu:denied")):
            score += 3
        if "github:path_may_move" in reasons:
            score -= 1
        scored.append((score, item, reasons))
    scored.sort(key=lambda row: (-row[0], row[1].theme, row[1].label))

    definite = [row for row in scored if row[0] >= 3]
    maybe = [row for row in scored if 1 <= row[0] < 3]

    lines: list[str] = [
        "# raw 缺失删除候选（本地证据）",
        "",
        f"- 更新时间：{today}",
        f"- raw 缺失总量：{len(items)}",
        f"- 强候选：{len(definite)}",
        f"- 弱候选：{len(maybe)}",
        "",
        "- 说明：这里不做联网验证，只依据本地 `manifest.message` / 占位信息推断“很可能失效”。删前建议你肉眼确认一次。",
        "",
        "## 强候选（建议直接删）",
        "",
    ]
    if not definite:
        lines.append("- 无")
    else:
        for _, item, reasons in definite:
            lines.append(f"- `{item.theme}` | [{item.label}](<{item.url}>) | 原预期 raw：`{path_text(Path(item.raw_path), repo_root) if item.raw_path else 'missing'}`")
            lines.append(f"  理由：`{', '.join(reasons)}`")

    lines.extend(["", "## 弱候选（可能可恢复，先别删）", ""])
    if not maybe:
        lines.append("- 无")
    else:
        for _, item, reasons in maybe[:80]:
            lines.append(f"- `{item.theme}` | [{item.label}](<{item.url}>)")
            lines.append(f"  理由：`{', '.join(reasons)}`")

    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def main() -> int:
    args = parse_args()
    repo_root = args.repo_root.resolve()
    bookmark_files = args.inputs or sorted((repo_root / "raw").glob("bookmarks_articles_2026_4_18_part*.md"))

    entries = load_entries(bookmark_files)
    manifest_dir = repo_root / "raw" / "bookmarks-ingest"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = manifest_dir / "manifest.json"
    index_path = manifest_dir / "index.md"
    review_path = manifest_dir / "review.md"
    manifest = load_manifest(manifest_path)
    manifest = {
        url: item
        for url, item in manifest.items()
        if not JUNK_LABEL_PATTERN.search(item.label)
    }
    canonicalize_numbered_raw_dirs(repo_root, manifest)
    if args.archive_downloads:
        archive_downloads_into_raw(repo_root, manifest)
    correct_blocked_items_with_recovered_raw(manifest, repo_root)
    repair_missing_raw_paths(manifest, repo_root)
    if args.refetch_github_raw:
        refetch_github_raw(manifest, repo_root, limit=args.limit)
        canonicalize_numbered_raw_dirs(repo_root, manifest)

    if args.redownload_missing_raw:
        targets = [item for item in missing_raw_items(manifest) if not is_video_item(item)]
        targets.sort(key=missing_raw_priority)
        if args.limit > 0:
            targets = targets[: args.limit]

        for item in targets:
            entry = BookmarkEntry(
                source_file=item.source_file,
                part=0,
                theme=item.theme,
                label=item.label,
                url=item.url,
            )
            status, raw_path, source_note, message = run_entry(entry, repo_root)
            item.status = status
            item.raw_path = raw_path
            if source_note:
                item.source_note = source_note
            item.fetch_method = infer_fetch_method(detect_kind(item.url), message, raw_path)
            item.message = message
            item.updated = datetime.now().astimezone().isoformat(timespec="seconds")
            save_manifest(manifest_path, manifest)

    if args.tavily_fill_weak_content:
        raw_source_index = build_raw_source_index(repo_root)
        verified_states = build_verified_states(manifest, raw_source_index)
        weak_items = [
            item for item in manifest.values()
            if verified_states[item.url].bucket == "weak_content" and not is_video_item(item)
        ]
        weak_items.sort(key=missing_raw_priority)
        if args.limit > 0:
            weak_items = weak_items[: args.limit]
        for item in weak_items:
            entry = BookmarkEntry(
                source_file=item.source_file,
                part=0,
                theme=item.theme,
                label=item.label,
                url=item.url,
            )
            status, raw_path, source_note, message = run_entry(entry, repo_root, prefer_tavily=True)
            item.status = status
            item.raw_path = raw_path
            if source_note:
                item.source_note = source_note
            item.fetch_method = infer_fetch_method(detect_kind(item.url), message, raw_path)
            item.message = message
            item.updated = datetime.now().astimezone().isoformat(timespec="seconds")
            save_manifest(manifest_path, manifest)

    if args.tavily_fill_weak_content_selective:
        raw_source_index = build_raw_source_index(repo_root)
        verified_states = build_verified_states(manifest, raw_source_index)
        weak_items = [
            item
            for item in manifest.values()
            if verified_states[item.url].bucket == "weak_content"
            and not is_video_item(item)
            and detect_kind(item.url) != "pdf"
            and (item.fetch_method or "") != "tavily"
        ]
        weak_items.sort(key=missing_raw_priority)
        if args.limit > 0:
            weak_items = weak_items[: args.limit]
        for item in weak_items:
            entry = BookmarkEntry(
                source_file=item.source_file,
                part=0,
                theme=item.theme,
                label=item.label,
                url=item.url,
            )
            status, raw_path, source_note, message = run_entry(entry, repo_root, prefer_tavily=True)
            item.status = status
            item.raw_path = raw_path
            if source_note:
                item.source_note = source_note
            item.fetch_method = infer_fetch_method(detect_kind(item.url), message, raw_path)
            item.message = message
            item.updated = datetime.now().astimezone().isoformat(timespec="seconds")
            save_manifest(manifest_path, manifest)
    if args.refresh_source_notes or args.enrich_source_notes:
        refresh_and_enrich_source_notes(
            manifest,
            repo_root,
            refresh=args.refresh_source_notes,
            enrich_limit=args.enrich_source_notes,
        )
    if args.write_missing_raw_recover:
        # Deprecated: merged into review.md. Keep flag for compatibility.
        pass
    if args.write_missing_raw_delete_candidates:
        # Deprecated: merged into review.md. Keep flag for compatibility.
        pass
    save_manifest(manifest_path, manifest)

    # Keep a single review doc: remove legacy standalone files if present.
    (manifest_dir / "missing-raw-recover.md").unlink(missing_ok=True)
    (manifest_dir / "missing-raw-delete-candidates.md").unlink(missing_ok=True)

    # Always refresh views after any mutation mode.
    write_progress_index(index_path, manifest, repo_root, bookmark_files)
    write_theme_indexes(manifest_dir, manifest, repo_root)
    write_review_report(review_path, manifest, repo_root)

    eligible: list[BookmarkEntry] = []
    domain_filters = [item.strip().lower() for item in args.domains.split(",") if item.strip()]
    for entry in entries:
        item = ensure_manifest_item(entry, manifest)
        if args.theme and entry.theme != args.theme:
            continue
        if domain_filters and not any(filter_value in item.domain for filter_value in domain_filters):
            continue
        if item.status == "success":
            continue
        if item.status in {"blocked", "error", "unsupported"} and not args.retry_errors:
            continue
        eligible.append(entry)

    if args.prefer_non_wechat:
        eligible.sort(key=entry_priority)
    else:
        eligible.sort(key=lambda entry: (entry.part, entry.label))

    if args.limit > 0:
        eligible = eligible[: args.limit]

    for entry in eligible:
        item = ensure_manifest_item(entry, manifest)
        status, raw_path, source_note, message = run_entry(entry, repo_root)
        item.status = status
        item.raw_path = raw_path
        item.source_note = source_note
        item.fetch_method = infer_fetch_method(detect_kind(entry.url), message, raw_path)
        item.message = message
        item.updated = datetime.now().astimezone().isoformat(timespec="seconds")
        save_manifest(manifest_path, manifest)
        write_progress_index(index_path, manifest, repo_root, bookmark_files)
        write_theme_indexes(manifest_dir, manifest, repo_root)
        write_review_report(review_path, manifest, repo_root)
        save_manifest(manifest_path, manifest)
        print(f"{status:11} {entry.label} :: {entry.url}")

    if not eligible:
        write_progress_index(index_path, manifest, repo_root, bookmark_files)
        write_theme_indexes(manifest_dir, manifest, repo_root)
        write_review_report(review_path, manifest, repo_root)
        save_manifest(manifest_path, manifest)
        noise_path = manifest_dir / "noise.md"
        if noise_path.exists():
            noise_path.unlink()
        print("No eligible bookmark entries to process.")

    print(f"Manifest: {manifest_path}")
    print(f"Index: {index_path}")
    print(f"Review: {review_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
