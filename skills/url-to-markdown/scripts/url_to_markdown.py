#!/usr/bin/env python3

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import mimetypes
import os
import re
import ssl
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

VOID_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}
SKIP_TAGS = {"script", "style", "noscript", "svg", "canvas", "iframe"}
HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
CJK_PATTERN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


def build_ssl_context() -> ssl.SSLContext:
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


@dataclass
class Node:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list["Node"] = field(default_factory=list)
    text: str = ""


class DOMBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("document")
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = Node(tag=tag.lower(), attrs={k.lower(): v or "" for k, v in attrs})
        self.stack[-1].children.append(node)
        if tag.lower() not in VOID_TAGS:
            self.stack.append(node)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data: str) -> None:
        if data:
            self.stack[-1].children.append(Node(tag="#text", text=data))


def slugify(value: str, fallback: str = "page") -> str:
    value = value.strip().lower()
    value = re.sub(r"[^\w\s-]", "", value, flags=re.UNICODE)
    value = re.sub(r"[-\s]+", "-", value).strip("-_")
    return value or fallback


def sanitize_path_component(value: str, fallback: str = "page") -> str:
    value = re.sub(r'[\\/:*?"<>|]', " ", value).strip()
    value = re.sub(r"\s+", " ", value)
    value = value.rstrip(".")
    return value or fallback


def normalize_note_title(value: str, fallback: str = "未命名页面") -> str:
    value = clean_inline_text(value)
    value = re.sub(r"^\s*GitHub\s*-\s*", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s*[·|｜]\s*GitHub\s*$", "", value, flags=re.IGNORECASE)
    value = value.replace("/", "／").replace("\\", "＼")
    value = re.sub(r"\s+", " ", value).strip().rstrip(".")
    if len(value) > 120:
        value = value[:120].rstrip()
    return value or fallback


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    index = 2
    while True:
        candidate = path.with_name(f"{path.name} {index}")
        if not candidate.exists():
            return candidate
        index += 1


def rename_output_dir(output_dir: Path, title: str) -> Path:
    target = unique_path(output_dir.parent / sanitize_path_component(title, fallback=output_dir.name))
    if target == output_dir:
        return output_dir
    output_dir.rename(target)
    return target


def clean_inline_text(text: str) -> str:
    text = unescape(text).replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def collapse_blank_lines(text: str) -> str:
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + "\n"


def yaml_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def is_cjk(char: str) -> bool:
    return bool(char) and bool(CJK_PATTERN.match(char))


def needs_space(previous: str, current: str) -> bool:
    if not previous or not current:
        return False
    prev_last = previous[-1]
    curr_first = current[0]
    if prev_last.isspace() or curr_first.isspace():
        return False
    if prev_last in "([{/<\n":
        return False
    if curr_first in ").,;:!?]}%>，。！？；：、":
        return False
    if is_cjk(prev_last) or is_cjk(curr_first):
        return False
    return True


def text_content(node: Node) -> str:
    if node.tag == "#text":
        return node.text
    return "".join(text_content(child) for child in node.children)


def iter_nodes(node: Node) -> Iterable[Node]:
    yield node
    for child in node.children:
        yield from iter_nodes(child)


def find_first(node: Node, tags: set[str]) -> Node | None:
    for current in iter_nodes(node):
        if current.tag in tags:
            return current
    return None


def pick_main_content(root: Node) -> Node:
    candidates: list[tuple[float, Node]] = []
    for node in iter_nodes(root):
        if node.tag not in {"article", "main", "section", "div", "body"}:
            continue
        attrs = " ".join(
            [node.attrs.get("class", ""), node.attrs.get("id", ""), node.attrs.get("role", "")]
        ).lower()
        text = clean_inline_text(text_content(node))
        if len(text) < 120:
            continue
        score = float(len(text))
        if node.tag in {"article", "main"}:
            score += 1500
        if any(word in attrs for word in ("article", "content", "post", "main", "markdown", "doc")):
            score += 1200
        if any(word in attrs for word in ("nav", "footer", "header", "sidebar", "menu", "comment")):
            score -= 2000
        score += text.count("。") * 3
        score += text.count(". ") * 2
        candidates.append((score, node))

    if candidates:
        candidates.sort(key=lambda item: item[0], reverse=True)
        return candidates[0][1]

    body = find_first(root, {"body"})
    return body or root


def node_attr(node: Node, *names: str) -> str:
    for name in names:
        value = node.attrs.get(name, "")
        if value:
            return value
    return ""


class AssetManager:
    def __init__(self, assets_dir: Path, base_url: str | None) -> None:
        self.assets_dir = assets_dir
        self.base_url = base_url or ""
        self.assets_dir.mkdir(parents=True, exist_ok=True)
        self.cache: dict[str, str] = {}
        self.counter = 0

    def save_image(self, src: str) -> str | None:
        if not src:
            return None
        if src in self.cache:
            return self.cache[src]

        if src.startswith("data:"):
            saved = self._save_data_url(src)
        else:
            saved = self._download_url(src)

        if saved:
            self.cache[src] = saved
        return saved

    def _save_data_url(self, src: str) -> str | None:
        match = re.match(r"data:(image/[-+\w.]+)?(;base64)?,(.*)", src, flags=re.IGNORECASE | re.DOTALL)
        if not match:
            return None
        mime_type = match.group(1) or "image/png"
        payload = match.group(3)
        try:
            raw = base64.b64decode(payload) if match.group(2) else payload.encode("utf-8")
        except Exception:
            return None
        ext = mimetypes.guess_extension(mime_type) or ".bin"
        return self._write_bytes(raw, f"image-{self.counter + 1}{ext}")

    def _download_url(self, src: str) -> str | None:
        absolute = urljoin(self.base_url, src) if self.base_url else src
        absolute = absolute.replace("http://mmbiz.qpic.cn/", "https://mmbiz.qpic.cn/")
        absolute = absolute.replace("http://mmbiz.qpic.cn", "https://mmbiz.qpic.cn")
        base_host = urlparse(self.base_url or "").netloc.lower()
        referer = self.base_url if base_host else ""
        request = Request(
            absolute,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; url-to-markdown/1.0)",
                "Accept": "image/*,*/*;q=0.8",
                **({"Referer": referer} if referer and "mp.weixin.qq.com" in base_host else {}),
            },
        )
        try:
            with urlopen(request, timeout=20, context=build_ssl_context()) as response:
                raw = response.read()
                content_type = response.headers.get_content_type()
        except Exception:
            return None

        parsed = urlparse(absolute)
        original_name = Path(parsed.path).name or ""
        safe_name = slugify(Path(original_name).stem, fallback=f"image-{self.counter + 1}")
        ext = Path(original_name).suffix
        if not ext:
            ext = mimetypes.guess_extension(content_type or "") or ".bin"
        return self._write_bytes(raw, f"{safe_name}{ext}")

    def _write_bytes(self, raw: bytes, preferred_name: str) -> str:
        digest = hashlib.sha1(raw).hexdigest()[:12]
        suffix = Path(preferred_name).suffix or ".bin"
        stem = slugify(Path(preferred_name).stem, fallback="image")
        if len(stem) > 80:
            stem = stem[:80].rstrip("-_") or "image"
        filename = f"{stem}-{digest}{suffix}"
        path = self.assets_dir / filename
        if not path.exists():
            self.counter += 1
            path.write_bytes(raw)
        return f"assets/{filename}"


class MarkdownRenderer:
    def __init__(self, base_url: str | None, asset_manager: AssetManager) -> None:
        self.base_url = base_url or ""
        self.asset_manager = asset_manager

    def render(self, node: Node) -> str:
        rendered = self._render_block(node, list_depth=0)
        return collapse_blank_lines(rendered)

    def _render_children(self, node: Node, list_depth: int) -> str:
        parts = [self._render_block(child, list_depth=list_depth) for child in node.children]
        return "".join(part for part in parts if part)

    def _render_inline(self, node: Node) -> str:
        if node.tag == "#text":
            return clean_inline_text(node.text)
        if node.tag in SKIP_TAGS:
            return ""
        if node.tag == "br":
            return "\n"
        if node.tag in {"strong", "b"}:
            inner = self._render_inline_children(node)
            return f"**{inner}**" if inner else ""
        if node.tag in {"em", "i"}:
            inner = self._render_inline_children(node)
            return f"*{inner}*" if inner else ""
        if node.tag == "code":
            inner = clean_inline_text(text_content(node))
            if not inner:
                return ""
            ticks = "``" if "`" in inner else "`"
            return f"{ticks}{inner}{ticks}"
        if node.tag == "a":
            href = node_attr(node, "href")
            href = urljoin(self.base_url, href) if href and self.base_url else href
            label = self._render_inline_children(node) or href
            if not href:
                return label
            if label == href:
                return f"<{href}>"
            return f"[{label}]({href})"
        if node.tag == "img":
            src = node_attr(node, "src") or ""
            data_src = node_attr(node, "data-src", "data-original", "data-lazy-src") or ""
            # WeChat and other sites often put a placeholder in src (data URL) and the real image in data-src.
            if (not src or src.startswith("data:")) and data_src:
                src = data_src
            alt = clean_inline_text(node_attr(node, "alt")) or "image"
            local_path = self.asset_manager.save_image(src)
            return f"![{alt}]({local_path})" if local_path else ""
        return self._render_inline_children(node)

    def _render_inline_children(self, node: Node) -> str:
        parts: list[str] = []
        for child in node.children:
            piece = self._render_inline(child)
            if not piece:
                continue
            if parts and needs_space(parts[-1], piece):
                parts.append(" ")
            parts.append(piece)
        return "".join(parts).strip()

    def _render_block(self, node: Node, list_depth: int) -> str:
        if node.tag == "#text":
            return clean_inline_text(node.text)
        if node.tag in SKIP_TAGS:
            return ""
        if node.tag in HEADING_TAGS:
            level = int(node.tag[1])
            text = self._render_inline_children(node)
            return f"\n{'#' * level} {text}\n\n" if text else ""
        if node.tag == "p":
            text = self._render_inline_children(node)
            return f"\n{text}\n\n" if text else ""
        if node.tag in {"nav", "header", "footer", "aside"}:
            return ""
        if node.tag in {"article", "main", "section", "div", "body", "figure", "figcaption"}:
            content = self._render_children(node, list_depth=list_depth)
            return f"\n{content}\n" if content.strip() else ""
        if node.tag == "blockquote":
            inner = collapse_blank_lines(self._render_children(node, list_depth=list_depth)).strip()
            if not inner:
                return ""
            quoted = "\n".join(f"> {line}" if line else ">" for line in inner.splitlines())
            return f"\n{quoted}\n\n"
        if node.tag == "pre":
            code = text_content(node).strip("\n")
            if not code:
                return ""
            language = self._detect_code_language(node)
            fence = f"```{language}".rstrip()
            return f"\n{fence}\n{code}\n```\n\n"
        if node.tag == "hr":
            return "\n---\n\n"
        if node.tag == "ul":
            return self._render_list(node, ordered=False, list_depth=list_depth)
        if node.tag == "ol":
            return self._render_list(node, ordered=True, list_depth=list_depth)
        if node.tag == "table":
            return self._render_table(node)
        if node.tag == "img":
            inline = self._render_inline(node)
            return f"\n{inline}\n\n" if inline else ""
        if node.tag in {"li", "tbody", "thead", "tr", "td", "th"}:
            return self._render_children(node, list_depth=list_depth)
        return self._render_inline_children(node)

    def _render_list(self, node: Node, ordered: bool, list_depth: int) -> str:
        lines: list[str] = []
        index = 1
        for child in node.children:
            if child.tag != "li":
                continue
            item = collapse_blank_lines(self._render_children(child, list_depth=list_depth + 1)).strip()
            if not item:
                continue
            prefix = f"{index}. " if ordered else "- "
            continuation = " " * len(prefix)
            item_lines = item.splitlines()
            lines.append(prefix + item_lines[0])
            lines.extend(continuation + line if line else "" for line in item_lines[1:])
            index += 1
        if not lines:
            return ""
        indent = "  " * list_depth
        content = "\n".join(indent + line if line else "" for line in lines)
        return f"\n{content}\n\n"

    def _render_table(self, node: Node) -> str:
        rows: list[list[str]] = []
        for current in iter_nodes(node):
            if current.tag != "tr":
                continue
            cells: list[str] = []
            for cell in current.children:
                if cell.tag not in {"td", "th"}:
                    continue
                text = collapse_blank_lines(self._render_children(cell, list_depth=0)).replace("\n", " ").strip()
                cells.append(text or " ")
            if cells:
                rows.append(cells)
        if not rows:
            return ""
        width = max(len(row) for row in rows)
        normalized = [row + [" "] * (width - len(row)) for row in rows]
        header = normalized[0]
        separator = ["---"] * width
        table_lines = [
            "| " + " | ".join(header) + " |",
            "| " + " | ".join(separator) + " |",
        ]
        for row in normalized[1:]:
            table_lines.append("| " + " | ".join(row) + " |")
        return "\n" + "\n".join(table_lines) + "\n\n"

    def _detect_code_language(self, node: Node) -> str:
        for current in iter_nodes(node):
            class_name = current.attrs.get("class", "")
            for token in class_name.split():
                if token.startswith("language-"):
                    return token.removeprefix("language-")
                if token.startswith("lang-"):
                    return token.removeprefix("lang-")
        return ""


def run_agent_browser(session: str, *args: str) -> str:
    command = ["agent-browser", "--session", session, *args]
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"agent-browser failed: {' '.join(command)}")
    return result.stdout.strip()


def fetch_rendered_html(url: str, session: str) -> tuple[str, str, str]:
    run_agent_browser(session, "open", url)
    run_agent_browser(session, "wait", "1500")
    final_url = run_agent_browser(session, "get", "url")
    title = run_agent_browser(session, "get", "title")
    html = run_agent_browser(session, "get", "html", "html")
    if not html:
        raise RuntimeError("agent-browser returned empty HTML")
    return final_url, title, html


def fetch_markdown_with_tavily(url: str) -> tuple[str, str, str, str]:
    tavily_api_key = (os.environ.get("TAVILY_API_KEY") or "").strip()
    if not tavily_api_key:
        raise RuntimeError("TAVILY_API_KEY is not set")

    payload = json.dumps(
        {
            "urls": url,
            "extract_depth": "advanced",
            "format": "markdown",
            "include_images": True,
            "include_favicon": True,
            "timeout": 30,
        }
    )
    request = Request(
        "https://api.tavily.com/extract",
        data=payload.encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {tavily_api_key}",
        },
        method="POST",
    )
    with urlopen(request, timeout=40, context=build_ssl_context()) as response:
        raw = response.read().decode("utf-8")
    body = json.loads(raw)
    results = body.get("results") or []
    if not results:
        failed_results = body.get("failed_results") or []
        if failed_results:
            first = failed_results[0]
            raise RuntimeError(first.get("error") or "tavily extract failed")
        raise RuntimeError("tavily extract returned no results")

    first_result = results[0]
    markdown = (first_result.get("raw_content") or "").strip()
    if not markdown:
        raise RuntimeError("tavily extract returned empty markdown")
    return first_result.get("url") or url, "", markdown, raw


def infer_title_from_markdown(markdown: str) -> str:
    for raw_line in markdown.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            return line.lstrip("#").strip()
        if len(line) <= 120:
            return line
    return ""


def fetch_html_with_curl(url: str) -> tuple[str, str, str]:
    result = subprocess.run(
        [
            "curl",
            "-L",
            "--fail",
            "--silent",
            "--show-error",
            "-A",
            "Mozilla/5.0 (compatible; url-to-markdown/1.0)",
            url,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "curl fetch failed")
    html = result.stdout
    if not html.strip():
        raise RuntimeError("curl returned empty HTML")
    return url, "", html


def read_html(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8", "utf-8-sig", "gb18030", "big5", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def build_markdown(title: str, source_url: str, html: str, output_dir: Path) -> str:
    parser = DOMBuilder()
    parser.feed(html)
    main = pick_main_content(parser.root)
    asset_manager = AssetManager(output_dir / "assets", source_url)
    renderer = MarkdownRenderer(source_url, asset_manager)
    body = renderer.render(main)
    body = clean_wechat_noise_markdown(body, source_url)

    saved_at = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
    parts = [f"# {title or 'Untitled Page'}", ""]
    if source_url:
        parts.append(f"- Source: <{source_url}>")
    parts.append(f"- Saved: {saved_at}")
    parts.append("- HTML: [source.html](source.html)")
    parts.append("")
    parts.append("---")
    parts.append("")
    parts.append(body.strip())
    parts.append("")
    return collapse_blank_lines("\n".join(parts))


def localize_markdown_images(markdown: str, source_url: str, output_dir: Path) -> str:
    asset_manager = AssetManager(output_dir / "assets", source_url)
    image_pattern = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")

    def replace(match: re.Match[str]) -> str:
        alt = clean_inline_text(match.group(1)) or "image"
        src = match.group(2).strip()
        if not src or src.startswith("assets/"):
            return match.group(0)
        local_path = asset_manager.save_image(src)
        if not local_path:
            return match.group(0)
        return f"![{alt}]({local_path})"

    return image_pattern.sub(replace, markdown)


def build_markdown_from_extract(title: str, source_url: str, markdown: str, output_dir: Path) -> str:
    saved_at = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
    localized_markdown = localize_markdown_images(markdown, source_url, output_dir)
    localized_markdown = clean_wechat_noise_markdown(localized_markdown, source_url)
    parts = [f"# {title or 'Untitled Page'}", ""]
    if source_url:
        parts.append(f"- Source: <{source_url}>")
    parts.append(f"- Saved: {saved_at}")
    parts.append("- Extract: [source.tavily.json](source.tavily.json)")
    parts.append("")
    parts.append("---")
    parts.append("")
    parts.append(localized_markdown.strip())
    parts.append("")
    return collapse_blank_lines("\n".join(parts))


def clean_wechat_noise_markdown(markdown: str, source_url: str) -> str:
    host = urlparse(source_url or "").netloc.lower()
    if "mp.weixin.qq.com" not in host:
        return markdown

    # 1) Drop known boilerplate lines that often appear near the top of WeChat captures.
    drop_line_patterns = (
        r"^\s*在小说阅读器读本章\s*$",
        r"^\s*去阅读\s*$",
        r"^\s*在小说阅读器中沉浸阅读\s*$",
        r"^\s*分享留言收藏听过\s*$",
        r"^\s*点击上方蓝字关注.*$",
        r"^\s*点击上方蓝字关注我们.*$",
    )
    drop_re = re.compile("|".join(f"(?:{p})" for p in drop_line_patterns))
    lines = [line for line in markdown.splitlines() if not drop_re.match(line.strip())]
    text = "\n".join(lines)

    # 2) Remove noisy tail modules. Some are strong end-markers, others are applied only if near the end.
    strong_truncate_markers = (
        "预览时标签不可点",
        "微信扫一扫可打开此内容",
        "Scan to Follow",
        "继续滑动看下一个",
        "Got It",
        "Cancel",
        "Allow",
    )
    late_truncate_markers = (
        "微信扫一扫赞赏作者",
        "赞赏作者",
        "写留言",
        "精选留言",
        "投诉",
        "举报",
        "推荐阅读",
        "视频小程序赞",
        "轻点两下取消赞在看",
        "轻点两下取消在看",
    )

    def truncate_at(marker: str) -> None:
        nonlocal text
        idx = text.find(marker)
        if idx != -1:
            text = text[:idx].rstrip()

    for marker in strong_truncate_markers:
        if marker in text:
            truncate_at(marker)
            break

    if text:
        for marker in late_truncate_markers:
            idx = text.find(marker)
            if idx == -1:
                continue
            # Only treat as tail module if it appears late, to avoid removing legitimate content.
            if idx / max(len(text), 1) >= 0.75:
                text = text[:idx].rstrip()
                break

    # 3) Remove common "recommendation" section if rendered as a heading.
    # Keep it conservative: drop only when it's a dedicated section.
    text = re.sub(r"(?ms)^\s*#{1,6}\s*推荐阅读\s*$.*?(?=^\s*#{1,6}\s+|\Z)", "", text)
    text = re.sub(r"(?ms)^\s*#{1,6}\s*(精选留言|写留言|投诉|举报)\s*$.*?(?=^\s*#{1,6}\s+|\Z)", "", text)

    return collapse_blank_lines(text).strip()


def render_with_tavily(
    source_url: str,
    title: str,
    tavily_markdown: str,
    tavily_raw: str,
    output_dir: Path,
) -> tuple[str, str, str, bool]:
    if not title:
        title = infer_title_from_markdown(tavily_markdown)
    title = normalize_note_title(title)
    tavily_source_path = output_dir / "source.tavily.json"
    tavily_source_path.write_text(tavily_raw, encoding="utf-8")
    markdown = build_markdown_from_extract(
        title=title,
        source_url=source_url,
        markdown=tavily_markdown,
        output_dir=output_dir,
    )
    (output_dir / "source.html").write_text("", encoding="utf-8")
    (output_dir / "index.md").write_text(markdown, encoding="utf-8")
    return title, markdown, source_url, True


def summarize_markdown(markdown: str, limit: int = 5) -> list[str]:
    lines: list[str] = []
    for raw_line in markdown.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", "---")):
            continue
        if line.startswith("- Source:") or line.startswith("- Saved:") or line.startswith("- HTML:"):
            continue
        if line.startswith(("![", "|", "```")):
            continue
        lines.append(line)
        if len(lines) >= limit:
            break
    return lines


def is_blocked_page(title: str, source_url: str, markdown: str) -> bool:
    text = f"{title}\n{source_url}\n{markdown}".lower()
    hard_blocked_markers = (
        "当前环境异常，完成验证后即可继续访问",
        "该内容已被发布者删除",
        "微信公众平台运营中心",
        "refreshing too often",
        "verification code will refresh",
        "just a moment...",
        "this website uses a security service to protect against malicious bots",
        "没有权限访问",
        "请输入密码访问",
        "page not found · github",
        "file not found · github",
        "there isn’t a github pages site here",
        "去验证",
        "验证码",
        "appmsgcaptcha",
        "wappoc_appmsgcaptcha",
    )
    soft_blocked_markers = (
        "视频小程序赞，轻点两下取消赞在看",
    )
    if title.strip() == "微信公众平台":
        return True
    if any(marker.lower() in text for marker in hard_blocked_markers):
        # 微信正文常会夹带一些平台噪音，若正文已经足够长，就不应误判为受限页。
        body = markdown.strip()
        has_substantial_content = len(body) >= 2000 and body.count("\n") >= 20
        if has_substantial_content:
            return False
        return True
    return any(marker.lower() in text for marker in soft_blocked_markers) and len(markdown.strip()) < 1200


def infer_related_pages(title: str, source_url: str, markdown: str) -> list[str]:
    text = f"{title}\n{source_url}\n{markdown}".lower()
    related: list[str] = []

    def add(page: str) -> None:
        if page not in related:
            related.append(page)

    keyword_map = {
        "AI Coding": ["codex", "claude", "代码", "编程", "pr", "github", "审核", "review", "routine"],
        "Agent": ["agent", "代理", "codex", "claude", "automation", "自动化", "routine"],
        "MCP": ["mcp"],
        "提示工程": ["prompt", "提示词", "提示工程"],
        "个人 AI 知识库": ["wiki", "知识库", "obsidian", "source note"],
        "Wiki-first Knowledge Base": ["wiki-first", "知识库", "wiki"],
        "RAG": ["rag", "检索"],
        "AI 应用与落地案例": ["应用", "落地", "case", "案例"],
    }

    for page, keywords in keyword_map.items():
        if any(keyword in text for keyword in keywords):
            add(page)

    return related


def build_source_note(
    title: str,
    source_url: str,
    markdown_relpath: str,
    html_relpath: str,
    markdown: str,
    related_pages: list[str],
) -> str:
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    bullets = summarize_markdown(markdown, limit=5)
    core_points = "\n".join(f"- {line}" for line in bullets) if bullets else "- 待补充"
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
                f"- 抓取产物：[{markdown_relpath}]({markdown_relpath})",
                f"- 原始 HTML：[${html_relpath}]({html_relpath})".replace("$", ""),
                "",
                "## 方法 / 框架",
                "- 待补充",
                "",
                "## 可复用启发",
                "- 待补充",
                "",
                "## 争议与局限",
                "- 当前为自动转写结果，尚未人工清洗结构与事实。",
                "",
                "## 关联页面",
                *related_lines,
                "",
            ]
        )
    )


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
    parser = argparse.ArgumentParser(
        description="Fetch a webpage with agent-browser and convert rendered HTML to local Markdown."
    )
    source_group = parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument("--url", help="URL to open with agent-browser.")
    source_group.add_argument("--html-file", type=Path, help="Previously saved HTML file to convert.")
    parser.add_argument("--output", type=Path, required=True, help="Output directory.")
    parser.add_argument("--base-url", default="", help="Base URL for resolving links and images.")
    parser.add_argument(
        "--source-url",
        default="",
        help="Override the Source URL written into index.md and wiki source note (useful when fetching raw/plain mirrors).",
    )
    parser.add_argument("--title", default="", help="Optional title override.")
    parser.add_argument("--session", default="url-to-markdown", help="agent-browser session name.")
    parser.add_argument(
        "--prefer-tavily",
        action="store_true",
        help="Prefer Tavily extract first (falls back to browser/curl). Requires TAVILY_API_KEY.",
    )
    parser.add_argument(
        "--use-title-dir",
        action="store_true",
        help="Rename the output directory to the final page title after fetching.",
    )
    parser.add_argument(
        "--wiki-source-note",
        action="store_true",
        help="Also generate a source note draft under wiki/sources in the current repository.",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path.cwd(),
        help="Repository root used with --wiki-source-note. Defaults to current directory.",
    )
    return parser.parse_args()


def looks_like_html(text: str) -> bool:
    head = text.lstrip()[:500].lower()
    return "<html" in head or "<!doctype html" in head or "<head" in head or "<body" in head


def looks_like_markdown(text: str) -> bool:
    if not text or looks_like_html(text):
        return False
    head = text.lstrip()[:2000]
    if head.startswith("#"):
        return True
    if "\n# " in head or "\n## " in head:
        return True
    if "\n```" in head:
        return True
    if "\n- " in head and "\n\n" in head:
        return True
    return False


def extract_markdown_from_pre(html: str) -> str:
    """
    Some "raw text" endpoints (e.g. raw file views) are served as a minimal HTML
    wrapper containing a single <pre>. If we convert that HTML, we end up with one
    giant fenced code block. Instead, extract the <pre> content as plaintext/markdown.
    """
    if not html or not looks_like_html(html):
        return ""
    match = re.search(r"(?is)<pre[^>]*>(.*?)</pre>", html)
    if not match:
        return ""
    content = match.group(1)
    # If it contains other structural tags, it's probably real HTML.
    if re.search(r"(?is)<(div|span|a|script|style|img|table|article)\\b", content):
        return ""
    return unescape(content).strip("\n")


def strip_redundant_title(markdown: str, title: str) -> str:
    if not markdown.strip() or not title.strip():
        return markdown
    lines = markdown.splitlines()
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i >= len(lines):
        return markdown
    first = lines[i].strip()
    if first.startswith("#"):
        first_title = first.lstrip("#").strip()
        if first_title and first_title.lower() == title.strip().lower():
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            return "\n".join(lines[j:]).lstrip("\n")
    return markdown


def build_markdown_from_markdown(title: str, source_url: str, raw_markdown: str) -> str:
    saved_at = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
    body = strip_redundant_title(raw_markdown, title).strip()
    parts = [f"# {title or 'Untitled Page'}", ""]
    if source_url:
        parts.append(f"- Source: <{source_url}>")
    parts.append(f"- Saved: {saved_at}")
    parts.extend(["", "---", ""])
    if body:
        parts.append(body)
    return "\n".join(parts).rstrip() + "\n"


def main() -> int:
    args = parse_args()
    output_dir: Path = args.output
    output_dir.mkdir(parents=True, exist_ok=True)
    used_tavily = False
    tavily_raw = ""
    markdown_mode = False

    try:
        if args.url:
            if args.prefer_tavily:
                try:
                    source_url, title, tavily_markdown, tavily_raw = fetch_markdown_with_tavily(args.url)
                    used_tavily = True
                except Exception:
                    pass
            if not used_tavily:
                try:
                    source_url, title, html = fetch_rendered_html(args.url, args.session)
                except Exception:
                    try:
                        source_url, title, tavily_markdown, tavily_raw = fetch_markdown_with_tavily(args.url)
                        used_tavily = True
                    except Exception:
                        source_url, title, html = fetch_html_with_curl(args.url)
                recovered = extract_markdown_from_pre(html)
                if recovered:
                    html = recovered
                    markdown_mode = True
                elif looks_like_markdown(html):
                    markdown_mode = True
        else:
            html = read_html(args.html_file)
            source_url = args.base_url
            title = args.title
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    canonical_source_url = args.source_url.strip() or source_url

    if not title:
        if used_tavily:
            title = infer_title_from_markdown(tavily_markdown)
        elif markdown_mode:
            title = infer_title_from_markdown(html)
    if not title and not used_tavily:
        parser = DOMBuilder()
        parser.feed(html)
        title_node = find_first(parser.root, {"title", "h1"})
        title = clean_inline_text(text_content(title_node)) if title_node else ""
    if not title:
        title = slugify(canonical_source_url or args.html_file.stem, fallback="untitled-page").replace("-", " ").title()
    title = normalize_note_title(title)

    if args.use_title_dir:
        output_dir = rename_output_dir(output_dir, title)

    source_path = output_dir / "source.html"
    markdown_path = output_dir / "index.md"
    if used_tavily:
        title, markdown, source_url, used_tavily = render_with_tavily(
            source_url=canonical_source_url,
            title=title,
            tavily_markdown=tavily_markdown,
            tavily_raw=tavily_raw,
            output_dir=output_dir,
        )
    else:
        source_path.write_text(html, encoding="utf-8")
        if markdown_mode:
            markdown = build_markdown_from_markdown(title=title, source_url=canonical_source_url, raw_markdown=html)
        else:
            markdown = build_markdown(title=title, source_url=canonical_source_url, html=html, output_dir=output_dir)
        markdown_path.write_text(markdown, encoding="utf-8")

    source_note_path: Path | None = None
    if args.wiki_source_note:
        blocked_page = is_blocked_page(title, canonical_source_url, markdown)
        if blocked_page and args.url and not used_tavily:
            try:
                source_url, tavily_title, tavily_markdown, tavily_raw = fetch_markdown_with_tavily(args.url)
                title, markdown, source_url, used_tavily = render_with_tavily(
                    source_url=canonical_source_url,
                    title=tavily_title or title,
                    tavily_markdown=tavily_markdown,
                    tavily_raw=tavily_raw,
                    output_dir=output_dir,
                )
                markdown_path = output_dir / "index.md"
                source_path = output_dir / "source.html"
                blocked_page = is_blocked_page(title, canonical_source_url, markdown)
            except Exception:
                pass
        today = datetime.now().astimezone().strftime("%Y-%m-%d")
        try:
            markdown_relpath = markdown_path.relative_to(args.repo_root).as_posix()
        except ValueError:
            markdown_relpath = markdown_path.resolve().as_posix()
        if blocked_page:
            print("Skipped wiki ingest: blocked page detected.")
        else:
            wiki_sources_dir = args.repo_root / "wiki" / "sources"
            wiki_sources_dir.mkdir(parents=True, exist_ok=True)
            source_note_path = wiki_sources_dir / f"{title}.md"
            try:
                source_relpath = (
                    (output_dir / "source.tavily.json").relative_to(args.repo_root).as_posix()
                    if used_tavily
                    else source_path.relative_to(args.repo_root).as_posix()
                )
            except ValueError:
                source_relpath = (
                    (output_dir / "source.tavily.json").resolve().as_posix()
                    if used_tavily
                    else source_path.resolve().as_posix()
                )
            source_note = build_source_note(
                title=title,
                source_url=canonical_source_url,
                markdown_relpath=markdown_relpath,
                html_relpath=source_relpath,
                markdown=markdown,
                related_pages=infer_related_pages(title, canonical_source_url, markdown),
            )
            source_note_path.write_text(source_note, encoding="utf-8")
            summary_lines = summarize_markdown(markdown, limit=1)
            summary = summary_lines[0] if summary_lines else "自动抓取网页并转换为 Markdown。"
            append_source_to_index(args.repo_root / "wiki" / "index.md", title, summary, today)
            append_ingest_log(
                args.repo_root / "wiki" / "log.md",
                today,
                title,
                [
                    f"使用 `url-to-markdown` 抓取网页并保存到 `{markdown_relpath}`。",
                    f"生成资料摘要页 `wiki/sources/{title}.md`。",
                    "自动更新 `wiki/index.md` 与 `wiki/log.md`。",
                ],
            )
    print(f"Markdown: {markdown_path}")
    print(f"HTML: {source_path}")
    print(f"Assets: {output_dir / 'assets'}")
    if source_note_path:
        print(f"Source note: {source_note_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
