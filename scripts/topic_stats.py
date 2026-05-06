#!/usr/bin/env python3
"""
Compute topic stats for this wiki.

Metrics:
- mounted_sources: number of `wiki/sources/*.md` whose frontmatter `sources:` includes [[<topic>]]
- featured_total: number of entries in topic frontmatter `sources:`
- featured_source_notes: number of topic frontmatter `sources:` entries that refer to an existing `wiki/sources/*.md`

This repo intentionally avoids external deps (no PyYAML), so frontmatter parsing is minimal and tolerant.
"""

from __future__ import annotations

import argparse
import os
import re
from dataclasses import dataclass
from pathlib import Path


FRONTMATTER_DELIM = "---"


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _extract_frontmatter(md: str) -> str | None:
    lines = md.splitlines()
    if not lines or lines[0].strip() != FRONTMATTER_DELIM:
        return None
    for i in range(1, len(lines)):
        if lines[i].strip() == FRONTMATTER_DELIM:
            return "\n".join(lines[1:i]) + "\n"
    return None


def _strip_quotes(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and ((s[0] == s[-1] == '"') or (s[0] == s[-1] == "'")):
        return s[1:-1]
    return s


def parse_frontmatter(md: str) -> dict[str, object]:
    fm = _extract_frontmatter(md)
    if fm is None:
        return {}

    result: dict[str, object] = {}
    current_key: str | None = None
    current_list: list[str] | None = None

    key_re = re.compile(r"^([A-Za-z0-9_./-]+):(?:\s*(.*))?$")
    list_item_re = re.compile(r"^\s*-\s+(.*)$")

    for raw in fm.splitlines():
        line = raw.rstrip("\n")
        if not line.strip() or line.lstrip().startswith("#"):
            continue

        m_key = key_re.match(line)
        if m_key:
            if current_key is not None and current_list is not None:
                result[current_key] = current_list
            current_key = m_key.group(1)
            value = (m_key.group(2) or "").strip()
            if value == "":
                current_list = []
                continue
            current_list = None
            result[current_key] = _strip_quotes(value)
            continue

        m_item = list_item_re.match(line)
        if m_item and current_key is not None:
            if current_list is None:
                current_list = []
            current_list.append(_strip_quotes(m_item.group(1).strip()))
            continue

        # Unknown line; ignore to keep parser tolerant.

    if current_key is not None and current_list is not None:
        result[current_key] = current_list

    return result


def _wikilink(title: str) -> str:
    return f"[[{title}]]"


@dataclass(frozen=True)
class TopicStat:
    title: str
    mounted_sources: int
    featured_total: int
    featured_source_notes: int


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


def compute_stats(repo_root: Path) -> list[TopicStat]:
    topics_dir = repo_root / "wiki" / "topics"
    sources_dir = repo_root / "wiki" / "sources"

    topic_titles = _load_titles_by_path(topics_dir)
    source_titles = _load_titles_by_path(sources_dir)
    source_title_set = set(source_titles.values())

    # Pre-parse source note frontmatters once.
    source_frontmatter_sources: dict[Path, list[str]] = {}
    for sp in sorted(sources_dir.glob("*.md")):
        fm = parse_frontmatter(_read_text(sp))
        raw_sources = fm.get("sources")
        if isinstance(raw_sources, list):
            source_frontmatter_sources[sp] = [str(x) for x in raw_sources]
        else:
            source_frontmatter_sources[sp] = []

    stats: list[TopicStat] = []
    for tp, title in topic_titles.items():
        topic_link = _wikilink(title)

        mounted = 0
        for sp, links in source_frontmatter_sources.items():
            if topic_link in links or title in links:
                mounted += 1

        tfm = parse_frontmatter(_read_text(tp))
        featured_raw = tfm.get("sources")
        featured_list = featured_raw if isinstance(featured_raw, list) else []
        featured_total = len(featured_list)

        featured_source_notes = 0
        for item in featured_list:
            m = re.match(r"^\[\[(.+?)\]\]$", str(item).strip())
            name = m.group(1).strip() if m else str(item).strip()
            if name in source_title_set:
                featured_source_notes += 1

        stats.append(
            TopicStat(
                title=title,
                mounted_sources=mounted,
                featured_total=featured_total,
                featured_source_notes=featured_source_notes,
            )
        )

    # Sort by mounted_sources desc, then title for stability.
    stats.sort(key=lambda s: (-s.mounted_sources, s.title))
    return stats


def _render_markdown(stats: list[TopicStat]) -> str:
    lines = []
    lines.append("| Topic | wiki/sources 挂载数 | frontmatter 精选数 | frontmatter 精选(仅 source note) |")
    lines.append("| --- | ---: | ---: | ---: |")
    for s in stats:
        lines.append(
            f"| [[{s.title}]] | {s.mounted_sources} | {s.featured_total} | {s.featured_source_notes} |"
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
        print(_render_markdown(stats), end="")
        return 0

    raise RuntimeError(f"Unknown format: {args.format}")


if __name__ == "__main__":
    raise SystemExit(main())
