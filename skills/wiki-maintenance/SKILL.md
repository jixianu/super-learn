---
name: wiki-maintenance
description: 用于维护这个仓库的 AI 知识库：巡检 raw/download 新资料、发现知识空白、评估主题健康度、检查目录用途、刷新 Obsidian 图谱设置，或执行完整日常维护。触发场景包括“巡检下载区”“做一轮知识库体检”“生成主题健康报告”“检测空白”“目录治理”“刷新图谱”等。
---

# wiki-maintenance

这个 skill 用于仓库里的例行维护。优先使用内置 CLI，让结果落到 `wiki/report/`，不要只停留在聊天里。

## 主要命令

```bash
python3 scripts/wiki_maintenance.py downloads --write
python3 scripts/wiki_maintenance.py gaps --write
python3 scripts/wiki_maintenance.py health --write
python3 scripts/wiki_maintenance.py structure --write
python3 scripts/wiki_maintenance.py analyses --write
python3 scripts/wiki_maintenance.py graph --write
python3 scripts/wiki_maintenance.py all --write
```

## 流程

1. 运行匹配的子命令。
2. 阅读 `wiki/report/` 里的生成报告。
3. 如果用户要的是实际维护，就不要停在诊断结果，要继续处理：
   - `downloads`：阅读待处理的 raw 资料，写入或更新 `wiki/sources/`，然后更新 `wiki/index.md`、hub 页面和 `wiki/log.md`。
   - `gaps` / `health`：挑最弱的 topic 或 concept，补 source 挂载、analysis 锚点和结构段落。
   - `structure`：把结果当成复核门禁，不是自动删除命令。
   - `analyses`：判断哪些分析页应保留在根目录，哪些快照应移动到 `wiki/analyses/archive/`，哪些治理页仍值得保留在根目录。
   - `graph`：更新 `.obsidian/graph.json`，并保持 `[[报告中心]]` 作为图谱入口页之一。
4. 当维护操作改动了 wiki 内容时，向 `wiki/log.md` 追加一条 `maintenance` 记录。

## 备注

- 不要为了清队列而创建只有占位内容的 source note。
- 优先保留 `wiki/report/` 里的稳定页面，不要堆日期化的一次性输出。
- `python3 scripts/wiki_maintenance.py all --write` 是默认的日常维护入口。
