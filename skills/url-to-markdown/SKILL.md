---
name: url-to-markdown
description: 将网页打开、保存为本地 HTML，下载引用图片，并转换成结构清晰的 UTF-8 Markdown。用于抓网页、归档页面为 Markdown、保留图片本地副本，或把 URL 转成不乱码的笔记。
---

# url-to-markdown

当输出必须是本地 Markdown 产物，而不是临时摘要时，使用这个 skill。

## 这个 skill 做什么

- 使用 `agent-browser` 打开 URL。
- 如果 `agent-browser` 失败且有 `TAVILY_API_KEY`，就回退到 Tavily Extract，并使用返回的 markdown。
- 如果 Tavily 不可用或失败，再回退到 `curl`。
- 将渲染后的 HTML 以 UTF-8 保存。
- 把页面图片下载到本地 `assets/` 目录。
- 将正文转换成可读 Markdown。
- 需要时可以为这个仓库生成 `wiki/sources/` 的 source note 草稿。
- 保留中文和其他 Unicode 文本，不转成旧编码。

## 默认输出结构

```text
<output>/
├── index.md
├── source.html
└── assets/
```

## 推荐流程

1. 普通网页优先使用内置脚本：

```bash
python3 skills/url-to-markdown/scripts/url_to_markdown.py \
  --url "https://example.com/article" \
  --output "/tmp/example-article"
```

如果你还想同时生成本仓库里的 source note 草稿：

```bash
python3 skills/url-to-markdown/scripts/url_to_markdown.py \
  --url "https://example.com/article" \
  --output "raw/example-article" \
  --wiki-source-note
```

2. 如果页面需要先人工交互，再用 `agent-browser` 到达最终状态，导出 HTML 后再转换：

```bash
agent-browser --session article open "https://example.com/article"
agent-browser --session article wait 1500
agent-browser --session article get html html > /tmp/article.html
python3 skills/url-to-markdown/scripts/url_to_markdown.py \
  --html-file /tmp/article.html \
  --base-url "https://example.com/article" \
  --output "/tmp/example-article"
```

## 备注

- 优先使用脚本，不要手写转换逻辑。它已经处理了标题捕获、正文抽取、本地图片下载、空白清理和 UTF-8 输出。
- 这个 Markdown 是为笔记和 wiki 入库优化的，不追求像素级复刻原页面。
- `--wiki-source-note` writes a draft page to `wiki/sources/<title>.md` and also updates `wiki/index.md` and `wiki/log.md`.
- 对于 JS 极重的页面，先在 `agent-browser` 里加载完整页面，再导出 HTML，最后走 `--html-file`。
- Tavily Extract 官方接口返回的是 `markdown/text`，不是 `html`。因此 Tavily 兜底模式下会额外保存 `source.tavily.json`，再直接生成 Markdown。
