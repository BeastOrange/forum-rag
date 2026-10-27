# 检索与知识可信度验证记录

这批改动修复无命中的全量词法回退、ID 查找扫描、中文子串召回，以及知识审核和来源版本校验。它保留 SQLite、离线运行和已有 CLI/MCP 必填参数。

## 用同一份数据比较

验证使用 2026-10-27 本地索引的 SQLite backup 副本：9,023 篇帖子，45,927 个一级 chunk（含父级共 54,950 个），74 篇官方文档，228 个文档 chunk，0 个知识页。检索默认使用 256 维哈希后端。

`tests/fixtures/search_eval.json` 包含 60 条由来源标题、明确子串和 ID 构造的检查：44 条已知目标、16 条空查询或虚构标识符。它是回归检查集，不是完整人工相关性标注；已知目标命中率不能解读为通用搜索准确率。

以真实 stdio MCP 调用旧代码 60 次，新代码每条 3 次。第一次调用并非操作系统冷缓存；小样本 p95 只描述本轮分布。源码指纹、语料规模和逐条结果保存在 [旧版记录](evaluation/search-before.json) 和 [新版记录](evaluation/search-after.json) 中，便于复查。文件保留第一轮整合版本的指纹；之后的修复涉及知识资格过滤和证据检查，原评测库没有知识页。

| 检查 | 旧代码 | 第一轮整合代码 |
| --- | ---: | ---: |
| 前 5 项找回已知目标 | 41/44 | 44/44 |
| 无答案检查正确返回空 | 0/16 | 16/16 |
| 首次调用样本 p50 | 277 ms | 34 ms |
| 首次调用样本 p95 | 12,036 ms | 265 ms |
| 新版重复调用 p95 | 未测 | 257 ms |
| 运行错误 | 0 | 0 |

这些数据支持修复有效，不能证明所有查询的性能、完整召回率或因果分解。中文版兼容检索仍可能在 SQLite 内扫描文本；没有引入外部向量库或默认模型下载。

## 搜索行为

- 空查询和无词法依据的哈希结果不再返回帖子。正常 FTS 零结果不再取回整个语料库到 Python 打分。
- 缺少 FTS 时使用有数量上限的字面候选查询；FTS 查询错误向上传递，不伪装成没有资料。
- 有效 ID 走等值查询，保留社区过滤；ID 不存在仍可按普通文本查找。
- `%`、`_`、反斜杠按字面查找。中文子串兼容已有 unicode61 索引，读取不重建索引或回填缓存。

## 知识提案需要审核

`propose_knowledge_page` 现在始终创建草稿。旧 `auto_publish` 参数保留，但高 confidence 不再绕过证据审核；这是有意改变的发布行为。

新增可选 `source_evidence`，每项包含 `topic_id`、`claim`、`excerpt`。提案保存来源 hash；摘录位置可以自动核对，结论是否被支持仍由审核者判断。旧 schema 或未提供证据的页不会进入默认可信搜索。

审核并发布：

```text
review_knowledge_page(slug, action="reviewed", note="核对了哪些证据和适用条件")
publish_knowledge_page(slug)
```

`needs_review` 暂停可信复用；`archived` 保留原因和事件，可用 `replacement_slug` 指向替代页。冲突双方和来源变化页在默认搜索中排除。`depends_on` 传播不可用状态，`related_to` 不传播；不自动改写衍生知识。只有 `supports`、`refines` 入链可增加排序分数。

显式知识写入只更新对应的 FTS 记录。读取不迁移旧库。不存在按时间自动判定新知识正确、按访问量删除知识或训练模型权重的功能。

## 运行验证

使用已有 Python 环境并指向本工作区源码，无需重新安装依赖：

```bash
env -u FORCE_COLOR -u CLICOLOR_FORCE TERM=dumb NO_COLOR=1 \
  PYTHONPATH=src python -m pytest -q

PYTHONPATH=src python scripts/evaluate_search.py \
  --db /path/to/forum-copy.sqlite3 --transport mcp \
  --repeats 3 --output /tmp/search-report.json
```

旧代码完整基线为 46 项通过。第一轮整合发现两项旧测试仍假定无审核自动发布，改成明确审核或草稿检查后，完整测试为 68 项通过。后续独立审查又发现评论 JSON 转义和混合查询候选配额问题；修正后的最终结果记录在本文末尾。

继承环境里的强制终端颜色曾造成 JSON 文本断言失败；上面的测试命令移除该环境影响，没有修改产品输出来掩盖失败。

## 在 Lody 使用本工作区版本

原 MCP 配置指向原项目目录，不会自动使用本 worktree 的修复。新入口可以复用已有虚拟环境：

```bash
/path/to/existing/.venv/bin/python /path/to/this-checkout/scripts/run_mcp.py \
  --db /path/to/existing/forum.sqlite3
```

此入口从当前 checkout 加载代码，默认关闭隐式导入；显式设置 `WQ_FORUM_RAG_AUTO_REFRESH` 可恢复原有自动刷新行为。Lody 保存配置后仍要求用户在可信界面选择它，保存不等于当前会话已加载。

本轮已保存 `forum-rag-worktree` 配置指向该入口。原 `forum-rag` 配置仍指向原项目源码；要使用此工作区的修复，应在 Lody 中审阅并选择 `forum-rag-worktree`。工作区移除后需更新该路径。

实际启动检查发现 20 个工具（含 `review_knowledge_page`），无命中返回 0、ID 返回 1、文档搜索返回结果。启动前后原始数据库的大小、mtime 和 SHA-256 一致，未改写原库。

## 尚未验证的范围

- 人工标注的同义、跨语言、复杂研究问题准确率，以及并发压力和真正冷启动。
- 独立模型语义召回、模型质量对比、默认模型配置；哈希后端没有模型级语义能力。
- 查询日志驱动补采。先保证未命中判定，再设计反馈采集；搜索片段不作为完整正文，零访问不作为废弃依据。
- 真实知识库的质量分布：现有索引中没有知识页，可信度检查通过独立临时测试库验证。

## 最终验收

- 完整测试：**71 passed in 74.50s**；源码、脚本和测试的 `compileall` 通过，`git diff --check` 通过。
- 原搜索回归、知识资格与评测统计均先观察失败再修复；混合查询、评论转义、1200 页依赖链也有失败复现和通过记录。
- 独立 Reviewer 发现两项问题，修复后限定复审均为 **ADDRESSED**，本轮范围内没有 blocker；另检查了坏 JSON、跨评论拼接、元数据及依赖环。
- 临时库上真实 MCP 调用跑通“提案 → 审核 → 发布 → 搜索 → 归档 → 空结果”，未对原库执行写操作。
- 改动保留在当前隔离工作区，未提交、推送或合并。
