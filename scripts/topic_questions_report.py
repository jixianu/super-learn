#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from collections import Counter

from topic_stats import parse_frontmatter


ROOT = Path(__file__).resolve().parent.parent
WIKI = ROOT / "wiki"
REPORT_DIR = WIKI / "report"
REPORT_TITLE = "主题擅长问题前十"
REPORT_PATH = REPORT_DIR / f"{REPORT_TITLE}.md"
SUPPLEMENT_REPORT_TITLE = "待补强方向"
SUPPLEMENT_REPORT_PATH = REPORT_DIR / f"{SUPPLEMENT_REPORT_TITLE}.md"
QUESTION_LIMIT = 10
MIN_ANSWERABILITY_SCORE = 75
WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
HEADING_RE = re.compile(r"^(#{2,3})\s+(.+?)\s*$")
BULLET_RE = re.compile(r"^\s*[-*]\s+(.*\S)\s*$")
BACKTICK_RE = re.compile(r"`([^`]+)`")
QUESTIONISH_RE = re.compile(r"[？?]$")


@dataclass(frozen=True)
class Page:
    path: Path
    stem: str
    text: str
    fm: dict[str, object]
    links: list[str]


@dataclass(frozen=True)
class QuestionCandidate:
    question: str
    source: str
    score: int


@dataclass(frozen=True)
class SupplementItem:
    issue: str
    target: str
    action: str


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _iter_markdown_files(path: Path) -> list[Path]:
    if not path.exists():
        return []
    return sorted(p for p in path.rglob("*.md") if p.is_file())


def _load_pages() -> list[Page]:
    pages: list[Page] = []
    for path in _iter_markdown_files(WIKI):
        text = _read_text(path)
        pages.append(
            Page(
                path=path,
                stem=path.stem,
                text=text,
                fm=parse_frontmatter(text),
                links=WIKILINK_RE.findall(text),
            )
        )
    return pages


def _normalize_wikilink(value: str) -> str:
    return value.split("|", 1)[0].strip()


def _clean_text(value: str) -> str:
    value = value.strip()
    value = re.sub(r"^[\-\*\d\.\s]+", "", value)
    value = value.rstrip("。；;，,：:")
    return value.strip()


def _section_map(text: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for raw in text.splitlines():
        m = HEADING_RE.match(raw)
        if m:
            current = m.group(2).strip()
            sections.setdefault(current, [])
            continue
        if current is not None:
            sections[current].append(raw)
    return sections


def _section_lines(sections: dict[str, list[str]], needles: list[str]) -> list[str]:
    out: list[str] = []
    for heading, lines in sections.items():
        if any(needle in heading for needle in needles):
            out.extend(lines)
    return out


def _bullet_items(lines: list[str]) -> list[str]:
    items: list[str] = []
    for line in lines:
        m = BULLET_RE.match(line)
        if m:
            item = _clean_text(m.group(1))
            if item:
                items.append(item)
    return items


def _extract_terms(text: str) -> list[str]:
    terms: list[str] = []
    for term in BACKTICK_RE.findall(text):
        cleaned = _clean_text(term)
        if cleaned:
            terms.append(cleaned)
    for link in WIKILINK_RE.findall(text):
        cleaned = _clean_text(_normalize_wikilink(link))
        if cleaned:
            terms.append(cleaned)
    for raw in re.findall(r"[A-Za-z][A-Za-z0-9+/_-]{1,40}", text):
        cleaned = raw.strip()
        if len(cleaned) >= 3:
            terms.append(cleaned)
    counts = Counter(terms)
    ordered: list[str] = []
    for term, _ in counts.most_common():
        if term not in ordered and len(term) <= 40:
            ordered.append(term)
    return ordered


def _looks_questionish(text: str) -> bool:
    return bool(QUESTIONISH_RE.search(text)) or any(text.startswith(prefix) for prefix in ("为什么", "如何", "怎么", "什么", "哪", "是否"))


def _make_question(topic: str, phrase: str) -> str:
    phrase = _clean_text(phrase)
    if not phrase:
        return f"{topic} 还有哪些最值得追问的问题？"
    if _looks_questionish(phrase):
        return phrase
    if phrase.endswith("是什么"):
        return f"{topic} 里的 {phrase}？"
    if len(phrase) <= 16:
        return f"{topic} 的 {phrase} 是什么？"
    return f"为什么 {phrase}？"


def _judgment_question(title: str, phrase: str) -> str:
    phrase = _clean_text(phrase)
    phrase = re.sub(rf"^{re.escape(title)}(?:里|中)?[，,:：\s]*", "", phrase)
    phrase = re.sub(rf"^{re.escape(title)}\s*的\s*", "", phrase)
    phrase = phrase.removeprefix("它的").strip()
    phrase = phrase.lstrip("的").strip()
    if not phrase:
        return f"为什么 {title} 的这条判断成立？"
    if _looks_questionish(phrase):
        return phrase
    return f"为什么 {phrase}？"


def _answerability_label(score: int) -> str:
    if score >= 90:
        return "高"
    if score >= 75:
        return "中"
    return "低"


def _topic_specific_questions(page: Page, title: str, related: list[str]) -> list[QuestionCandidate]:
    sections = _section_map(page.text)
    candidates: list[QuestionCandidate] = []

    current_judgment = _bullet_items(_section_lines(sections, ["当前判断", "当前结论", "新增判断"]))
    for item in current_judgment[:6]:
        candidates.append(
            QuestionCandidate(
                question=_judgment_question(title, item),
                source="当前判断",
                score=96,
            )
        )

    anchor_lines = _bullet_items(_section_lines(sections, ["分析锚点"]))
    for anchor in anchor_lines[:3]:
        anchor = _clean_text(anchor)
        candidates.append(
            QuestionCandidate(
                question=f"围绕 {title}，{anchor} 最能说明什么？",
                source="分析锚点",
                score=92,
            )
        )

    evidence_lines = _bullet_items(_section_lines(sections, ["关键案例 / 证据", "关键案例", "已入库案例"]))
    for evidence in evidence_lines[:4]:
        evidence = _clean_text(evidence)
        candidates.append(
            QuestionCandidate(
                question=f"对 {title} 来说，{evidence} 支持了哪条核心判断？",
                source="关键案例 / 证据",
                score=94,
            )
        )

    related_links = related[:3]
    for link in related_links:
        candidates.append(
            QuestionCandidate(
                question=f"{title} 和 {link} 的边界到底在哪里？",
                source="关联页面",
                score=84,
            )
        )
    for left, right in zip(related_links, related_links[1:]):
        candidates.append(
            QuestionCandidate(
                question=f"如果要把 {title} 落到实践里，应该先从 {left} 还是 {right} 入手？",
                source="关联页面",
                score=82,
            )
        )

    terms = [term for term in _extract_terms(page.text) if term.lower() != title.lower()]
    interesting_terms = [term for term in terms if term not in related_links]
    for offset in range(0, min(len(interesting_terms), 9), 3):
        group = interesting_terms[offset : offset + 3]
        if len(group) < 2:
            continue
        focus = "、".join(group)
        candidates.append(
            QuestionCandidate(
                question=f"在 {title} 里，{focus} 这几个概念分别承担什么职责？",
                source="术语高频",
                score=78,
            )
        )

    question_lines = _bullet_items(_section_lines(sections, ["关注问题", "关键问题", "重点子题"]))
    for item in question_lines[:4]:
        candidates.append(
            QuestionCandidate(
                question=_make_question(title, item),
                source="问题清单",
                score=76,
            )
        )

    return candidates


def _supplement_for_gap(issue: str, topic: str) -> SupplementItem:
    issue = _clean_text(issue)
    text = f"{topic} {issue}"
    if any(term in text for term in ("孩子", "教育", "家庭", "成长", "年龄", "陪学")):
        return SupplementItem(
            issue=issue,
            target="wiki/analyses/儿童与家庭场景的分龄 AI 使用边界",
            action="按年龄、能力和场景三列整理判断，并补 2-3 个家庭陪学或儿童成长的 source 证据。",
        )
    if any(term in text for term in ("学校", "升学", "补课", "自学", "家长", "中国")):
        return SupplementItem(
            issue=issue,
            target="wiki/analyses/儿童与家庭场景的分龄 AI 使用边界",
            action="补中国学习场景的证据：学校、家庭、自学、升学、成人再学习分别怎么用 AI，以及哪些边界不能越。",
        )
    if any(term in text for term in ("能力", "判断", "表达", "抽象", "审美", "迁移")):
        return SupplementItem(
            issue=issue,
            target="wiki/analyses/AI 时代学习的能力结构",
            action="补 2-3 条学习证据，把能力分层写清楚，并说明哪些能力更像慢变量、哪些更容易被商品化。",
        )
    if any(term in text for term in ("任务", "工作流", "复盘", "输出", "产物")):
        return SupplementItem(
            issue=issue,
            target="wiki/analyses/成人在 AI 时代的任务驱动学习系统",
            action="把任务驱动学习写成可执行闭环：选任务、补上下文、做输出、接真实使用、做复盘。",
        )
    if any(term in text for term in ("基础", "脉络", "课程", "教材", "入门", "地图")):
        return SupplementItem(
            issue=issue,
            target="wiki/analyses/AI 时代学习的能力结构",
            action="补一张入门地图：先学什么、后学什么、哪些内容适合作为底层教材、哪些只适合做辅助阅读。",
        )
    return SupplementItem(
        issue=issue,
        target="wiki/analyses/AI 时代学习的能力结构 + 对应 topic",
        action="先把问题改写成一个可判定命题，再补 2-3 条 source 证据或反例；最后只把稳定结论回写到 topic，保留争议点在 analysis。",
    )


def _topic_supplement_items(page: Page) -> list[SupplementItem]:
    sections = _section_map(page.text)
    items: list[SupplementItem] = []
    topic = str(page.fm.get("title") or page.stem).strip() or page.stem

    unresolved = _bullet_items(_section_lines(sections, ["未解决问题"]))
    for item in unresolved[:3]:
        items.append(_supplement_for_gap(item, topic))

    judgments = _bullet_items(_section_lines(sections, ["当前判断", "当前结论", "新增判断"]))
    if len(judgments) < 4:
        items.append(
            SupplementItem(
                issue="当前判断偏少，Top10 容易被关联页或术语问题填满。",
                target="topic 当前判断",
                action="从已入库 source 中提炼 2-3 条稳定判断，写成可被追问的因果句。",
            )
        )

    evidence = _bullet_items(_section_lines(sections, ["关键案例 / 证据", "关键案例", "已入库案例"]))
    if len(evidence) < 4:
        items.append(
            SupplementItem(
                issue="关键案例 / 证据偏少，问题可答性主要依赖概念判断。",
                target="topic 关键案例 / 证据 + wiki/sources",
                action="补 2-4 条高质量 source note，并在 topic 中写清每条 source 支持哪条判断。",
            )
        )

    anchors = _bullet_items(_section_lines(sections, ["分析锚点"]))
    if len(anchors) < 2:
        items.append(
            SupplementItem(
                issue="分析锚点偏少，缺少能承接复杂追问的深度页面。",
                target="wiki/analyses",
                action="把最核心的争议或决策问题沉淀成 analysis，再从 topic 的 `## 分析锚点` 双链过去。",
            )
        )

    return items[:5]


def _existing_created(path: Path, fallback: str) -> str:
    if not path.exists():
        return fallback
    created = parse_frontmatter(_read_text(path)).get("created")
    if isinstance(created, str) and created.strip():
        return created.strip()
    return fallback


def _frontmatter(title: str, created: str, updated: str) -> str:
    return "\n".join(
        [
            "---",
            f"title: {title}",
            "type: analysis",
            "status: active",
            f"created: {created}",
            f"updated: {updated}",
            "tags:",
            "  - ai",
            "  - wiki",
            "  - report",
            "  - maintenance",
            "sources: []",
            "---",
            "",
        ]
    )


def _topic_pages(pages: list[Page]) -> list[Page]:
    return [page for page in pages if page.path.parent == WIKI / "topics"]


def _known_pages_by_stem(pages: list[Page]) -> dict[str, Page]:
    return {page.stem: page for page in pages}


def _candidate_related_pages(page: Page, known_pages: dict[str, Page]) -> list[str]:
    related: list[str] = []
    for raw_link in page.links:
        link = _normalize_wikilink(raw_link)
        if link == page.stem:
            continue
        target = known_pages.get(link)
        if target is None:
            continue
        if target.path.parent == WIKI / "sources":
            continue
        if link not in related:
            related.append(link)
    return related[:3]


def render_topic_questions_report(report_date: date | None = None) -> str:
    report_date = report_date or date.today()
    pages = _load_pages()
    topics = _topic_pages(pages)
    known_pages = _known_pages_by_stem(pages)

    lines: list[str] = [
        f"# {REPORT_TITLE}",
        "",
        "## 这份报告做什么",
        "",
        f"- 给每个 `topic` 自动抽取最值得追问的前 {QUESTION_LIMIT} 个问题。",
        "- 问题优先来自主题页自己的正文：`当前判断`、`关键案例`、`分析锚点`、`关联页面`、`高频术语`。",
        "- 排序标准是 `可回答度`：越能从当前 wiki 稳定回答的问题，排得越前。",
        "- 只保留 `可回答度` 为中/高的问题；不足前十时不使用低可答问题补位。",
        "- 低可答问题不会进入前十，而是进入每个 topic 下的 `待补强方向`，说明该补 source、topic 判断还是 analysis；完整 Deep Research 提示词见 [[待补强方向]]。",
        "- 目标不是通用题库，而是把每个 topic 里最有信息密度的内容变成可直接发问的入口。",
        "",
        "## 主题列表",
        "",
    ]

    for page in topics:
        title = str(page.fm.get("title") or page.stem).strip() or page.stem
        related = _candidate_related_pages(page, known_pages)
        candidates = _topic_specific_questions(page, title, related)
        deduped: list[QuestionCandidate] = []
        seen: set[str] = set()
        for candidate in sorted(candidates, key=lambda item: (-item.score, item.source, item.question)):
            normalized = candidate.question.strip()
            if not normalized or normalized in seen:
                continue
            if candidate.score < MIN_ANSWERABILITY_SCORE:
                continue
            seen.add(normalized)
            deduped.append(candidate)
            if len(deduped) == QUESTION_LIMIT:
                break
        lines.extend(
            [
                f"### [[{title}]]",
                "",
                f"- 相关页：{', '.join(f'[[{item}]]' for item in related) if related else '-'}",
            ]
        )
        for idx, candidate in enumerate(deduped, start=1):
            lines.append(
                f"- {idx}. {candidate.question}  （可回答度：{_answerability_label(candidate.score)}，信号：{candidate.source}）"
            )
        supplements = _topic_supplement_items(page)
        if supplements:
            lines.extend(["", "#### [[待补强方向]]", ""])
            for item in supplements:
                lines.append(f"- 缺口：{item.issue}")
                lines.append(f"  - 补到：`{item.target}`")
                lines.append(f"  - 动作：{item.action}")
        lines.append("")

    lines.extend(
        [
            "## 使用方式",
            "",
            "- 从 `[[报告中心]]` 进入这份清单，再回到对应主题页继续补判断或补证据。",
            "- 当某个主题的问题已经稳定，下一步不是重复列问题，而是把答案沉淀回 `主题 / 概念 / 分析`。",
            "",
            f"最后刷新日期：`{report_date.isoformat()}`",
        ]
    )
    return "\n".join(lines) + "\n"


def _deepresearch_prompt(topic: str, item: SupplementItem, idx: int) -> str:
    return "\n".join(
        [
            "```markdown",
            "## 研究主题",
            "",
            item.issue,
            "",
            "## 研究目标",
            "",
            "我正在维护一个由 LLM 协助长期演进的个人 AI wiki，主题集中在 AI 教育、学习能力提升、AI 如何配合学习、孩子在 AI 环境中的成长，以及中国场景下的学习提升。",
            "这个 wiki 不是临时问答记录，而是按照 `raw -> wiki/sources -> wiki/topics -> wiki/analyses -> log` 的流程持续沉淀知识。",
            f"请围绕 `{topic}` 的上述待补强问题生成一份可用于补强这个学习型 wiki 的专业研究报告。",
            f"报告需要帮助我补强 `{item.target}`，并支持后续把资料转成 source note、把稳定结论回写到 topic、必要时沉淀为 analysis。",
            "不要只给泛泛建议，要给出可被沉淀为长期知识的判断、证据、边界条件、反例和可操作框架。",
            "",
            "## 报告要求",
            "",
            "1. 先给出 5-8 条核心结论，每条都说明适用条件和不适用条件。",
            "2. 明确区分事实、解释、推测；证据不足的地方要直接标注。",
            "3. 优先引用高质量来源：论文、官方文档、企业工程实践、一手案例、权威访谈或成熟开源项目文档。",
            "4. 对每个关键来源说明它支持了哪条结论，避免只列参考链接。",
            "5. 给出反例和失败模式，说明哪些做法看似合理但实际容易失效。",
            "6. 给出可以直接转成 wiki 内容的框架、决策表、指标表或检查清单。",
            "7. 最后列出还需要继续验证的问题，以及建议补充的资料类型。",
            "",
            "## 输出结构",
            "",
            "- 执行摘要",
            "- 核心发现",
            "- 概念与定义",
            "- 证据综述",
            "- 框架 / 决策表",
            "- 实践建议",
            "- 失败模式与反例",
            "- 未解决问题",
            "- 来源清单",
            "",
            "## 重点产出",
            "",
            f"- 请优先产出能完成这个动作的内容：{item.action}",
            "- 请额外给出一段 `可回写到 topic 当前判断的候选结论`，用 3-5 条 bullet 表示。",
            "- 请给出一段 `建议新建或更新的 wiki 页面`，说明哪些内容应该进入 `wiki/sources`、哪些应该回写 `wiki/topics`、哪些值得升级为 `wiki/analyses`。",
            "- 请给出一段 `建议的双链关系`，列出应连接的概念页、主题页和分析页。",
            "```",
        ]
    )


def render_supplement_prompts_report(report_date: date | None = None) -> str:
    report_date = report_date or date.today()
    pages = _load_pages()
    topics = _topic_pages(pages)

    lines: list[str] = [
        f"# {SUPPLEMENT_REPORT_TITLE}",
        "",
        "## 这份报告做什么",
        "",
        "- 根据 [[主题擅长问题前十]] 里的 `待补强方向`，为每个主题生成可直接用于 Gemini 深度研究 的提示词。",
        "- 每个提示词都包含 `研究目标 / 报告要求 / 输出结构 / 重点产出`。",
        "- Deep Research 生成报告后，建议放入 `raw/`，再按 ingest 流程转成 source note、analysis 和 topic 回写。",
        "",
        "## 使用流程",
        "",
        "1. 复制某个 topic 下的提示词到 Gemini Deep Research。",
        "2. 将生成的专业报告保存到 `raw/`。",
        "3. 让我执行入库和补强：新建 source note、必要时新建 analysis、回写 topic、更新 log。",
        "",
        "## Deep Research 提示词",
        "",
    ]

    for page in topics:
        topic = str(page.fm.get("title") or page.stem).strip() or page.stem
        supplements = _topic_supplement_items(page)
        if not supplements:
            continue
        lines.extend([f"### [[{topic}]]", ""])
        for idx, item in enumerate(supplements, start=1):
            lines.extend(
                [
                    f"#### {idx}. {item.issue}",
                    "",
                    f"- 补到：`{item.target}`",
                    f"- 动作：{item.action}",
                    "",
                    _deepresearch_prompt(topic, item, idx),
                    "",
                ]
            )

    lines.append(f"最后刷新日期：`{report_date.isoformat()}`")
    return "\n".join(lines) + "\n"


def write_topic_questions_report(report_date: date | None = None) -> Path:
    report_date = report_date or date.today()
    body = render_topic_questions_report(report_date)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    created = _existing_created(REPORT_PATH, report_date.isoformat())
    REPORT_PATH.write_text(
        _frontmatter(REPORT_TITLE, created, report_date.isoformat()) + body.rstrip() + "\n",
        encoding="utf-8",
    )
    return REPORT_PATH


def write_supplement_prompts_report(report_date: date | None = None) -> Path:
    report_date = report_date or date.today()
    body = render_supplement_prompts_report(report_date)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    created = _existing_created(SUPPLEMENT_REPORT_PATH, report_date.isoformat())
    SUPPLEMENT_REPORT_PATH.write_text(
        _frontmatter(SUPPLEMENT_REPORT_TITLE, created, report_date.isoformat()) + body.rstrip() + "\n",
        encoding="utf-8",
    )
    return SUPPLEMENT_REPORT_PATH


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a question map for every topic in the wiki.")
    parser.add_argument("--write", action="store_true", help="Write the report to wiki/report/")
    parser.add_argument("--date", default=None, help="Report date in YYYY-MM-DD format.")
    args = parser.parse_args()

    report_date = None
    if args.date:
        report_date = datetime.strptime(args.date, "%Y-%m-%d").date()

    body = render_topic_questions_report(report_date)
    if args.write:
        write_topic_questions_report(report_date)
        write_supplement_prompts_report(report_date)
    else:
        print(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
