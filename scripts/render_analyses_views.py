#!/usr/bin/env python3
"""
Render static analysis dashboards from per-page metadata.

This keeps leaf analyses as the source of truth while making overview/topic pages
cheap to maintain. Obsidian users also get Dataview blocks for live preview.
"""

from __future__ import annotations

import argparse
import os
import re
from dataclasses import dataclass
from pathlib import Path

from topic_stats import parse_frontmatter


ROOT = Path(__file__).resolve().parent.parent
WIKI = ROOT / "wiki"
ANALYSES = WIKI / "analyses"
ANALYSIS_TOPICS = ANALYSES / "topics"
INDEX_PAGE = ANALYSES / "主题索引.md"
AUTO_START = "<!-- AUTO:START -->"
AUTO_END = "<!-- AUTO:END -->"

TOPIC_ORDER = [
    "AI时代如何学习",
]
KIND_ORDER = ["backbone", "slice", "governance", "snapshot"]
PRIORITY_ORDER = ["high", "medium", "low"]
KIND_LABELS = {
    "backbone": "主线总纲",
    "slice": "专题切片",
    "governance": "治理页",
    "snapshot": "阶段快照",
}
PRIORITY_LABELS = {
    "high": "高",
    "medium": "中",
    "low": "低",
}
REQUIRED_ANALYSIS_FIELDS = {
    "topic",
    "kind",
    "summary",
    "priority",
    "last_reviewed",
    "evergreen",
}


def _is_missing(value: object) -> bool:
    return value is None or value == "" or value == []


@dataclass(frozen=True)
class TopicPage:
    path: Path
    title: str
    topic: str


@dataclass(frozen=True)
class AnalysisPage:
    path: Path
    title: str
    topic: str
    kind: str
    priority: str
    updated: str
    last_reviewed: str
    evergreen: bool
    summary: str


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _split_frontmatter(text: str) -> tuple[str | None, str]:
    if not text.startswith("---\n"):
        return None, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return None, text
    return text[4:end], text[end + 5 :]


def _replace_auto_block(text: str, block: str) -> str:
    start = text.find(AUTO_START)
    end = text.find(AUTO_END)
    if start == -1 or end == -1 or end < start:
        raise ValueError("missing auto block markers")
    prefix = text[: start + len(AUTO_START)]
    suffix = text[end:]
    return f"{prefix}\n\n{block.rstrip()}\n\n{suffix}"


def _sort_key(page: AnalysisPage) -> tuple[int, int, str, str]:
    updated_key = page.updated.replace("-", "") if page.updated else "00000000"
    return (
        KIND_ORDER.index(page.kind),
        PRIORITY_ORDER.index(page.priority),
        f"{99999999 - int(updated_key):08d}",
        page.title,
    )


def _parse_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes", "1"}:
            return True
        if lowered in {"false", "no", "0"}:
            return False
    raise ValueError(f"invalid boolean value: {value!r}")


def _load_topic_pages() -> list[TopicPage]:
    pages: list[TopicPage] = []
    for path in sorted(ANALYSIS_TOPICS.glob("*.md")):
        fm = parse_frontmatter(_read_text(path))
        title = str(fm.get("title") or path.stem).strip()
        topic = str(fm.get("topic") or "").strip()
        if not topic:
            raise ValueError(f"{path.relative_to(ROOT)} missing `topic` frontmatter")
        pages.append(TopicPage(path=path, title=title, topic=topic))
    order = {topic: idx for idx, topic in enumerate(TOPIC_ORDER)}
    pages.sort(key=lambda p: (order.get(p.topic, 999), p.title))
    return pages


def _load_analysis_pages() -> list[AnalysisPage]:
    pages: list[AnalysisPage] = []
    for path in sorted(ANALYSES.glob("*.md")):
        if path.name == INDEX_PAGE.name:
            continue

        fm = parse_frontmatter(_read_text(path))
        if fm.get("type") != "analysis":
            continue

        missing = [key for key in REQUIRED_ANALYSIS_FIELDS if key not in fm or _is_missing(fm.get(key))]
        if missing:
            joined = ", ".join(missing)
            raise ValueError(f"{path.relative_to(ROOT)} missing analysis metadata: {joined}")

        pages.append(
            AnalysisPage(
                path=path,
                title=str(fm.get("title") or path.stem).strip(),
                topic=str(fm["topic"]).strip(),
                kind=str(fm["kind"]).strip(),
                priority=str(fm["priority"]).strip(),
                updated=str(fm.get("updated") or "").strip(),
                last_reviewed=str(fm["last_reviewed"]).strip(),
                evergreen=_parse_bool(fm["evergreen"]),
                summary=str(fm["summary"]).strip(),
            )
        )

    for page in pages:
        if page.kind not in KIND_LABELS:
            raise ValueError(f"{page.path.relative_to(ROOT)} has unknown kind `{page.kind}`")
        if page.priority not in PRIORITY_LABELS:
            raise ValueError(f"{page.path.relative_to(ROOT)} has unknown priority `{page.priority}`")

    pages.sort(key=_sort_key)
    return pages


def _group_by_topic(pages: list[AnalysisPage]) -> dict[str, list[AnalysisPage]]:
    grouped = {topic: [] for topic in TOPIC_ORDER}
    for page in pages:
        grouped.setdefault(page.topic, []).append(page)
    for items in grouped.values():
        items.sort(key=_sort_key)
    return grouped


def _pick_backbone(items: list[AnalysisPage]) -> str:
    backbones = sorted(
        (page for page in items if page.kind == "backbone"),
        key=lambda page: (page.updated, page.title),
    )
    if backbones:
        return f"[[{backbones[0].title}]]"
    if items:
        return f"[[{items[0].title}]]"
    return "-"


def _topic_metrics(items: list[AnalysisPage]) -> dict[str, object]:
    if not items:
        return {
            "total": 0,
            "backbone": 0,
            "slice": 0,
            "governance": 0,
            "snapshot": 0,
            "high": 0,
            "evergreen": 0,
            "updated": "-",
            "reviewed": "-",
        }

    return {
        "total": len(items),
        "backbone": sum(page.kind == "backbone" for page in items),
        "slice": sum(page.kind == "slice" for page in items),
        "governance": sum(page.kind == "governance" for page in items),
        "snapshot": sum(page.kind == "snapshot" for page in items),
        "high": sum(page.priority == "high" for page in items),
        "evergreen": sum(page.evergreen for page in items),
        "updated": max(page.updated for page in items),
        "reviewed": max(page.last_reviewed for page in items),
    }


def _render_page_line(page: AnalysisPage) -> str:
    evergreen = "常青" if page.evergreen else "阶段性"
    priority = PRIORITY_LABELS[page.priority]
    return f"- [[{page.title}]]：{page.summary}｜{KIND_LABELS[page.kind]}｜{priority}｜{page.updated}｜{evergreen}"


def _render_topic_block(topic: str, items: list[AnalysisPage]) -> str:
    metrics = _topic_metrics(items)
    lines: list[str] = []
    lines.append("## 快速预览（自动生成）")
    lines.append("")
    lines.append(f"- 分析总数：{metrics['total']}")
    lines.append(f"- 主线总纲：{metrics['backbone']}")
    lines.append(f"- 专题切片：{metrics['slice']}")
    lines.append(f"- 治理页：{metrics['governance']}")
    lines.append(f"- 阶段快照：{metrics['snapshot']}")
    lines.append(f"- 高优先级：{metrics['high']}")
    lines.append(f"- 常青页：{metrics['evergreen']}")
    lines.append(f"- 最近更新：{metrics['updated']}")
    lines.append(f"- 最近复核：{metrics['reviewed']}")
    lines.append("")
    lines.append("## 静态目录（自动生成）")
    lines.append("")

    for kind in KIND_ORDER:
        group = [page for page in items if page.kind == kind]
        if not group:
            continue
        if kind == "backbone":
            group = sorted(group, key=lambda page: (page.updated, page.title))
        lines.append(f"### {KIND_LABELS[kind]}")
        lines.append("")
        lines.extend(_render_page_line(page) for page in group)
        lines.append("")

    lines.append("## Obsidian 动态预览")
    lines.append("")
    lines.append("```dataview")
    lines.append("TABLE kind as 类型, priority as 优先级, updated as 更新, last_reviewed as 复核, evergreen as 常青, summary as 摘要")
    lines.append('FROM "wiki/analyses"')
    lines.append(f'WHERE type = "analysis" AND topic = "{topic}"')
    lines.append('SORT choice(kind = "backbone", 0, choice(kind = "slice", 1, choice(kind = "governance", 2, 3))) ASC, choice(priority = "high", 0, choice(priority = "medium", 1, 2)) ASC, updated DESC')
    lines.append("```")
    return "\n".join(lines)


def _render_index_block(topic_pages: list[TopicPage], grouped: dict[str, list[AnalysisPage]]) -> str:
    lines: list[str] = []
    lines.append("## 主题看板（自动生成）")
    lines.append("")
    lines.append("| 主题 | 分析数 | 主线总纲 | 专题切片 | 治理/快照 | 高优先级 | 常青页 | 最近更新 | 最近复核 | 入口 |")
    lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- | --- |")
    by_topic_page = {page.topic: page for page in topic_pages}
    for topic in TOPIC_ORDER:
        items = grouped.get(topic, [])
        metrics = _topic_metrics(items)
        topic_page = by_topic_page.get(topic)
        entry = f"[[{topic_page.title}]]" if topic_page else "-"
        lines.append(
            f"| {topic} | {metrics['total']} | {metrics['backbone']} | {metrics['slice']} | "
            f"{metrics['governance'] + metrics['snapshot']} | {metrics['high']} | {metrics['evergreen']} | "
            f"{metrics['updated']} | {metrics['reviewed']} | {entry} |"
        )

    lines.append("")
    lines.append("## 每个主题先看什么（自动生成）")
    lines.append("")
    for topic in TOPIC_ORDER:
        items = grouped.get(topic, [])
        if not items:
            continue
        lead = _pick_backbone(items)
        lead_title = lead[2:-2] if lead.startswith("[[") and lead.endswith("]]") else ""
        extras = [page for page in items if page.title != lead_title][:2]
        if extras:
            links = " / ".join(f"[[{page.title}]]" for page in extras)
            lines.append(f"- {topic}：先看 {lead}；继续下钻 {links}")
        else:
            lines.append(f"- {topic}：先看 {lead}")

    lines.append("")
    lines.append("## Obsidian 动态预览")
    lines.append("")
    lines.append("```dataview")
    lines.append("TABLE topic as 主题, kind as 类型, priority as 优先级, updated as 更新, last_reviewed as 复核, evergreen as 常青, summary as 摘要")
    lines.append('FROM "wiki/analyses"')
    lines.append('WHERE type = "analysis"')
    lines.append('SORT topic ASC, choice(kind = "backbone", 0, choice(kind = "slice", 1, choice(kind = "governance", 2, 3))) ASC, choice(priority = "high", 0, choice(priority = "medium", 1, 2)) ASC, updated DESC')
    lines.append("```")
    return "\n".join(lines)


def render(repo_root: Path) -> None:
    topic_pages = _load_topic_pages()
    analysis_pages = _load_analysis_pages()
    grouped = _group_by_topic(analysis_pages)

    index_text = _read_text(repo_root / "wiki" / "analyses" / "主题索引.md")
    updated_index = _replace_auto_block(index_text, _render_index_block(topic_pages, grouped))
    (repo_root / "wiki" / "analyses" / "主题索引.md").write_text(updated_index, encoding="utf-8")

    for topic_page in topic_pages:
        page_text = _read_text(topic_page.path)
        updated_page = _replace_auto_block(page_text, _render_topic_block(topic_page.topic, grouped.get(topic_page.topic, [])))
        topic_page.path.write_text(updated_page, encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--repo-root",
        default=os.getcwd(),
        help="Repo root (default: cwd)",
    )
    args = ap.parse_args()

    repo_root = Path(args.repo_root).resolve()
    render(repo_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
