---
name: pdf-wiki-ingest
description: 将 PDF 解析为适合本仓库的中文 wiki 资料页。当前只支持文字版 PDF，提取正文并回写 raw/、wiki/sources/、wiki/index.md 和 wiki/log.md。
---

# PDF 入库到 Wiki

## Overview

当用户要把 PDF 变成这个仓库里的稳定知识时使用这个 skill。它负责从 `raw/pdf/.../source.pdf` 到 `wiki/sources/...md` 的转换，并把稳定结论继续回写到 `wiki/topics/`、`wiki/concepts/`、`wiki/overview.md`、`wiki/index.md` 和 `wiki/log.md`。

## Implementation

实际可执行入口在 [scripts/pdf-wiki-ingest/scripts/pdf_to_wiki.py](scripts/pdf_to_wiki.py)。

依赖约定：

- 文本抽取：`pypdf`

## Workflow

1. 先判断 PDF 是否有可抽取的文本层。
1. 文字版 PDF 直接抽取正文。
1. 如果没有文本层，直接标记为不支持，不再尝试 OCR。
1. 把抽取结果整理成 source note。
1. 更新 wiki 导航和维护记录。

## Type Decision

把输入分成两类处理：

- 文本版 PDF：抽取文本层。
- 非文本版 PDF：直接失败，提示暂不支持。

不要因为文件后缀是 `.pdf` 就默认它真的是 PDF。要检查内容类型或 magic bytes。这个仓库里确实存在名义上是 `.pdf`、实际却是 HTML 登录页或壳页的文件。

## Extraction Rules

- 有确定性文本解析器时，优先用它抽正文。
- 当前版本不做 OCR。
- 保留证据链：原始 URL、raw 路径、PDF 路径、解析限制。
- 区分事实、解释和不确定性。
- 如果解析失败是因为 HTML、登录页或拦截页，要明确标记，不要硬凑摘要。

## Wiki Writeback

当抽取结果足够保留时：

- 新建或更新 `wiki/sources/<title>.md`。
- 保持 frontmatter 符合这个仓库的 source note 约定。
- 补齐 `## 核心观点`、`## 关键事实`、`## 方法 / 框架`、`## 可复用启发`、`## 争议与局限`、`## 关联页面`。
- 把 source note 链接到最相关的 topic 或 concept 页面。
- 更新 `wiki/index.md`，并在 `wiki/log.md` 追加一条入库记录。

当材料不值得保留为知识时：

- 只有在便于后续调试或重跑时，才保留原始产物。
- 不要强行总结，直接标记为 blocked、failed 或 shell content。

## Repo Conventions

- `raw/` 是只读输入层。
- `wiki/sources/` 是 PDF 的第一层稳定知识层。
- `wiki/topics/` 和 `wiki/concepts/` 只收稳定结论，不收全部细节。
- `wiki/analyses/` 只用于确实值得做纵向或横向判断的 PDF。

## Good Triggers

遇到下面这类请求就用这个 skill：

- "把这个 PDF 入库"
- "解析 `raw/pdf/.../source.pdf`"
- "判断这个 PDF 是文本版还是扫描版"
- "把 PDF 解析成 source note"
- "更新 wiki 索引和日志"
