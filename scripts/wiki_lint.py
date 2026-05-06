#!/usr/bin/env python3

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

from topic_stats import parse_frontmatter


ROOT = Path(__file__).resolve().parent.parent
WIKI_DIR = ROOT / "wiki"
SUSPICIOUS_SOURCE_PATTERNS = (
    "403 forbidden",
    "file not found",
    "page not found",
    "just a moment",
    "not found",
)
SUSPICIOUS_SOURCE_EXACT = {
    "the",
    "my",
    "notion",
    "[](https:／／www.anthropic.com／)",
}
STRICT_LIMITS = {
    "max_unintegrated_source_ratio": 0.50,
    "max_hubs_without_analysis": 0,
    "max_suspicious_sources": 0,
    "max_missing_frontmatter": 0,
    "max_analyses_missing_metadata": 0,
}
WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
REQUIRED_ANALYSIS_METADATA = {
    "topic",
    "kind",
    "summary",
    "priority",
    "last_reviewed",
    "evergreen",
}


def is_missing(value: object) -> bool:
    return value is None or value == "" or value == []


def iter_markdown_files(path: Path) -> list[Path]:
    return sorted(path.rglob("*.md"))


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def title_map(paths: list[Path]) -> dict[str, Path]:
    return {path.stem: path for path in paths}


def wikilinks(text: str) -> list[str]:
    return WIKILINK_RE.findall(text)


def rel(path: Path) -> str:
    return str(path.relative_to(ROOT))


def is_hub(path: Path) -> bool:
    return path.parent.name in {"topics", "concepts"} or path == WIKI_DIR / "overview.md"


def is_excluded_backlink_target(path: Path) -> bool:
    return path.name in {"index.md", "log.md"}


def is_analysis_leaf(path: Path, text: str) -> bool:
    if path.parent != WIKI_DIR / "analyses":
        return False
    fm = parse_frontmatter(text)
    return fm.get("type") == "analysis"


def compute_metrics() -> dict[str, object]:
    all_pages = iter_markdown_files(WIKI_DIR)
    by_title = title_map(all_pages)
    analysis_titles = {path.stem for path in iter_markdown_files(WIKI_DIR / "analyses")}

    backlinks = Counter()
    non_source_backlinks: dict[str, set[str]] = defaultdict(set)
    missing_frontmatter: list[str] = []
    hubs_without_analysis: list[str] = []
    analyses_missing_metadata: list[str] = []

    for path in all_pages:
        text = read_text(path)
        if path.name != "log.md" and not text.startswith("---\n"):
            missing_frontmatter.append(rel(path))

        links = wikilinks(text)
        for link in links:
            if link in by_title:
                backlinks[link] += 1
                # A source is considered "integrated" once any non-source, non-index/log wiki page links to it.
                if not path.match(str(WIKI_DIR / "sources" / "*.md")) and not is_excluded_backlink_target(path):
                    non_source_backlinks[link].add(rel(path))

        if path.parent.name in {"topics", "concepts"}:
            if not (set(links) & analysis_titles):
                hubs_without_analysis.append(rel(path))

        if is_analysis_leaf(path, text):
            fm = parse_frontmatter(text)
            missing = [key for key in REQUIRED_ANALYSIS_METADATA if key not in fm or is_missing(fm.get(key))]
            if missing:
                joined = ", ".join(sorted(missing))
                analyses_missing_metadata.append(f"{rel(path)} :: {joined}")

    source_pages = iter_markdown_files(WIKI_DIR / "sources")
    unintegrated_sources = [
        rel(path)
        for path in source_pages
        if not non_source_backlinks.get(path.stem)
    ]

    suspicious_sources = []
    for path in source_pages:
        stem = path.stem.lower()
        if any(pattern in stem for pattern in SUSPICIOUS_SOURCE_PATTERNS) or stem in SUSPICIOUS_SOURCE_EXACT:
            suspicious_sources.append(rel(path))

    return {
        "total_pages": len(all_pages),
        "total_sources": len(source_pages),
        "integrated_sources": len(source_pages) - len(unintegrated_sources),
        "unintegrated_sources": unintegrated_sources,
        "suspicious_sources": suspicious_sources,
        "missing_frontmatter": missing_frontmatter,
        "hubs_without_analysis": hubs_without_analysis,
        "analyses_missing_metadata": analyses_missing_metadata,
    }


def print_report(metrics: dict[str, object]) -> None:
    total_sources = int(metrics["total_sources"])
    integrated_sources = int(metrics["integrated_sources"])
    unintegrated_sources = metrics["unintegrated_sources"]
    suspicious_sources = metrics["suspicious_sources"]
    missing_frontmatter = metrics["missing_frontmatter"]
    hubs_without_analysis = metrics["hubs_without_analysis"]
    analyses_missing_metadata = metrics["analyses_missing_metadata"]
    unintegrated_ratio = len(unintegrated_sources) / total_sources if total_sources else 0.0

    print("Karpathy wiki health")
    print(f"- total pages: {metrics['total_pages']}")
    print(
        f"- source integration: {integrated_sources}/{total_sources} integrated, "
        f"{len(unintegrated_sources)} unintegrated ({unintegrated_ratio:.1%})"
    )
    print(f"- hub coverage: {len(hubs_without_analysis)} topic/concept pages without analysis anchor")
    print(f"- suspicious source titles: {len(suspicious_sources)}")
    print(f"- missing frontmatter: {len(missing_frontmatter)}")
    print(f"- analyses missing metadata: {len(analyses_missing_metadata)}")

    preview_sections = (
        ("Hubs without analysis anchor", hubs_without_analysis),
        ("Suspicious source pages", suspicious_sources),
        ("Pages missing frontmatter", missing_frontmatter),
        ("Analyses missing metadata", analyses_missing_metadata),
        ("Sample unintegrated sources", unintegrated_sources[:30]),
    )
    for header, items in preview_sections:
        if not items:
            continue
        print(f"\n{header}:")
        for item in items:
            print(f"  - {item}")


def strict_failures(metrics: dict[str, object]) -> list[str]:
    total_sources = int(metrics["total_sources"])
    unintegrated_sources = metrics["unintegrated_sources"]
    unintegrated_ratio = len(unintegrated_sources) / total_sources if total_sources else 0.0
    failures = []

    if unintegrated_ratio > STRICT_LIMITS["max_unintegrated_source_ratio"]:
        failures.append(
            "unintegrated source ratio "
            f"{unintegrated_ratio:.1%} > {STRICT_LIMITS['max_unintegrated_source_ratio']:.0%}"
        )
    if len(metrics["hubs_without_analysis"]) > STRICT_LIMITS["max_hubs_without_analysis"]:
        failures.append(
            f"hubs without analysis {len(metrics['hubs_without_analysis'])} "
            f"> {STRICT_LIMITS['max_hubs_without_analysis']}"
        )
    if len(metrics["suspicious_sources"]) > STRICT_LIMITS["max_suspicious_sources"]:
        failures.append(
            f"suspicious sources {len(metrics['suspicious_sources'])} "
            f"> {STRICT_LIMITS['max_suspicious_sources']}"
        )
    if len(metrics["missing_frontmatter"]) > STRICT_LIMITS["max_missing_frontmatter"]:
        failures.append(
            f"missing frontmatter {len(metrics['missing_frontmatter'])} "
            f"> {STRICT_LIMITS['max_missing_frontmatter']}"
        )
    if len(metrics["analyses_missing_metadata"]) > STRICT_LIMITS["max_analyses_missing_metadata"]:
        failures.append(
            f"analyses missing metadata {len(metrics['analyses_missing_metadata'])} "
            f"> {STRICT_LIMITS['max_analyses_missing_metadata']}"
        )
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description="Lint wiki health from a Karpathy-style compression perspective.")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero when core wiki health thresholds are not met.",
    )
    args = parser.parse_args()

    metrics = compute_metrics()
    print_report(metrics)

    if not args.strict:
        return 0

    failures = strict_failures(metrics)
    if not failures:
        return 0

    print("\nStrict check failed:")
    for failure in failures:
        print(f"  - {failure}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
