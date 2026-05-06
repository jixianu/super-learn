

## 第五步：一条指令，AI把笔记编成维基

读取 raw/ 中的所有内容。然后按照 CLAUDE.md 中的规则在 wiki/ 中编译一个维基。先创建 INDEX.md，然后为每个主要主题创建一个 .md 文件。链接相关主题。总结每个源。

## 第六步：开始提问，打造活的知识库

“基于 wiki/ 中的所有内容，我对 【主题】 理解中最大的三个空白是什么？”
“比较源 A 和源 B 对 【概念】 的说法。它们在哪里有分歧？”
“仅使用这个知识库中的内容，给我写一份 500 字的 【主题】 简报。”

## 第七步：定期检查，不让错误复利

审查整个 wiki/ 目录。标记文章之间的任何矛盾。找出提到但从未解释的主题。列出任何没有 raw/ 中源支持的声明。建议 3 篇能填补空白的新文章。


``` shell

python3 scripts/wiki_maintenance.py downloads --write
python3 scripts/wiki_maintenance.py gaps --write
python3 scripts/wiki_maintenance.py health --write
python3 scripts/wiki_maintenance.py structure --write
python3 scripts/wiki_maintenance.py analyses --write
python3 scripts/wiki_maintenance.py graph --write
python3 scripts/wiki_maintenance.py all --write
python3 scripts/wiki_maintenance.py concepts --write

```
