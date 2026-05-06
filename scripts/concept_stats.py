#!/usr/bin/env python3
"""
Compute concept stats for this wiki.

Metrics:
- mounted_sources: number of `wiki/sources/*.md` whose frontmatter `sources:` includes [[<concept>]]
- featured_total: number of entries in concept frontmatter `sources:`
- featured_source_notes: number of concept frontmatter `sources:` entries that refer to an existing `wiki/sources/*.md`
- analysis_count: number of analysis pages that link to or declare the concept as their topic
- backlinks: number of incoming wikilinks from the wiki
- score: weighted health score for concept pages

This stays separate from topic stats so concepts can use a different maintenance
and scoring model without coupling back to the topic scoreboard.
"""

from __future__ import annotations

import argparse
import os
import re
from dataclasses import dataclass
from pathlib import Path

from topic_stats import parse_frontmatter


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _wikilink(title: str) -> str:
    return f"[[{title}]]"


def _fdate(value: object, fallback: str = "-") -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return fallback


def _freshness_score(updated: str) -> int:
    # Keep the concept scorer simple and deterministic: newer pages get a slight bump.
    if not updated or updated == "-":
        return 0
    return 10


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


def _section_hits(text: str) -> int:
    section_groups = {
        "scope": ("## 范围", "## 这是什么", "## 在本库中的角色"),
        "judgement": ("## 当前判断", "## 当前结论", "## 进一步判断", "## 新增判断"),
        "evidence": ("## 关键案例 / 证据", "## 已入库案例", "## 近期资料启发", "## 关键事实"),
        "analysis": ("## 分析锚点",),
        "links": ("## 关联页面", "## 主题入口"),
        "questions": ("## 未解决问题",),
    }
    return sum(any(marker in text for marker in markers) for markers in section_groups.values())


@dataclass(frozen=True)
class ConceptStat:
    title: str
    mounted_sources: int
    featured_total: int
    featured_source_notes: int
    analysis_count: int
    backlinks: int
    updated: str
    score: int
    grade: str
    gaps: tuple[str, ...]


def _load_titles_by_path(md_dir: Path) -> dict[Path, str]:
    out: dict[Path, str] = {}
    for path in sorted(md_dir.glob("*.md")):
        fm = parse_frontmatter(_read_text(path))
        title = fm.get("title")
        if isinstance(title, str) and title.strip():
            out[path] = title.strip()
        else:
            out[path] = path.stem
    return out


def compute_stats(repo_root: Path) -> list[ConceptStat]:
    wiki_dir = repo_root / "wiki"
    concepts_dir = wiki_dir / "concepts"
    sources_dir = wiki_dir / "sources"
    analyses_dir = wiki_dir / "analyses"

    concept_titles = _load_titles_by_path(concepts_dir)
    source_titles = _load_titles_by_path(sources_dir)
    source_title_set = set(source_titles.values())

    source_frontmatter_sources: dict[Path, list[str]] = {}
    for sp in sorted(sources_dir.glob("*.md")):
        fm = parse_frontmatter(_read_text(sp))
        raw_sources = fm.get("sources")
        if isinstance(raw_sources, list):
            source_frontmatter_sources[sp] = [str(x) for x in raw_sources]
        else:
            source_frontmatter_sources[sp] = []

    analysis_pages = []
    for ap in sorted(analyses_dir.glob("*.md")):
        fm = parse_frontmatter(_read_text(ap))
        if fm.get("type") == "analysis":
            analysis_pages.append((ap, fm))

    backlinks: dict[str, int] = {title: 0 for title in concept_titles.values()}
    for page_path in wiki_dir.rglob("*.md"):
        text = _read_text(page_path)
        for title in concept_titles.values():
            if _wikilink(title) in text:
                backlinks[title] = backlinks.get(title, 0) + 1

    stats: list[ConceptStat] = []
    for cp, title in concept_titles.items():
        concept_link = _wikilink(title)

        mounted = 0
        for links in source_frontmatter_sources.values():
            if concept_link in links or title in links:
                mounted += 1

        cfm = parse_frontmatter(_read_text(cp))
        featured_raw = cfm.get("sources")
        featured_list = featured_raw if isinstance(featured_raw, list) else []
        featured_total = len(featured_list)

        featured_source_notes = 0
        for item in featured_list:
            m = re.match(r"^\[\[(.+?)\]\]$", str(item).strip())
            name = m.group(1).strip() if m else str(item).strip()
            if name in source_title_set:
                featured_source_notes += 1

        analysis_count = 0
        for ap, fm in analysis_pages:
            topic = str(fm.get("topic") or "").strip()
            text = _read_text(ap)
            if topic == title or concept_link in text or title in text:
                analysis_count += 1

        updated = _fdate(cfm.get("updated"))
        section_hits = _section_hits(_read_text(cp))

        score = 0
        score += round(min(mounted, 20) / 20 * 30)
        score += round(min(featured_source_notes, 8) / 8 * 15)
        score += round(min(analysis_count, 3) / 3 * 20)
        score += round(min(backlinks.get(title, 0), 12) / 12 * 15)
        score += round(section_hits / 6 * 10)
        score += _freshness_score(updated)
        score = min(score, 100)

        gaps: list[str] = []
        if mounted == 0:
            gaps.append("缺 source 挂载")
        elif mounted < 3:
            gaps.append("source 证据偏薄")
        if analysis_count == 0:
            gaps.append("缺 analysis 锚点")
        if backlinks.get(title, 0) < 3:
            gaps.append("被引用偏少")
        if section_hits < 4:
            gaps.append("正文结构偏薄")
        if _freshness_score(updated) == 0:
            gaps.append("缺更新时间")
        stats.append(
            ConceptStat(
                title=title,
                mounted_sources=mounted,
                featured_total=featured_total,
                featured_source_notes=featured_source_notes,
                analysis_count=analysis_count,
                backlinks=backlinks.get(title, 0),
                updated=updated,
                score=score,
                grade=_grade(score),
                gaps=tuple(gaps[:2]),
            )
        )

    stats.sort(key=lambda s: (-s.score, s.title))
    return stats


def render_markdown(stats: list[ConceptStat]) -> str:
    lines = [
        "| Concept | 分数 | 等级 | 挂载 source | 精选 source | analysis | backlinks | updated | 主要短板 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |",
    ]
    for s in stats:
        gaps = "；".join(s.gaps) if s.gaps else "无明显短板"
        lines.append(
            f"| [[{s.title}]] | {s.score} | {s.grade} | {s.mounted_sources} | "
            f"{s.featured_source_notes}/{s.featured_total} | {s.analysis_count} | {s.backlinks} | {s.updated} | "
            f"{gaps} |"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--repo-root",
        default=os.getcwd(),
        help="Repo root (default: cwd)",
    )
    ap.add_argument(
        "--format",
        choices=["md"],
        default="md",
        help="Output format",
    )
    args = ap.parse_args()

    repo_root = Path(args.repo_root).resolve()
    stats = compute_stats(repo_root)

    if args.format == "md":
        print(render_markdown(stats), end="")
        return 0

    raise RuntimeError(f"Unknown format: {args.format}")


if __name__ == "__main__":
    raise SystemExit(main())
