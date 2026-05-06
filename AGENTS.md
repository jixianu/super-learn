# AI Note Wiki Maintenance Guide

本仓库是一个由 LLM 协助维护的个人 AI 知识库。目标不是临时问答，而是持续积累、持续整理、持续修订。

## 目标

- 将原始资料沉淀为可维护的 wiki，而不是把知识留在聊天记录里。
- 让 LLM 在新增资料时主动更新已有页面、补充交叉链接、标记冲突和空白。
- 让提问得到的高质量分析结果也能回写为 wiki 页面。

## 三层结构

- `raw/`: 原始资料层，只读，不修改。可放 PDF、网页剪藏、会议纪要、截图、外部导出的 markdown。
- `wiki/`: 知识库层，由 LLM 维护的 markdown 页面。
- `templates/`: 页面模板，供新建 source note、concept page、analysis page 时复用。

## Wiki 目录约定

- `wiki/index.md`: 总索引，按主题列出页面、一句话摘要、状态。
- `wiki/log.md`: 追加式日志，记录 ingest、query、lint。
- `wiki/overview.md`: AI 知识库总览页，维护主线认知和导航。
- `wiki/concepts/`: 概念页，例如 `Agent.md`、`RAG.md`、`MCP.md`。
- `wiki/topics/`: 主题页，例如 `AI coding.md`、`多模态.md`、`提示工程.md`。
- `wiki/sources/`: 资料摘要页，一份原始资料对应一页。
- `wiki/analyses/`: 回答问题后沉淀下来的分析页、对比页、路线图。
- `wiki/report/`: 日常巡检、健康评分、目录治理、图谱配置等运维报告页。

## 命名规则

- 页面标题直接作为文件名，优先中文，必要时保留英文术语。
- 文件名简洁明确，不加日期前缀，日期写入 frontmatter。
- 主题页和概念页应尽量稳定，避免频繁改名。

## Frontmatter 约定

除日志外，wiki 页面尽量使用 YAML frontmatter：

```yaml
---
title: 页面标题
type: overview|concept|topic|source|analysis
status: seed|active|mature
created: YYYY-MM-DD
updated: YYYY-MM-DD
tags:
  - ai
sources:
  - [[某资料摘要页]]
---
```

## Ingest 工作流

当新增一份资料到 `raw/` 后，LLM 应按以下顺序工作：

1. 阅读资料并提炼核心观点、事实、术语、案例、方法。
2. 在 `wiki/sources/` 新建对应资料摘要页。
3. 更新 `wiki/index.md`。
4. 更新已有的概念页、主题页、总览页；必要时新建页面。
5. 标记冲突、争议、证据不足或后续待验证的问题。
6. 在 `wiki/log.md` 追加一条 ingest 记录。

## Query 工作流

当用户提问时：

1. 先读 `wiki/index.md` 和相关主题页。
2. 从已存在页面中综合回答，不要只依赖原始资料。
3. 若回答形成了稳定知识，应在 `wiki/analyses/` 新建或更新页面。
4. 在 `wiki/log.md` 追加一条 query 记录。

## Analyses 工作流

`wiki/analyses/` 用于沉淀深度研究、横向对比、路线判断、专题判断，不用于承接所有普通资料的 ingest。

- 对普通资料，默认进入 `wiki/sources/`，并将有效增量分发到 `wiki/topics/` 与 `wiki/concepts/`。
- 对重点研究对象，可升级为 `wiki/analyses/` 页面，例如公司、产品、赛道、方法论。
- 当进入 `analyses` 层时，优先采用“横纵分析法”：
  - 纵向：沿时间轴梳理起源、关键节点、演进逻辑、转折与因果。
  - 横向：在当前时间切面对竞品、替代方案、生态位、风险机会做对比。
- “横纵分析法”是深度研究模板，不是全量 ingest 模板。不要对所有来源都套用该写法。
- 一个对象只有在资料积累到足够支撑纵向与横向判断时，才升级为 `analysis` 页面。

## Lint 工作流

定期检查：

- 孤儿页面：没有被其他页面链接到。
- 断裂主题：索引里有条目但缺少核心概念页。
- 过时结论：旧结论已被新资料推翻或弱化。
- 证据不足：存在判断但缺少来源支撑。
- 重复页面：多个页面内容高度重合。

## Codex 维护触发

当用户要你“持续维护”这个知识库时，优先把结果落到 `wiki/report/`，而不是只在聊天里汇报。

- `巡检下载区` / `下载区入库`
  - 先运行 `python3 scripts/wiki_maintenance.py downloads --write`
  - 若存在 `待建 source` 或 `待整合`，继续完成实际入库与回写
- `清理已入库下载`
  - 先确认 `python3 scripts/wiki_maintenance.py downloads --write` 已显示全部 `已入库`
  - 再运行 `python3 scripts/wiki_maintenance.py prune-downloads --write`
- `做一轮知识空白扫描`
  - 运行 `python3 scripts/wiki_maintenance.py gaps --write`
- `生成主题健康报告`
  - 运行 `python3 scripts/wiki_maintenance.py health --write`
- `生成目录治理报告`
  - 运行 `python3 scripts/wiki_maintenance.py structure --write`
- `生成 Topic擅长问题Top10`
  - 运行 `python3 scripts/wiki_maintenance.py questions --write`
- `生成 analyses 维护报告` / `检查 analyses 该怎么收口`
  - 运行 `python3 scripts/wiki_maintenance.py analyses --write`
- `刷新 Obsidian 图谱`
  - 运行 `python3 scripts/wiki_maintenance.py graph --write`
- `跑一轮知识库日常维护` / `做一轮知识库体检`
  - 运行 `python3 scripts/wiki_maintenance.py all --write`

若报告暴露了明确积压，不要停在“生成报告”这一步；应继续按 `raw -> sources -> topics/concepts -> analyses -> log` 完成回写。

## 写作要求

- 优先写“有用的知识组织”，不是堆摘要。
- 每页都尽量包含：
  - 这是什么
  - 为什么重要
  - 和哪些概念相连
  - 当前结论
  - 证据与来源
  - 未解决问题
- 对有争议的问题，明确区分“事实”“解释”“推测”。
- 能双链就双链，保持页面之间可跳转。

## 用户当前主题

当前仓库聚焦 `AI`，尤其偏向：

- AI 应用与落地案例
- AI coding / 编程助手
- 前端与 AI 结合
- 提示工程
- 本地知识库与 agent 工作流

LLM 在新增内容时，优先围绕这些主线组织知识。
