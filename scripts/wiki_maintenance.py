#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import re
import unicodedata
import shutil
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Iterable

from topic_stats import parse_frontmatter
from topic_questions_report import render_supplement_prompts_report, render_topic_questions_report


ROOT = Path(__file__).resolve().parent.parent
WIKI = ROOT / "wiki"
RAW = ROOT / "raw"
REPORT_DIR = WIKI / "report"
DOWNLOAD_DIR = RAW / "download"
ANALYSES_DIR = WIKI / "analyses"
GRAPH_CONFIG_PATH = ROOT / ".obsidian" / "graph.json"
WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")

REPORT_TAGS = ["ai", "wiki", "report", "maintenance"]
REPORT_FILES = {
    "downloads": REPORT_DIR / "下载区入库巡检.md",
    "downloads_prune": REPORT_DIR / "下载区清理报告.md",
    "gaps": REPORT_DIR / "知识空白扫描.md",
    "health": REPORT_DIR / "主题健康度报告.md",
    "topic_questions": REPORT_DIR / "主题擅长问题前十.md",
    "supplement_prompts": REPORT_DIR / "待补强方向.md",
    "structure": REPORT_DIR / "维基目录治理报告.md",
    "analyses": REPORT_DIR / "分析维护报告.md",
    "graph": REPORT_DIR / "维基关系图谱.md",
    "hub": REPORT_DIR / "报告中心.md",
}
GRAPH_COLOR_GROUPS = [
    ("path:\"wiki/topics\"", 0xA64B2A),
    ("path:\"wiki/analyses/topics\"", 0xC7882A),
    ("path:\"wiki/analyses\"", 0x7D5BA6),
    ("path:\"wiki/sources\"", 0x567A9E),
    ("path:\"wiki/report\"", 0xC05B5B),
]
DIRECTORY_SCOPES = [
    ("topics", WIKI / "topics", False),
    ("analyses", ANALYSES_DIR, False),
    ("analyses/topics", ANALYSES_DIR / "topics", False),
    ("analyses/archive", ANALYSES_DIR / "archive", True),
    ("sources", WIKI / "sources", False),
    ("report", REPORT_DIR, False),
]
SECTION_GROUPS = {
    "scope": ("## 范围", "## 这是什么", "## 在本库中的角色"),
    "judgement": ("## 当前判断", "## 当前结论", "## 进一步判断", "## 新增判断"),
    "evidence": ("## 关键案例 / 证据", "## 已入库案例", "## 近期资料启发", "## 关键事实"),
    "analysis": ("## 分析锚点",),
    "links": ("## 关联页面", "## 主题入口"),
    "questions": ("## 未解决问题",),
}
SECTION_GUIDANCE = {
    "scope": "`## 这是什么` / `## 范围`",
    "judgement": "`## 当前判断` / `## 新增判断`",
    "evidence": "`## 关键案例 / 证据` / `## 已入库案例`",
    "analysis": "`## 分析锚点`",
    "links": "`## 关联页面`",
    "questions": "`## 未解决问题`",
}


@dataclass(frozen=True)
class Page:
    path: Path
    stem: str
    text: str
    fm: dict[str, object]
    links: list[str]


@dataclass(frozen=True)
class RawEntry:
    category: str
    path: Path
    rel_path: Path
    title: str
    kind: str


@dataclass(frozen=True)
class DownloadStatus:
    entry: RawEntry
    status: str
    source_title: str | None
    integrated: bool
    note: str


@dataclass(frozen=True)
class DownloadPruneResult:
    deleted: list[DownloadStatus]
    skipped: list[DownloadStatus]


@dataclass(frozen=True)
class HubMetric:
    hub_type: str
    title: str
    path: Path
    mounted_sources: int
    featured_total: int
    featured_source_notes: int
    analysis_count: int
    backlinks: int
    section_hits: int
    updated: str
    score: int
    grade: str
    gaps: list[str]


@dataclass(frozen=True)
class RawCoverage:
    category: str
    total_entries: int
    covered_entries: int
    uncovered_titles: list[str]

    @property
    def ratio(self) -> float:
        if self.total_entries == 0:
            return 1.0
        return self.covered_entries / self.total_entries


@dataclass(frozen=True)
class DirectoryMetric:
    key: str
    path: Path
    page_count: int
    inbound_links: int
    zero_backlink_pages: int
    recent_pages: int
    decision: str
    reason: str


@dataclass(frozen=True)
class RepoState:
    report_date: date
    all_pages: list[Page]
    source_pages: list[Page]
    topic_pages: list[Page]
    analysis_pages: list[Page]
    backlinks: Counter[str]
    non_source_backlinks: dict[str, set[str]]
    raw_entries_by_category: dict[str, list[RawEntry]]
    download_statuses: list[DownloadStatus]
    topic_metrics: list[HubMetric]
    raw_coverage: list[RawCoverage]
    seed_sources: list[Page]
    directory_metrics: list[DirectoryMetric]


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _iter_markdown_files(path: Path, recursive: bool = True) -> list[Path]:
    if not path.exists():
        return []
    pattern = "**/*.md" if recursive else "*.md"
    return sorted(p for p in path.glob(pattern) if p.is_file())


def _load_page(path: Path) -> Page:
    text = _read_text(path)
    return Page(
        path=path,
        stem=path.stem,
        text=text,
        fm=parse_frontmatter(text),
        links=WIKILINK_RE.findall(text),
    )


def _load_pages() -> tuple[list[Page], list[Page], list[Page], list[Page]]:
    all_pages = [_load_page(path) for path in _iter_markdown_files(WIKI)]
    source_pages = [page for page in all_pages if page.path.parent == WIKI / "sources"]
    topic_pages = [page for page in all_pages if page.path.parent == WIKI / "topics"]
    analysis_pages = [
        page
        for page in all_pages
        if page.path.parent == ANALYSES_DIR and page.fm.get("type") == "analysis"
    ]
    return all_pages, source_pages, topic_pages, analysis_pages


def _page_rel(path: Path) -> str:
    return str(path.relative_to(ROOT))


def _normalize_title(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).casefold().strip()
    normalized = re.sub(r"\.(md|markdown|pdf|txt)$", "", normalized)
    normalized = re.sub(r"[\s\"'`“”‘’《》》【】\[\]\(\)\{\}:：,，.。!！?？|\\/·—_-]+", "", normalized)
    return normalized


def _parse_date(value: object) -> date | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def _fdate(value: object, fallback: str = "-") -> str:
    parsed = _parse_date(value)
    return parsed.isoformat() if parsed else fallback


def _parse_bool(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes", "1"}:
            return True
        if lowered in {"false", "no", "0"}:
            return False
    return default


def _is_source_page(page: Page) -> bool:
    return page.path.parent == WIKI / "sources"


def _is_excluded_backlink_source(page: Page) -> bool:
    return page.path.name in {"index.md", "log.md"}


def _build_backlinks(all_pages: list[Page]) -> tuple[Counter[str], dict[str, set[str]]]:
    known_titles = {page.stem for page in all_pages}
    backlinks: Counter[str] = Counter()
    non_source_backlinks: dict[str, set[str]] = defaultdict(set)
    for page in all_pages:
        for link in page.links:
            if link not in known_titles:
                continue
            backlinks[link] += 1
            if not _is_source_page(page) and not _is_excluded_backlink_source(page):
                non_source_backlinks[link].add(_page_rel(page.path))
    return backlinks, non_source_backlinks


def _frontmatter_links(page: Page) -> list[str]:
    raw = page.fm.get("sources")
    if isinstance(raw, list):
        return [str(item).strip() for item in raw]
    return []


def _featured_source_note_count(page: Page, source_titles: set[str]) -> tuple[int, int]:
    featured = _frontmatter_links(page)
    source_note_count = 0
    for item in featured:
        match = re.match(r"^\[\[(.+?)\]\]$", item)
        title = match.group(1).strip() if match else item.strip()
        if title in source_titles:
            source_note_count += 1
    return len(featured), source_note_count


def _section_hits(text: str) -> int:
    return sum(any(marker in text for marker in markers) for markers in SECTION_GROUPS.values())


def _missing_section_groups(text: str) -> list[str]:
    return [name for name, markers in SECTION_GROUPS.items() if not any(marker in text for marker in markers)]


def _freshness_score(updated: str, report_date: date) -> int:
    parsed = _parse_date(updated)
    if parsed is None:
        return 0
    age = (report_date - parsed).days
    if age <= 7:
        return 10
    if age <= 30:
        return 8
    if age <= 90:
        return 6
    if age <= 180:
        return 4
    return 2


def _grade(score: int) -> str:
    if score >= 90:
        return "A"
    if score >= 80:
        return "B"
    if score >= 70:
        return "C"
    if score >= 60:
        return "D"
    return "E"


def _hub_metric(
    hub_type: str,
    page: Page,
    source_pages: list[Page],
    source_titles: set[str],
    analysis_pages: list[Page],
    backlinks: dict[str, set[str]],
    report_date: date,
) -> HubMetric:
    mounted_sources = 0
    for source_page in source_pages:
        links = _frontmatter_links(source_page)
        if f"[[{page.stem}]]" in links or page.stem in links:
            mounted_sources += 1

    featured_total, featured_source_notes = _featured_source_note_count(page, source_titles)
    analysis_count = len(
        {
            analysis_page.stem
            for analysis_page in analysis_pages
            if page.stem in analysis_page.links or str(analysis_page.fm.get("topic") or "").strip() == page.stem
        }
    )
    inbound = len(backlinks.get(page.stem, set()))
    section_hits = _section_hits(page.text)
    updated = _fdate(page.fm.get("updated"))

    score = 0
    score += round(min(mounted_sources, 20) / 20 * 30)
    score += round(min(featured_source_notes, 8) / 8 * 15)
    score += round(min(analysis_count, 4) / 4 * 20)
    score += round(min(inbound, 10) / 10 * 10)
    score += round(section_hits / len(SECTION_GROUPS) * 15)
    score += _freshness_score(updated, report_date)
    score = min(score, 100)

    gaps: list[str] = []
    if mounted_sources == 0:
        gaps.append("缺 source 挂载")
    elif mounted_sources < 5:
        gaps.append("source 证据偏薄")
    if analysis_count == 0:
        gaps.append("缺 analysis 锚点")
    if section_hits < 4:
        gaps.append("正文结构偏薄")
    if inbound < 3:
        gaps.append("被引用偏少")
    if _freshness_score(updated, report_date) <= 4:
        gaps.append("近期未复核")
    if not gaps:
        gaps.append("结构完整")

    return HubMetric(
        hub_type=hub_type,
        title=page.stem,
        path=page.path,
        mounted_sources=mounted_sources,
        featured_total=featured_total,
        featured_source_notes=featured_source_notes,
        analysis_count=analysis_count,
        backlinks=inbound,
        section_hits=section_hits,
        updated=updated,
        score=score,
        grade=_grade(score),
        gaps=gaps[:2],
    )


def _collect_raw_entries() -> dict[str, list[RawEntry]]:
    entries: dict[str, list[RawEntry]] = {}
    if not RAW.exists():
        return entries

    for category_dir in sorted(p for p in RAW.iterdir() if p.is_dir() and not p.name.startswith(".")):
        category_entries: list[RawEntry] = []
        for child in sorted(p for p in category_dir.iterdir() if not p.name.startswith(".")):
            if child.is_dir():
                kind = "dir"
                title = child.name
            else:
                kind = child.suffix.lstrip(".") or "file"
                title = child.stem
            category_entries.append(
                RawEntry(
                    category=category_dir.name,
                    path=child,
                    rel_path=child.relative_to(ROOT),
                    title=title,
                    kind=kind,
                )
            )
        entries[category_dir.name] = category_entries
    return entries


def _entry_is_referenced(source_page: Page, entry: RawEntry) -> bool:
    rel = entry.rel_path.as_posix()
    if entry.path.is_dir():
        return f"{rel}/" in source_page.text or rel in source_page.text
    return rel in source_page.text


def _match_download_entry(entry: RawEntry, source_pages: list[Page]) -> Page | None:
    by_normalized: dict[str, Page] = {}
    for page in source_pages:
        by_normalized.setdefault(_normalize_title(page.stem), page)

    title_match = by_normalized.get(_normalize_title(entry.title))
    if title_match is not None:
        return title_match

    for page in source_pages:
        if _entry_is_referenced(page, entry):
            return page
    return None


def _collect_download_statuses(
    raw_entries_by_category: dict[str, list[RawEntry]],
    source_pages: list[Page],
    integrated_titles: set[str],
) -> list[DownloadStatus]:
    rows: list[DownloadStatus] = []
    for entry in raw_entries_by_category.get("download", []):
        source_page = _match_download_entry(entry, source_pages)
        if source_page is None:
            status = "待建 source"
            note = "raw/download 已有条目，但还没有对应 source note。"
            integrated = False
            source_title = None
        else:
            integrated = source_page.stem in integrated_titles
            source_title = source_page.stem
            if integrated:
                status = "已入库"
                note = "已存在 source note，且已被 topic/analysis 接住。"
            else:
                status = "待整合"
                note = "已有 source note，但还没形成稳定主线链接。"

        rows.append(
            DownloadStatus(
                entry=entry,
                status=status,
                source_title=source_title,
                integrated=integrated,
                note=note,
            )
        )

    order = {"待建 source": 0, "待整合": 1, "已入库": 2}
    rows.sort(key=lambda row: (order.get(row.status, 99), row.entry.title))
    return rows


def _remove_entry_path(path: Path) -> None:
    if not path.exists():
        return
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


def _prune_download_entries(state: RepoState) -> DownloadPruneResult:
    deleted: list[DownloadStatus] = []
    skipped: list[DownloadStatus] = []
    for row in state.download_statuses:
        if row.status == "已入库":
            _remove_entry_path(row.entry.path)
            deleted.append(row)
        else:
            skipped.append(row)
    return DownloadPruneResult(deleted=deleted, skipped=skipped)


def _collect_raw_coverage(
    raw_entries_by_category: dict[str, list[RawEntry]],
    source_pages: list[Page],
) -> list[RawCoverage]:
    coverage: list[RawCoverage] = []
    for category, entries in sorted(raw_entries_by_category.items()):
        covered: list[str] = []
        uncovered: list[str] = []
        for entry in entries:
            if any(_entry_is_referenced(source_page, entry) for source_page in source_pages):
                covered.append(entry.title)
            else:
                uncovered.append(entry.title)
        coverage.append(
            RawCoverage(
                category=category,
                total_entries=len(entries),
                covered_entries=len(covered),
                uncovered_titles=uncovered[:8],
            )
        )
    coverage.sort(key=lambda item: (item.ratio, item.category))
    return coverage


def _directory_decision(metric_key: str, page_count: int, zero_ratio: float, avg_links: float) -> tuple[str, str]:
    if metric_key == "sources":
        if zero_ratio > 0.2:
            return "复核", "source 页开始积累孤点，应优先看整合是否回退。"
        return "保留", "source 层仍是 ingest 到主线回写的缓冲层，当前可继续保留。"
    if metric_key == "topics":
        if avg_links < 4:
            return "复核", "topic 是知识库主入口，若引用偏少要补交叉链接。"
        return "保留", "topic 仍承担主线判断与导航，不建议并入 sources。"
    if metric_key == "analyses":
        return "保留", "analysis 叶子页已转成元数据驱动，保留为深度研究层。"
    if metric_key == "analyses/topics":
        return "保留", "analysis 主题页已成为第二入口，不建议再打回扁平列表。"
    if metric_key == "analyses/archive":
        if zero_ratio >= 0.8:
            return "复核", "archive 更像冷存储，不适合作主入口；建议继续收紧归档规则。"
        return "观察", "archive 可保留，但应避免成为新的内容黑洞。"
    if metric_key == "report":
        return "保留", "report 层用于日常巡检与评分，正是这次需要新增的运维层。"
    return "观察", "建议继续观察目录职责是否稳定。"


def _collect_directory_metrics(
    all_pages: list[Page],
    backlinks: Counter[str],
    report_date: date,
) -> list[DirectoryMetric]:
    metrics: list[DirectoryMetric] = []
    for key, base, recursive in DIRECTORY_SCOPES:
        pages = [_load_page(path) for path in _iter_markdown_files(base, recursive=recursive)]
        page_count = len(pages)
        inbound_links = sum(backlinks.get(page.stem, 0) for page in pages)
        zero_backlink_pages = sum(backlinks.get(page.stem, 0) == 0 for page in pages)
        recent_pages = 0
        for page in pages:
            updated = _parse_date(page.fm.get("updated"))
            if updated is not None and (report_date - updated).days <= 30:
                recent_pages += 1

        zero_ratio = zero_backlink_pages / page_count if page_count else 0.0
        avg_links = inbound_links / page_count if page_count else 0.0
        decision, reason = _directory_decision(key, page_count, zero_ratio, avg_links)
        metrics.append(
            DirectoryMetric(
                key=key,
                path=base,
                page_count=page_count,
                inbound_links=inbound_links,
                zero_backlink_pages=zero_backlink_pages,
                recent_pages=recent_pages,
                decision=decision,
                reason=reason,
            )
        )
    return metrics


def build_state(report_date: date) -> RepoState:
    all_pages, source_pages, topic_pages, analysis_pages = _load_pages()
    backlinks, non_source_backlinks = _build_backlinks(all_pages)
    integrated_titles = {page.stem for page in source_pages if non_source_backlinks.get(page.stem)}
    raw_entries_by_category = _collect_raw_entries()
    source_titles = {page.stem for page in source_pages}

    topic_metrics = sorted(
        (
            _hub_metric("topic", page, source_pages, source_titles, analysis_pages, non_source_backlinks, report_date)
            for page in topic_pages
        ),
        key=lambda item: (-item.score, item.title),
    )
    seed_sources = sorted(
        [page for page in source_pages if str(page.fm.get("status") or "").strip() == "seed"],
        key=lambda item: (_fdate(item.fm.get("updated"), "9999-99-99"), item.stem),
        reverse=True,
    )

    return RepoState(
        report_date=report_date,
        all_pages=all_pages,
        source_pages=source_pages,
        topic_pages=topic_pages,
        analysis_pages=analysis_pages,
        backlinks=backlinks,
        non_source_backlinks=non_source_backlinks,
        raw_entries_by_category=raw_entries_by_category,
        download_statuses=_collect_download_statuses(raw_entries_by_category, source_pages, integrated_titles),
        topic_metrics=topic_metrics,
        raw_coverage=_collect_raw_coverage(raw_entries_by_category, source_pages),
        seed_sources=seed_sources,
        directory_metrics=_collect_directory_metrics(all_pages, backlinks, report_date),
    )


def _escape_cell(value: object) -> str:
    return str(value).replace("|", "\\|")


def _markdown_table(headers: list[str], rows: Iterable[Iterable[object]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(_escape_cell(cell) for cell in row) + " |")
    return "\n".join(lines)


def _frontmatter(title: str, created: str, updated: str) -> str:
    lines = [
        "---",
        f"title: {title}",
        "type: analysis",
        "status: active",
        f"created: {created}",
        f"updated: {updated}",
        "tags:",
    ]
    for tag in REPORT_TAGS:
        lines.append(f"  - {tag}")
    lines.append("sources: []")
    lines.append("---")
    return "\n".join(lines) + "\n\n"


def _existing_created(path: Path, fallback: str) -> str:
    if not path.exists():
        return fallback
    return _fdate(parse_frontmatter(_read_text(path)).get("created"), fallback)


def _write_report(path: Path, title: str, body: str, report_date: date) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    created = _existing_created(path, report_date.isoformat())
    updated = report_date.isoformat()
    content = _frontmatter(title, created, updated) + body.rstrip() + "\n"
    path.write_text(content, encoding="utf-8")


def render_downloads_report(state: RepoState) -> str:
    rows = state.download_statuses
    need_create = sum(row.status == "待建 source" for row in rows)
    need_integrate = sum(row.status == "待整合" for row in rows)
    ready = sum(row.status == "已入库" for row in rows)

    table_rows = []
    for row in rows:
        table_rows.append(
            [
                row.entry.title,
                row.entry.kind,
                row.status,
                row.source_title or "-",
                _page_rel(row.entry.path),
            ]
        )

    lines = [
        "# 下载区入库巡检",
        "",
        "## 当前结论",
        "",
        f"- `raw/download` 当前共有 `{len(rows)}` 个条目。",
        f"- 已完成入库：`{ready}`",
        f"- 待建 source note：`{need_create}`",
        f"- 已有 source 但待整合：`{need_integrate}`",
        "",
        "## 处理原则",
        "",
        "- `待建 source`：先读原始材料，再新建 `wiki/sources/`，不要直接生成空壳页。",
        "- `待整合`：优先回写到 `wiki/topics/` 或 `wiki/overview.md`，避免 source note 停留在孤立摘要层。",
        "",
        "## 当前清单",
        "",
        _markdown_table(["条目", "类型", "状态", "source note", "raw 路径"], table_rows),
    ]
    return "\n".join(lines) + "\n"


def render_downloads_prune_report(state: RepoState, result: DownloadPruneResult, dry_run: bool = False) -> str:
    deleted_rows = [
        [row.entry.title, row.entry.kind, row.source_title or "-", _page_rel(row.entry.path)]
        for row in result.deleted
    ]
    skipped_rows = [
        [row.entry.title, row.status, row.note, _page_rel(row.entry.path)]
        for row in result.skipped
    ]

    lines = [
        "# 下载区清理报告",
        "",
        "## 当前结论",
        "",
        f"- 清理模式：`{'dry-run' if dry_run else 'write'}`",
        f"- 已删除已入库条目：`{len(result.deleted)}`",
        f"- 保留未入库条目：`{len(result.skipped)}`",
        "",
        "## 清理原则",
        "",
        "- 只删除 `status == 已入库` 的 `raw/download` 条目。",
        "- `待建 source` / `待整合` 一律保留，避免误删仍需处理的资料。",
        "- 清理后，`raw/download` 应保持为空或只留下少量人工暂存文件。",
        "",
        "## 已删除条目",
        "",
    ]
    if deleted_rows:
        lines.append(_markdown_table(["条目", "类型", "source note", "raw 路径"], deleted_rows))
    else:
        lines.append("- 当前没有可删除的已入库下载条目。")

    lines.extend(
        [
            "",
            "## 保留条目",
            "",
        ]
    )
    if skipped_rows:
        lines.append(_markdown_table(["条目", "状态", "说明", "raw 路径"], skipped_rows))
    else:
        lines.append("- 当前没有需要保留的待处理下载条目。")

    lines.extend(
        [
            "",
            "## 使用方式",
            "",
            "- 先跑 `python3 scripts/wiki_maintenance.py downloads --write` 确认状态，再跑 `python3 scripts/wiki_maintenance.py prune-downloads --write` 执行清理。",
            "- 清理完成后，可重新跑一次 `downloads --write` 刷新巡检报告。",
            "",
            f"最后刷新日期：`{state.report_date.isoformat()}`",
        ]
    )
    return "\n".join(lines) + "\n"


def render_health_report(state: RepoState) -> str:
    table_rows = []
    focus_lines: list[str] = []
    push_lines: list[str] = []
    page_by_title = {page.stem: page for page in state.topic_pages}
    for metric in sorted(state.topic_metrics, key=lambda item: (-item.score, item.title)):
        table_rows.append(
            [
                f"[[{metric.title}]]",
                metric.score,
                metric.grade,
                metric.mounted_sources,
                f"{metric.featured_source_notes}/{metric.featured_total}",
                metric.analysis_count,
                metric.backlinks,
                metric.updated,
                "；".join(metric.gaps),
            ]
        )
        if metric.score < 70 or metric.mounted_sources == 0:
            focus_lines.append(
                f"- [[{metric.title}]]：{metric.score} 分，短板是 {', '.join(metric.gaps)}。"
            )
        elif 85 <= metric.score < 100:
            actions: list[str] = []
            page = page_by_title.get(metric.title)
            if metric.mounted_sources < 20:
                actions.append(f"再补 `{20 - metric.mounted_sources}` 个 source 挂载")
            if metric.featured_source_notes < 8:
                actions.append(f"frontmatter 再补 `{8 - metric.featured_source_notes}` 个精选 source")
            if metric.analysis_count < 4:
                actions.append(f"再补 `{4 - metric.analysis_count}` 个 analysis 锚点")
            if metric.backlinks < 10:
                actions.append(f"再补 `{10 - metric.backlinks}` 个非 source 引用入口")
            if page is not None:
                missing_groups = _missing_section_groups(page.text)
                if missing_groups:
                    labels = [SECTION_GUIDANCE[name] for name in missing_groups if name in SECTION_GUIDANCE]
                    if labels:
                        actions.append("补 " + "、".join(labels))
            if _freshness_score(metric.updated, state.report_date) < 10:
                actions.append("更新 `updated` 并做一轮复核")
            if actions:
                push_lines.append(
                    f"- [[{metric.title}]]：离 `100` 还差 `{100 - metric.score}` 分，建议 {'；'.join(actions[:3])}。"
                )

    lines = [
        "# 主题健康度报告",
        "",
        "## 评分口径",
        "",
        "- `source 挂载` 30 分：该 topic 被多少 source note 直接挂载。",
        "- `精选证据` 15 分：frontmatter 里真实引用了多少 source note。",
        "- `analysis 锚点` 20 分：是否已经长出 analysis 支撑。",
        "- `被引用情况` 10 分：该主题是否真的成为别的页面入口。",
        "- `正文结构` 15 分：是否具备范围、判断、证据、分析锚点、关联页面等关键段落。",
        "- `新鲜度` 10 分：最近是否被复核过。",
        "",
        "## Topic 评分表",
        "",
        _markdown_table(
            ["主题", "分数", "等级", "挂载来源", "精选来源", "分析锚点", "入链数", "更新日期", "主要短板"],
            table_rows,
        ),
        "",
        "## 优先补厚对象",
        "",
    ]
    if focus_lines:
        lines.extend(focus_lines[:8])
    else:
        lines.append("- 当前所有 topic 都过了最低健康阈值。")
    lines.extend(
        [
            "",
            "## 冲顶建议",
            "",
        ]
    )
    if push_lines:
        lines.extend(push_lines[:8])
    else:
        lines.append("- 当前没有需要专门冲顶的高分 topic。")
    return "\n".join(lines) + "\n"


def render_gaps_report(state: RepoState) -> str:
    weak_topics = [metric for metric in state.topic_metrics if metric.score < 70 or metric.mounted_sources == 0]
    pending_downloads = [row for row in state.download_statuses if row.status != "已入库"]
    lowest_coverage = state.raw_coverage[:5]

    lines = [
        "# 知识空白扫描",
        "",
        "## 当前结论",
        "",
        f"- 待处理下载条目：`{len(pending_downloads)}`",
        f"- 低健康 topic：`{len(weak_topics)}`",
        f"- 仍处于 `seed` 状态的 source note：`{len(state.seed_sources)}`",
        "",
        "## 待补下载区",
        "",
    ]
    if pending_downloads:
        for row in pending_downloads[:10]:
            lines.append(f"- `{row.entry.title}`：{row.status}，{row.note}")
    else:
        lines.append("- `raw/download` 当前没有积压。")

    lines.extend(
        [
            "",
            "## 主题空白",
            "",
        ]
    )
    if weak_topics:
        for metric in weak_topics[:8]:
            lines.append(
                f"- [[{metric.title}]]：{metric.score} 分，短板是 {', '.join(metric.gaps)}。"
            )
    else:
        lines.append("- 当前没有明显掉到阈值以下的 topic。")

    lines.extend(
        [
            "",
            "## Source 质量待补",
            "",
        ]
    )
    if state.seed_sources:
        for page in state.seed_sources[:10]:
            lines.append(f"- [[{page.stem}]]：仍是 `seed`，建议优先压成可回写的 source note。")
    else:
        lines.append("- 当前没有 `seed` 状态的 source note。")

    coverage_rows = [
        [
            item.category,
            item.covered_entries,
            item.total_entries,
            f"{item.ratio:.0%}",
            "、".join(item.uncovered_titles[:4]) or "-",
        ]
        for item in lowest_coverage
    ]
    lines.extend(
        [
            "",
            "## Raw 分类覆盖率",
            "",
            _markdown_table(["raw 分类", "已覆盖", "总条目", "覆盖率", "未覆盖样本"], coverage_rows),
        ]
    )
    return "\n".join(lines) + "\n"


def render_structure_report(state: RepoState) -> str:
    rows = [
        [
            metric.key,
            metric.page_count,
            metric.inbound_links,
            metric.zero_backlink_pages,
            metric.recent_pages,
            metric.decision,
            metric.reason,
        ]
        for metric in state.directory_metrics
    ]

    lines = [
        "# 维基目录治理报告",
        "",
        "## 目录判断",
        "",
        _markdown_table(
            ["目录", "页数", "入链数", "零入链页", "30 天内更新", "结论", "说明"],
            rows,
        ),
        "",
        "## 当前建议",
        "",
        "- `wiki/topics/`、`wiki/analyses/` 仍然有清晰分工，不建议合并。",
        "- `wiki/analyses/archive/` 适合继续保留为冷存储，但不应承担发现入口；归档前要更明确“为什么归档”。",
        "- `wiki/report/` 建议正式纳入维护层，用来承接巡检、评分、空白和治理报告。",
        "- 不是目录级问题，但 `wiki/index.md` 与 `wiki/全索引.md` 职责相近，后续要持续守住“入口页 vs 全目录”的边界，否则会重复维护。",
    ]
    return "\n".join(lines) + "\n"


def _analysis_topic_rows(state: RepoState) -> list[list[object]]:
    grouped: dict[str, list[Page]] = defaultdict(list)
    for page in state.analysis_pages:
        topic = str(page.fm.get("topic") or "未分类").strip() or "未分类"
        grouped[topic].append(page)

    rows: list[list[object]] = []
    for topic, pages in sorted(grouped.items()):
        backbones = sum(str(page.fm.get("kind") or "").strip() == "backbone" for page in pages)
        slices = sum(str(page.fm.get("kind") or "").strip() == "slice" for page in pages)
        governance = sum(str(page.fm.get("kind") or "").strip() == "governance" for page in pages)
        snapshots = sum(str(page.fm.get("kind") or "").strip() == "snapshot" for page in pages)
        evergreen = sum(_parse_bool(page.fm.get("evergreen")) for page in pages)
        rows.append([topic, len(pages), backbones, slices, governance, snapshots, evergreen])
    rows.sort(key=lambda row: (-int(row[1]), str(row[0])))
    return rows


def _analysis_archive_candidates(state: RepoState) -> tuple[list[Page], list[Page]]:
    archive_now: list[Page] = []
    review_next: list[Page] = []
    for page in state.analysis_pages:
        kind = str(page.fm.get("kind") or "").strip()
        evergreen = _parse_bool(page.fm.get("evergreen"))
        title = str(page.fm.get("title") or page.stem).strip()
        if kind == "snapshot" and not evergreen:
            if "已完成" in page.path.stem or "理解中的三大空白" in title:
                archive_now.append(page)
            else:
                review_next.append(page)
        elif kind == "governance" and not evergreen:
            review_next.append(page)

    archive_now.sort(key=lambda page: (_fdate(page.fm.get("updated"), "9999-99-99"), page.stem), reverse=True)
    review_next.sort(key=lambda page: (_fdate(page.fm.get("updated"), "9999-99-99"), page.stem), reverse=True)
    return archive_now, review_next


def render_analyses_report(state: RepoState) -> str:
    topic_rows = _analysis_topic_rows(state)
    archive_now, review_next = _analysis_archive_candidates(state)
    root_total = len(state.analysis_pages)
    archive_pages = [_load_page(path) for path in _iter_markdown_files(ANALYSES_DIR / "archive")]
    archive_total = len(archive_pages)
    archived_completed_snapshots = sum(
        "已完成" in page.path.stem and str(page.fm.get("kind") or "").strip() == "snapshot"
        for page in archive_pages
    )
    snapshot_total = sum(str(page.fm.get("kind") or "").strip() == "snapshot" for page in state.analysis_pages)
    governance_total = sum(str(page.fm.get("kind") or "").strip() == "governance" for page in state.analysis_pages)
    backbone_total = sum(str(page.fm.get("kind") or "").strip() == "backbone" for page in state.analysis_pages)
    display_rows = [
        [
            row[0],
            row[1],
            row[2],
            row[3],
            row[4],
            row[5],
            row[6],
        ]
        for row in topic_rows
    ]

    lines = [
        "# 分析维护报告",
        "",
        "## 当前诊断",
        "",
        f"- `wiki/analyses/` 根目录当前有 `{root_total}` 个叶子分析页。",
        f"- 其中 `主线总纲={backbone_total}`，`治理页={governance_total}`，`阶段快照={snapshot_total}`。",
        f"- `archive/` 当前有 `{archive_total}` 个归档页。",
        f"- 现在的主要混乱点不是缺主题层，而是 `常青分析 / 治理页 / 阶段快照` 仍然同时堆在根目录。",
        "",
        "## 维护规则",
        "",
        "- 根目录只保留仍会被反复引用的活跃叶子页：`主线总纲 / 专题切片 / 治理页`，以及还没完成使命的少量 `阶段快照`。",
        "- `阶段快照` 只回答阶段性问题；一旦“空白已补齐 / 阶段目标已完成 / 新报告层已接手”，就应转入 `wiki/analyses/archive/`。",
        "- 主题入口只维护在 `wiki/analyses/topics/` 与 `wiki/analyses/主题索引.md`，不要再手写第二套目录。",
        "- 每次新增、归档、改 metadata 后，都要重跑 `python3 scripts/render_analyses_views.py`。",
        "",
        "## 主题分布",
        "",
        _markdown_table(
            ["主题", "总数", "主线总纲", "专题切片", "治理页", "阶段快照", "常青页"],
            display_rows,
        ),
        "",
        "## 建议立即归档",
        "",
    ]

    if archive_now:
        for page in archive_now:
            lines.append(
                f"- [[{page.stem}]]：`阶段快照 + 常青=false`，且文件名已标记 `已完成`，继续留在根目录只会增加视觉噪声。"
            )
    else:
        lines.append("- 当前没有明确应立即归档的 analysis 页。")

    lines.extend(
        [
            "",
            "## 下轮复核对象",
            "",
        ]
    )
    if review_next:
        for page in review_next[:8]:
            kind = str(page.fm.get("kind") or "").strip()
            lines.append(
                f"- [[{page.stem}]]：`{kind}` 且 `常青=false`，建议下一轮判断是继续保留、并入治理页，还是转归档。"
            )
    else:
        lines.append("- 当前没有额外的阶段性页需要特别复核。")

    lines.extend(
        [
            "",
            "## 对这个仓库的具体建议",
            "",
            "- `当前知识库如何继续变强（评分版）` 更像阶段快照；现在既然已有 `wiki/report/`，它后续更适合作为一段时间后的归档候选，而不是长期留在根目录。",
        "- `分析内容质量评分与重整建议` 与 `主题质量追踪与例行复核` 应保留在根目录，它们属于仍在承重的治理页。",
            "- `archive/` 不应只是冷冻箱；每次归档时，应该在报告里写清“为什么归档、被哪一页替代”。",
        ]
    )
    if archived_completed_snapshots:
        lines.insert(
            len(lines) - 3,
            f"- 4 篇 `当前 ... 理解中的三大空白（已完成）` 已完成首轮归档；后续同类 snapshot 也按这条规则处理。",
        )
    else:
        lines.insert(
            len(lines) - 3,
            "- 第一批最该归档的是 4 篇 `当前 ... 理解中的三大空白（已完成）`，因为它们已经完成了“暴露空白 -> 引出新页”的使命。",
        )
    return "\n".join(lines) + "\n"


def build_graph_config() -> dict[str, object]:
    if GRAPH_CONFIG_PATH.exists():
        try:
            data = json.loads(_read_text(GRAPH_CONFIG_PATH))
        except json.JSONDecodeError:
            data = {}
    else:
        data = {}

    data["search"] = 'path:"wiki"'
    data["showTags"] = False
    data["showAttachments"] = False
    data["showOrphans"] = True
    data["collapse-color-groups"] = True
    data["colorGroups"] = [
        {"query": query, "color": {"a": 1, "rgb": rgb}}
        for query, rgb in GRAPH_COLOR_GROUPS
    ]
    return data


def render_graph_report() -> str:
    group_rows = [[query, f"#{rgb:06X}"] for query, rgb in GRAPH_COLOR_GROUPS]
    lines = [
        "# 维基关系图谱",
        "",
        "## 当前预设",
        "",
        "- 全局图默认搜索范围：`path:\"wiki\"`，避免 `raw/` 与附件把图谱淹没。",
        "- 已按 `主题 / 分析主题 / 分析 / 来源 / 报告` 分组着色。",
        "- `scripts/` 本身是代码目录，不会直接进图谱；对应的可见入口是 [[脚本目录导航]]。",
        "- 保留 `show orphans = true`，方便继续发现孤页。",
        "",
        "## 颜色分组",
        "",
        _markdown_table(["查询", "颜色"], group_rows),
        "",
        "## 建议作为图谱入口的页面",
        "",
        "- [[overview|AI 教育与学习能力库总览]]",
        "- [[全索引]]",
        "- [[分析主题索引]]",
        "- [[报告中心]]",
        "- [[脚本目录导航]]",
        "",
        "## 本地图建议",
        "",
        "- 想看单主题结构时，从具体 topic 页打开本地图，而不是直接在全局图里缩放。",
        "- 想看治理关系时，从 [[报告中心]] 或 [[AI时代如何学习]] 进入图谱更清楚。",
    ]
    return "\n".join(lines) + "\n"


def render_report_hub(report_date: date) -> str:
    lines = [
        "# 报告中心",
        "",
        "本目录承接知识库的日常巡检、评分、目录治理和图谱配置，不再把这些结果散落在聊天里。",
        "",
        "## 固定报告",
        "",
        "- [[下载区入库巡检]]",
        "- [[下载区清理报告]]",
        "- [[知识空白扫描]]",
        "- [[主题健康度报告]]",
        "- [[主题擅长问题前十]]",
        "- [[待补强方向]]",
        "- [[维基目录治理报告]]",
        "- [[分析维护报告]]",
        "- [[维基关系图谱]]",
        "- [[脚本目录导航]]",
        "",
        "## Codex 触发短语",
        "",
        "- `巡检下载区并入库`：先跑下载区巡检，再处理 `raw/download` 的积压。",
        "- `清理已入库下载`：删除已经完成入库的 `raw/download` 条目。",
        "- `做一轮知识空白扫描`：生成当前需要补强的空白列表。",
        "- `生成主题健康报告`：重算 topic 健康度评分。",
        "- `生成主题擅长问题前十`：为每个主题生成最适合追问的问题，并同步生成 Deep Research 补强提示词。",
        "- `生成目录治理报告`：检查 wiki 目录是否还值得保留、是否需要收口。",
        "- `生成分析维护报告`：检查哪些 analysis 应留根目录、哪些应归档。",
        "- `刷新维基图谱`：更新图谱着色和图谱说明页。",
        "- `跑一轮知识库日常维护`：一次性执行全部巡检。",
        "",
        "## CLI 入口",
        "",
        "```bash",
        "python3 scripts/wiki_maintenance.py downloads --write",
        "python3 scripts/wiki_maintenance.py prune-downloads --write",
        "python3 scripts/wiki_maintenance.py gaps --write",
        "python3 scripts/wiki_maintenance.py health --write",
        "python3 scripts/wiki_maintenance.py questions --write",
        "python3 scripts/wiki_maintenance.py structure --write",
        "python3 scripts/wiki_maintenance.py analyses --write",
        "python3 scripts/wiki_maintenance.py graph --write",
        "python3 scripts/wiki_maintenance.py all --write",
        "```",
        "",
        f"最后刷新日期：`{report_date.isoformat()}`",
    ]
    return "\n".join(lines) + "\n"


def write_all_reports(state: RepoState) -> None:
    _write_report(REPORT_FILES["downloads"], "下载区入库巡检", render_downloads_report(state), state.report_date)
    _write_report(REPORT_FILES["gaps"], "知识空白扫描", render_gaps_report(state), state.report_date)
    _write_report(REPORT_FILES["health"], "主题健康度报告", render_health_report(state), state.report_date)
    _write_report(
        REPORT_FILES["topic_questions"],
        "主题擅长问题前十",
        render_topic_questions_report(state.report_date),
        state.report_date,
    )
    _write_report(
        REPORT_FILES["supplement_prompts"],
        "待补强方向",
        render_supplement_prompts_report(state.report_date),
        state.report_date,
    )
    _write_report(REPORT_FILES["structure"], "维基目录治理报告", render_structure_report(state), state.report_date)
    _write_report(REPORT_FILES["analyses"], "分析维护报告", render_analyses_report(state), state.report_date)
    _write_report(REPORT_FILES["graph"], "维基关系图谱", render_graph_report(), state.report_date)
    _write_report(REPORT_FILES["hub"], "报告中心", render_report_hub(state.report_date), state.report_date)


def write_graph_config() -> None:
    GRAPH_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    GRAPH_CONFIG_PATH.write_text(
        json.dumps(build_graph_config(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _summary_downloads(state: RepoState) -> str:
    counts = Counter(row.status for row in state.download_statuses)
    return (
        f"downloads: total={len(state.download_statuses)}, "
        f"待建 source={counts.get('待建 source', 0)}, "
        f"待整合={counts.get('待整合', 0)}, "
        f"已入库={counts.get('已入库', 0)}"
    )


def _summary_download_prune(result: DownloadPruneResult) -> str:
    return f"prune-downloads: deleted={len(result.deleted)}, kept={len(result.skipped)}"


def _summary_health(state: RepoState) -> str:
    weakest = sorted(state.topic_metrics, key=lambda item: (item.score, item.title))[:3]
    preview = ", ".join(f"{item.title}:{item.score}" for item in weakest)
    return f"health: weakest={preview}"


def _summary_topic_questions(state: RepoState) -> str:
    return f"questions: topics={len(state.topic_pages)}"


def _summary_gaps(state: RepoState) -> str:
    weak_topics = sum(metric.score < 70 or metric.mounted_sources == 0 for metric in state.topic_metrics)
    return (
        f"gaps: pending_downloads={sum(row.status != '已入库' for row in state.download_statuses)}, "
        f"weak_topics={weak_topics}, seed_sources={len(state.seed_sources)}"
    )


def _summary_structure(state: RepoState) -> str:
    flagged = [metric.key for metric in state.directory_metrics if metric.decision != "保留"]
    return f"structure: review={', '.join(flagged) if flagged else 'none'}"


def _summary_analyses(state: RepoState) -> str:
    archive_now, review_next = _analysis_archive_candidates(state)
    return (
        f"analyses: root={len(state.analysis_pages)}, "
        f"archive_now={len(archive_now)}, "
        f"review_next={len(review_next)}"
    )


def _print_or_write(body: str, path: Path | None, title: str, report_date: date, write: bool) -> None:
    if write:
        _write_report(path, title, body, report_date)
        _write_report(REPORT_FILES["hub"], "报告中心", render_report_hub(report_date), report_date)
    else:
        print(body)


def cmd_downloads(args: argparse.Namespace) -> int:
    state = build_state(args.date)
    body = render_downloads_report(state)
    _print_or_write(body, REPORT_FILES["downloads"], "下载区入库巡检", state.report_date, args.write)
    print(_summary_downloads(state))
    return 0


def cmd_prune_downloads(args: argparse.Namespace) -> int:
    state = build_state(args.date)
    if args.write:
        result = _prune_download_entries(state)
        refreshed = build_state(args.date)
        body = render_downloads_prune_report(refreshed, result, dry_run=False)
        _write_report(REPORT_FILES["downloads_prune"], "下载区清理报告", body, refreshed.report_date)
        _write_report(REPORT_FILES["downloads"], "下载区入库巡检", render_downloads_report(refreshed), refreshed.report_date)
        _write_report(REPORT_FILES["hub"], "报告中心", render_report_hub(refreshed.report_date), refreshed.report_date)
        print(_summary_download_prune(result))
        return 0

    result = DownloadPruneResult(
        deleted=[row for row in state.download_statuses if row.status == "已入库"],
        skipped=[row for row in state.download_statuses if row.status != "已入库"],
    )
    body = render_downloads_prune_report(state, result, dry_run=True)
    print(body)
    print(_summary_download_prune(result))
    return 0


def cmd_gaps(args: argparse.Namespace) -> int:
    state = build_state(args.date)
    body = render_gaps_report(state)
    _print_or_write(body, REPORT_FILES["gaps"], "知识空白扫描", state.report_date, args.write)
    print(_summary_gaps(state))
    return 0


def cmd_health(args: argparse.Namespace) -> int:
    state = build_state(args.date)
    body = render_health_report(state)
    _print_or_write(body, REPORT_FILES["health"], "主题健康度报告", state.report_date, args.write)
    print(_summary_health(state))
    return 0


def cmd_questions(args: argparse.Namespace) -> int:
    state = build_state(args.date)
    body = render_topic_questions_report(state.report_date)
    if args.write:
        _write_report(REPORT_FILES["topic_questions"], "主题擅长问题前十", body, state.report_date)
        _write_report(
            REPORT_FILES["supplement_prompts"],
            "待补强方向",
            render_supplement_prompts_report(state.report_date),
            state.report_date,
        )
        _write_report(REPORT_FILES["hub"], "报告中心", render_report_hub(state.report_date), state.report_date)
    else:
        print(body)
    print(_summary_topic_questions(state))
    return 0


def cmd_structure(args: argparse.Namespace) -> int:
    state = build_state(args.date)
    body = render_structure_report(state)
    _print_or_write(body, REPORT_FILES["structure"], "维基目录治理报告", state.report_date, args.write)
    print(_summary_structure(state))
    return 0


def cmd_analyses(args: argparse.Namespace) -> int:
    state = build_state(args.date)
    body = render_analyses_report(state)
    _print_or_write(body, REPORT_FILES["analyses"], "分析维护报告", state.report_date, args.write)
    print(_summary_analyses(state))
    return 0


def cmd_graph(args: argparse.Namespace) -> int:
    state = build_state(args.date)
    body = render_graph_report()
    if args.write:
        write_graph_config()
    _print_or_write(body, REPORT_FILES["graph"], "维基关系图谱", state.report_date, args.write)
    print("graph: updated=.obsidian/graph.json" if args.write else "graph: preview")
    return 0


def cmd_all(args: argparse.Namespace) -> int:
    state = build_state(args.date)
    if args.write:
        write_all_reports(state)
        write_graph_config()
    else:
        print(render_report_hub(state.report_date))
    print(_summary_downloads(state))
    print(_summary_gaps(state))
    print(_summary_health(state))
    print(_summary_topic_questions(state))
    print(_summary_structure(state))
    print(_summary_analyses(state))
    if args.write:
        print(f"reports: written={_page_rel(REPORT_DIR)}")
    return 0


def _report_date(value: str | None) -> date:
    if value is None:
        return date.today()
    parsed = _parse_date(value)
    if parsed is None:
        raise argparse.ArgumentTypeError(f"invalid date: {value}")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Maintain this AI wiki with stable maintenance reports.")
    parser.add_argument(
        "--date",
        type=_report_date,
        default=None,
        help="Report date in YYYY-MM-DD format. Defaults to today.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)
    for name, handler in {
        "downloads": cmd_downloads,
        "prune-downloads": cmd_prune_downloads,
        "gaps": cmd_gaps,
        "health": cmd_health,
        "questions": cmd_questions,
        "structure": cmd_structure,
        "analyses": cmd_analyses,
        "graph": cmd_graph,
        "all": cmd_all,
    }.items():
        sub = subparsers.add_parser(name)
        sub.add_argument("--write", action="store_true", help="Write report pages to wiki/report.")
        sub.set_defaults(func=handler)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.date is None:
        args.date = date.today()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
