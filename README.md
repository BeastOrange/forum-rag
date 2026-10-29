# forum-rag

<p align="center">
  <img src="./assets/readme/hero.svg" width="100%" alt="forum-rag：将研究文档与个人论坛导出本地索引到 SQLite，并通过 CLI 和 MCP 提供可追溯检索与知识沉淀。">
</p>

<p align="center">
  <strong>给 AI 使用的轻量本地检索与证据工具。</strong><br>
  用 SQLite 保存官方文档、个人论坛导出和知识页，让 AI 先查本地证据，减少反复调用在线论坛 MCP。默认不加载本地模型。
</p>

<p align="center">
  <code>Python 3.11+</code> · <code>SQLite / FTS5</code> · <code>MCP SDK 2.x</code> · 默认无模型
</p>

> **给 AI Agent 的入口：** 若希望 Claude Code、Codex、Gemini CLI 等直接操作本项目，请优先提供 [`README_AGENT.md`](README_AGENT.md)。

## 为什么使用它？

- **离线、轻量、可增量更新。** 不依赖外部检索服务；索引、全文检索、精确查找和 embedding cache 都落在本地 SQLite。
- **两类来源各自独立。** 仓库自带的官方研究文档与用户自己的论坛 JSON 共享存储文件，但在表、FTS 类型与 MCP 查询入口上彼此隔离，避免混淆来源。
- **让检索结果可以沉淀。** Agent 可把有论坛原帖支撑的稳定结论保存成知识页，建立来源绑定、关系图与可导出的 Markdown Wiki，而不是每次从 chunk 临时拼接答案。

## 给 AI 的轻量双语检索

默认使用本地 SQLite 检索。调用方 AI 提供原问题和中英文关键词，即可一次取回去重后的帖子及证据，不需要准备本地模型：

```text
search_forum_batch(
  query="如何使用 ts_backfill 处理缺失值？",
  query_variants=["缺失值 ts_backfill", "missing values ts_backfill"],
  top_k=6,
  evidence_chars=600,
  max_evidence_chars=6000,
  compact=True
)
```

工具保留实际命中查询、语言粗标记、片段 ID、内容版本与原文坐标。只在缺少上下文时调用 `get_forum_evidence` 读取该命中的邻块，需要完整讨论再用 `get_post`。语言标记和排序分数不代表事实可信度；本地搜索也不保证两种语言都有相关内容。翻译与问题理解由调用方 AI 完成，工具只执行有界的本地检索。

CLI 使用相同流程：

```bash
uv run forum-rag search-batch "如何使用 ts_backfill 处理缺失值？" \
  --variant "缺失值 ts_backfill" --variant "missing values ts_backfill" \
  --compact --db .cache/forum.sqlite3
uv run forum-rag forum-evidence "<返回的 chunk_id>" --db .cache/forum.sqlite3 \
  --expected-content-hash "<返回的 chunk_content_hash>" --max-chars 6000
```

最多接受三个附加查询；证据字符预算用于控制原文片段长度，不等同于整个 JSON 或 token 数量。调用方应检查超时、锁、索引降级和截断状态，避免将有界未命中解释成论坛没有答案。已有 MCP 客户端需要重启后才能看到新增工具。

## 从零开始：得到第一个结果

### 1. 安装并建立官方文档索引

```bash
git clone https://github.com/BeastOrange/forum-rag.git
cd forum-rag
```

#### 使用 uv

```bash
uv sync

# 项目要求 Python >= 3.11；没有合适版本时先执行：
# uv python install 3.11

# 必跑：将仓库内 74 篇官方 Markdown 文档写入默认 SQLite 索引
uv run forum-rag ingest-docs Documents
```

#### 使用 Conda

项目不依赖 `uv` 运行。若你使用 Conda，请在仓库根目录创建 Python 3.11+ 环境，并在该环境中用 `pip` 安装项目：

```bash
conda create -n forum-rag python=3.11 pip -y
conda activate forum-rag
python -m pip install -e .

# 必跑：将仓库内 74 篇官方 Markdown 文档写入默认 SQLite 索引
forum-rag ingest-docs Documents
```

后续示例以 `uv` 展示。Conda 环境中，将 `uv run forum-rag <command>` 替换为 `forum-rag <command>`，将 `uv run forum-rag-mcp` 替换为 `forum-rag-mcp`。

### 2. 搜索一条官方文档

```bash
uv run forum-rag search-docs "neutralization" --top-k 3
```

完整标题命中优先，因此这个查询优先返回 `Neutralization`；其他文档依词法排序。也可以查看完整内容：

```bash
uv run forum-rag show-doc neutralization
```

### 3. （可选）接入你自己的论坛导出

```bash
uv run forum-rag refresh /path/to/forum-export_YYYYMMDD_HHMMSS.json
```

完成后，同一 SQLite 文件中同时有官方文档和论坛帖；可使用 `search` / `show` 搜索论坛，也可供 MCP 工具使用。

```bash
uv run forum-rag search "alpha decay neutralization" --top-k 5
uv run forum-rag show 12913566170391
```

默认检索不加载本地模型，也无需安装模型依赖。中英文查询由调用方 AI 提供，可通过 `search_forum_batch` 一次提交。

调用 `search_forum_batch(..., compact=True)` 或 CLI `search-batch --compact` 可省去与 `evidence[].excerpt` 重复的 `snippet`，保留证据文本、版本标识和坐标。默认响应保持兼容。MCP 批量搜索与证据读取共享两个工作名额，满载返回 `busy`；客户端取消后会通知后台查询，线程退出后才释放名额。取消为协作式，数据库锁等待等阻塞操作可能延迟退出。

以下可选依赖仅保留给已有代码层 embedding 后端调用；安装后不会启用独立语义召回，也不会新增 MCP 工具：

```bash
uv sync --extra local-embeddings
```

Conda 环境中运行：

```bash
python -m pip install -e ".[local-embeddings]"
```

## 数据、分发与边界

| 内容 | 是否随仓库提供 | 用途与获取方式 |
| --- | --- | --- |
| 代码、测试、`pyproject.toml` | ✅ | `git clone` 即可获得。 |
| [`Documents/`](Documents/) 中 74 篇官方 Markdown 文档 | ✅ | `git clone` 后执行 `ingest-docs Documents`。 |
| 论坛 SQLite（例如 `.cache/forum.sqlite3`） | ❌ | 由每位用户使用自己的平台帐号导出 JSON 后，在本地运行 `refresh` / `index` 建立。 |

仅使用自带官方文档时，源码即可开始；检索个人论坛内容时，再提供自己的论坛 JSON 或既有 SQLite。`Documents/` 随源码分发，首次使用需执行 `ingest-docs Documents`。

如果你选择私下共享已经构建好的 SQLite，对方可以跳过论坛离线索引，只需将 `FORUM_RAG_DB` 指向该文件。请先自行评估相关平台条款及数据分享风险；该数据库默认不应提交到 Git。

## 检索工作流

```text
官方 Documents/ ─┐
                  ├─> SQLite / FTS5 候选 ─> 词法排序 / 多查询 RRF ─> CLI / MCP
个人论坛 JSON ───┘                                                │
                                                                   └─> 可追溯知识页与 Markdown Wiki
```

- **论坛索引：** 解析 community、topic、author、时间、投票/评论等轻量元数据，并从 HTML 清洗正文文本。
- **增量更新：** `index` 会根据源 JSON 的路径、mtime 与 size 跳过未变更文件；替换导出或需要强制刷新时使用 `--rebuild`。`refresh` 则把论坛索引和来源 manifest 提交组合为一步。
- **精确/近邻查询：** `find_by_exact` 面向 topic id、URL、标题、正文、评论与 chunk 的精确词命中；`related_posts` 基于标题寻找同社区相关主题。

首次显式重建论坛索引：

```bash
uv run forum-rag index \
  --json /path/to/forum-export_YYYYMMDD_HHMMSS.json \
  --db .cache/forum.sqlite3 \
  --rebuild
```

后续对同一来源的增量索引：

```bash
uv run forum-rag index \
  --json /path/to/forum-export_YYYYMMDD_HHMMSS.json \
  --db .cache/forum.sqlite3
```

## CLI 速查

| 目的 | 命令 |
| --- | --- |
| 查看所有命令 | `uv run forum-rag --help` |
| 批量双语证据 | `uv run forum-rag search-batch "query" --variant "另一语言关键词" --compact` |
| 扩展命中证据 | `uv run forum-rag forum-evidence CHUNK_ID --max-chars 6000` |
| 检查环境与索引 | `uv run forum-rag doctor --db .cache/forum.sqlite3` |
| 搜索 / 查看论坛 | `uv run forum-rag search "query"` · `uv run forum-rag show TOPIC_ID` |
| 搜索 / 查看官方文档 | `uv run forum-rag search-docs "query"` · `uv run forum-rag show-doc SLUG` |
| 文档入库 | `uv run forum-rag ingest-docs Documents` |
| 重建 FTS5 索引 | `uv run forum-rag search-reindex` |
| 查看论坛来源差异 | `uv run forum-rag source-status ./notes` · `uv run forum-rag source-ingest-plan ./notes --commit` |
| 使用知识层 | `uv run forum-rag <command>`，其中 `<command>` 为 `evolve-context`、`knowledge-search`、`knowledge-show`、`knowledge-lint`、`knowledge-graph` 或 `knowledge-export` |

所有命令均可通过 `--db .cache/forum.sqlite3` 指定索引文件；默认路径也是 `.cache/forum.sqlite3`。

### 可选中文候选索引

中文 bigram 候选索引默认关闭，无需模型或新增依赖。需要 SQLite 3.43 或更新版本；可先在数据库副本上显式构建：

```bash
uv run forum-rag search-reindex --db /path/to/forum.sqlite3 --chinese
```

构建后，在 MCP 服务进程的环境中设置 `FORUM_RAG_CHINESE_INDEX=1` 并重新连接客户端，才会启用中文候选路径。取消该环境变量即可恢复原路径；原 FTS 索引仍保留。缺少完整中文索引时沿用原路径，普通查询不会建索引或写库。

该路径要求查询中的每段连续中文在原字段中完整出现，多段按 AND 匹配；英文与代码部分只做字面子串检查，不提供翻译、词边界或语义理解。现有排序公式保持不变，候选仍有数量上限。索引会增加磁盘占用；启用前应核对自己的查询效果，当前不承诺整体准确率提升。

### 官方文档源

按完整标题搜索文档时（例如 `Operators`），完整标题命中优先返回，结果中的 `exact_title_match` 会标明这一点。`score` 保留原检索分数，完整标题以外的结果沿用原排序；分数不是置信概率。

`ingest-docs` 是增量操作：相同内容按 `content_hash` 跳过，默认会 prune 数据库中目录已不存在的文档。

```bash
uv run forum-rag ingest-docs Documents
# 可选：
#   --rebuild       强制全清重建
#   --no-prune      保留 DB 中已不存在于目录的文档
```

文档与论坛在设计上完全隔离：

- 文档表：`documents` / `doc_chunks`，FTS kind 为 `doc_chunk`
- 论坛表：`topics` / `chunks`，FTS kind 为 `forum_chunk`
- MCP 中 `search_docs` 只命中文档，`search_forum` 只命中论坛
- 两者共享 SQLite 文件与 embedding cache，但不互相污染

## MCP：让 Agent 使用本地证据

服务端默认从 `FORUM_RAG_DB` 获取索引路径，也可在每次工具调用时传入 `db`。

```bash
export FORUM_RAG_DB=/absolute/path/.cache/forum.sqlite3
uv run forum-rag-mcp
```

Claude Desktop 或其他兼容 MCP 客户端的配置示例：

```json
{
  "mcpServers": {
    "forum-rag": {
      "command": "uv",
      "args": [
        "--directory",
        "/absolute/path/forum-rag",
        "run",
        "forum-rag-mcp"
      ],
      "env": {
        "FORUM_RAG_DB": "/absolute/path/.cache/forum.sqlite3"
      }
    }
  }
}
```

若使用 Conda，图形化 MCP 客户端通常不会先执行 `conda activate`。请将 `command` 直接指向该环境中的 `forum-rag-mcp` 可执行文件：

```json
{
  "mcpServers": {
    "forum-rag": {
      "command": "/absolute/path/to/miniconda3/envs/forum-rag/bin/forum-rag-mcp",
      "args": [],
      "env": {
        "FORUM_RAG_DB": "/absolute/path/.cache/forum.sqlite3"
      }
    }
  }
}
```

Windows 下将可执行文件路径改为 `...\envs\forum-rag\Scripts\forum-rag-mcp.exe`。

<details>
<summary><strong>查看全部 22 个 MCP 工具</strong></summary>

| 分组 | 工具 |
| --- | --- |
| 论坛检索 | `search_forum(query, db=None, top_k=5)`<br>`get_post(topic_id, db=None)`<br>`find_by_exact(value, db=None, community=None, top_k=5)`<br>`related_posts(topic_id, db=None, top_k=5)` |
| 批量证据 | `search_forum_batch(query, query_variants=None, db=None, top_k=6, evidence_chars=600, max_evidence_chars=6000, compact=False)`<br>`get_forum_evidence(chunk_id, db=None, before=1, after=1, max_chars=6000, expected_content_hash=None)` |
| 官方文档 | `search_docs(query, db=None, top_k=5)`<br>`get_doc(slug, db=None)`<br>`ingest_docs(directory, db=None, rebuild=False, prune=True)` |
| 来源管理 | `source_status(source, db=None)`<br>`source_ingest_plan(source, db=None, commit=False)` |
| 知识层 | `build_evolution_context(query, db=None, top_k=5)`<br>`propose_knowledge_page(slug, title, summary, body, source_topic_ids, confidence, db=None, links=None, auto_publish=True, source_evidence=None)`<br>`search_knowledge(query, db=None, top_k=5, include_drafts=False)`<br>`get_knowledge_page(slug, db=None)`<br>`link_knowledge_pages(source_slug, target_slug, relation_type, db=None, weight=1.0, confidence=0.8)`<br>`lint_knowledge(db=None, slug=None)`<br>`publish_knowledge_page(slug, db=None)`<br>`graph_query(slug, db=None, depth=1, relation_type=None)`<br>`export_knowledge_wiki(db=None, output_dir=".cache/wiki", include_drafts=False)` |
| 审核与归档 | `review_knowledge_page(slug, action, note, db=None, replacement_slug=None)` |
| 维护 | `rebuild_search_index(db=None)` |

</details>

## 自进化知识层：从证据到可复用结论

知识层不内置外部 LLM API：**MCP 客户端负责阅读、总结和判断；本项目负责存储、证据定位、来源版本校验、审核记录、图谱连边和 lint。** 知识沉淀不会训练或修改模型权重。

推荐让 Agent 按以下流程工作：

1. `build_evolution_context("你的问题")`：优先返回已发布知识页，并附带完整论坛证据。
2. Agent 阅读上下文，归纳稳定结论。
3. `propose_knowledge_page(..., source_evidence=[...])`：写入草稿。每项证据包含 `topic_id`、`claim` 和原文中的 `excerpt`；系统保存来源的内容版本。旧调用仍可建草稿，`auto_publish` 参数保留兼容，但不会绕过审核。
4. 核对结论、适用范围和摘录后，调用 `review_knowledge_page(slug, "reviewed", note="审核说明")`，再调用 `publish_knowledge_page(slug)`。来源存在、摘录存在及高 confidence 都不等于结论正确；支持程度由审核者负责。
5. 后续用 `search_knowledge(...)` 查找已沉淀内容，必要时再回退到论坛检索。

默认搜索只返回审核通过、来源版本未变、无冲突且依赖有效的已发布知识。旧库没有审核版本的知识先排除，需补证据并重新审核；读取不会自动迁移数据库。新增 `conflicts_with` 会让双方失去可信资格；`depends_on` 的失效会传递给依赖页，`related_to` 不作为依赖。

显式归档：`review_knowledge_page(slug, "archived", note="废弃原因", replacement_slug="替代页")`；替代页可省略。保留历史和来源，不按发布时间或访问次数自动覆盖、删除或改写其他页面。已有归档状态继续使用。

知识页仍写入同一 SQLite，相关表包括：

- `knowledge_pages`：概念、规则、经验等页面
- `knowledge_sources`：知识页与论坛原帖的来源绑定
- `knowledge_links`：typed edges / backlink
- `knowledge_events`：append-only 演化日志

本地检查与导出示例：

```bash
uv run forum-rag evolve-context "alpha decay neutralization" --top-k 3
uv run forum-rag knowledge-search "neutralization" --json
uv run forum-rag knowledge-show alpha/neutralization-decay
uv run forum-rag knowledge-lint
uv run forum-rag knowledge-graph alpha/neutralization-decay --depth 2
uv run forum-rag knowledge-export --out .cache/wiki
```

## 验证检索与回答

使用数据库副本运行：

```bash
PYTHONPATH=src python scripts/evaluate_search.py \
  --db /path/to/forum-copy.sqlite3 \
  --transport mcp --repeats 3 --output /tmp/search-report.json
```

脚本记录语料规模、源码指纹、已知目标召回、无答案误返回和首次/重复调用耗时。默认 60 条检查针对本项目的本地导出构造；它们不是完整的人工相关性标注，不能据此声称通用准确率。请用 `--cases` 提供符合你自己数据的标注。首次调用也不代表操作系统冷缓存。评测输出请保存在本地，不提交生成的报告和日志。

对于 AI 回答，`scripts/evaluate_task_evidence.py` 可核对保存的任务、实际回答、证据和独立判断：

```bash
uv run python scripts/evaluate_task_evidence.py \
  --runs /path/to/saved-runs.json --judgments /path/to/judgments.json \
  --output /tmp/task-report.json
```

输入格式见脚本开头。脚本不会调用 AI 或检索工具；没有保存回答的记录保持未知，拒答与有证据的实质回答分别统计。机器判断不是人工准确率，`needs_online` 也不是实际线上调用节省量。它用于定位失败和复核证据，不替代人工验收。

## 检查本地环境

运行 `uv run forum-rag doctor --db .cache/forum.sqlite3` 可查看 Python、SQLite、MCP SDK、论坛与文档来源、FTS 索引状态。此命令只读，不创建数据库、不下载模型，也不重建索引。输出为 JSON，附有需要手动执行的修复建议；`ready` 表示本地依赖及至少一种已配置来源的检索结构可用；未导入论坛不影响仅用官方文档。各来源的状态会分别列出，不代表已经验证 MCP 客户端连接或检索准确度。

## 维护已有安装

本次升级使用官方 `mcp>=2.3.0,<3`，不需要独立的 `fastmcp` 包。先同步依赖，再重启 MCP 客户端；已有论坛索引无需重建，原 stdio 启动命令和已有参数保持兼容。

```bash
cd /path/to/forum-rag
git pull
uv sync --locked
uv run forum-rag doctor --db .cache/forum.sqlite3
```

Conda 环境中执行 `python -m pip install -e .`，再运行 `forum-rag doctor --db .cache/forum.sqlite3`。只有需要新增或更新仓库文档时，才另外执行 `ingest-docs Documents`；已有论坛数据不需要重新 `refresh`，也不要删除数据库。如果索引还含其他目录导入的文档，摄入时可加 `--no-prune`，保留目录外文档。

然后重启或重新连接 MCP 客户端。服务进程不会热重载代码；重启后应发现 22 个工具。已摄入官方文档时，可调用 `search_docs("neutralization")`、`get_doc("operators")`；有论坛数据时再验证 `search_forum_batch`。工具不可见时，检查客户端是否仍指向旧环境或错误的仓库目录。

如果运行过独立语义检索实验，`search_semantic_forum` 和模型准备、建索引命令已不在当前接口中。改用 `search_forum_batch` 传入中英文查询；既有论坛 SQLite 不依赖实验模型文件。

## 自动化任务的进程保护

长时间运行的测试、评测等自动化任务可通过 `scripts/run_bounded_task.py` 设置租约和时限。调用方先创建租约文件，在仍管理任务时持续更新其修改时间；工作进程不得自行续租。

```bash
# 由调用方创建并续租；此示例本身不会自动续租
touch /tmp/forum-rag-task.lease
uv run python scripts/run_bounded_task.py \
  --lease-file /tmp/forum-rag-task.lease --lease-ttl 30 --timeout 600 \
  -- python -m pytest -q
```

默认 30 秒无续租，或达到总时限，就停止该任务的整个进程组。停止续租或删除租约可以取消任务；正常结束也会清理遗留子进程。监管进程被强杀时，独立监控进程会通过控制管道断开回收任务组。

租约文件必须在启动前存在且未过期；长任务需要调用方续租。该工具使用 POSIX 进程组，适用于 macOS/Linux，不覆盖主动脱离进程组的程序。

## 当前范围与后续方向

已实现：本地 JSON 解析、SQLite 原帖与 chunk 索引、Markdown 文档摄入、FTS5 词法检索、双语批量查询与可定位证据、来源 manifest、知识页与来源绑定、typed graph、Wiki 导出、backlink boost 与 embedding cache。

已知限制：旧库中文 LIKE 回退仍可能全表扫描，冷请求或复杂查询可能超时；可选中文索引尚未证明整体质量收益，默认关闭。并发保护目前覆盖 MCP 批量检索和证据读取，不代表全部同步工具都有统一限流。

暂未实现：真人标注的完整准确率评测、PDF/截图/音频/视频等多模态摄入、定时或 always-on ingestion、由 LLM 自动识别矛盾，以及外部向量数据库或 seekdb 类服务化基础设施。查询日志驱动补采仍是后续方向；不能把搜索片段拼成完整原文，也不能按零访问量自动淘汰知识。

对外 CLI / MCP contract 与内部 `parser`、`storage`、`search` 模块解耦；后续替换 embedding backend 或调优 chunk/search 策略，不应改变既有 CLI/MCP 接口。
