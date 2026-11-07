# forum-rag v0.3.0

[English](#forum-rag-v030-english) · 简体中文

**论坛数据自己更新：用 BRAIN 帐号直连 Support API，不再依赖手动导出。**

以前，本地论坛索引只能靠浏览器插件导出 JSON，再手动 `refresh`。从这个版本起，Python 可以用你自己的 BRAIN 帐号直接从 Support API 拉取社区帖子、评论和 Help Center 文章，写入同一个 SQLite，支持全量构建和增量同步。是否交出帐号、交到什么程度，由你自己选：导入导出、每次手动输入、保存凭据，三种方式都支持。自动同步默认关闭。

- 相关 PR：[#4](https://github.com/BeastOrange/forum-rag/pull/4)
- 版本：`0.2.0` → `0.3.0`（新增功能，不含破坏性变更）
- 规模：相对 `v0.2.0`，36 个文件，+6,431 / −366 行
- MCP 工具数量不变，仍是 22 个

---

## 升级须知

这个版本没有破坏性变更，旧的调用方式、启动命令和数据库都能照常使用。以下几点会影响你的本地环境：

1. **新增运行时依赖 `keyring`**（`>=25.5,<26`，`uv.lock` 固定为 `25.7.0`），用于把凭据存进系统凭据库。升级时需要重新同步依赖。
2. **自动同步默认关闭。** 不运行 `forum-rag login` 并选择开启，或不设置 `WQ_FORUM_RAG_AUTO_SYNC=1`，MCP 启动时就不会联网，其他行为和以前一样。
3. **第一次同步会清理旧备份。** 同步前的备份默认只保留 1 份，保留规则匹配 `<数据库目录>/backups` 下所有 `<库名>-before-*.sqlite3`，包括以前手动留下的 `forum-before-*.sqlite3`。要保留的旧备份，请在第一次同步前移出该目录，或调大 `--keep-backups`。
4. **导出文件的自动导入现在能找到插件导出了。** 旧版的匹配规则是 `WQPCommunityState_*.json`，和插件实际生成的 `WQP_CommunityState_*.json` 对不上，所以检索前的自动导入从来没有生效过。修复后，导出目录里如果有比上次导入更新的插件导出，检索前会被自动导入。不需要的话，设置 `WQ_FORUM_RAG_AUTO_REFRESH=0`。
5. **旧库没有 Help Center 文章。** 0.3.0 之前的导入会忽略导出里的 Help Center 文章。想补全的话，运行一次 `forum-rag sync --full`，或用当前版本重新 `refresh` 一份导出。
6. **README 拆成了中英两份。** `README.md` 现在是英文版，中文版移到 `README_zh.md`，给 AI 的 `README_AGENT.md` 保持不变。

---

## 新增能力

### 从 Support API 同步论坛

```bash
uv run forum-rag sync --full    # 首次全量构建：所有社区帖子、评论和 Help Center 文章
uv run forum-rag sync           # 增量同步：从上次水位线往前 2 小时开始
uv run forum-rag sync-status    # 查看水位线、上次结果、凭据来源和备份
uv run forum-rag sync-check     # 只读检查登录和 API，不输出凭据
```

- **登录方式：** 先用 BRAIN 帐号登录，再通过 `/authentication/support` 换取 Support 会话。重定向由程序逐跳处理，在受 Cloudflare 保护的 `/hc/` 网页之前停下，之后只调用 `/api/v2` 的 JSON 接口，所以不需要浏览器。
- **增量同步：** 按 `updated_at` 倒序翻页，越过水位线就停。只给新帖和有变化的帖子重新拉取评论。
- **全量构建：** 覆盖所有社区和 Help Center 分类。中途中断后重新运行就能续上，评论数没变的帖子不会重复请求评论。只有完整跑完才写入水位线，不带 `--max-pages` / `--community` 的完整全量构建才会清理线上已删除的帖子。
- **请求节奏：** 参照插件的做法，两次请求至少间隔 300 ms，遇到 429 会按 `Retry-After` 退避重试，并发很小。全量构建约每秒 3 次请求。
- **不留中间文件：** 每页数据直接写库，只重建有变化的帖子的 FTS 行，同步期间检索不中断。Help Center 文章以 `Help Center / <分类>` 为社区名，作为论坛主题存储。
- **备份：** 写入已有数据库前，先用 SQLite 备份接口生成 `<库名>-before-sync-<UTC时间>.sqlite3`。默认保留 1 份，可以用 `--keep-backups N` 或 `WQ_FORUM_RAG_KEEP_BACKUPS` 修改。上一次同步没跑完时会复用那份备份，结果 JSON 中会标出 `backup_reused: true`，不会每次中断都重写一份完整的数据库。

### 三种方式，由你决定交不交帐号

| 方式 | 做法 | 本项目是否接触密码 |
| --- | --- | --- |
| 导入导出 | 插件导出 JSON，运行 `forum-rag refresh <导出>` | 不接触 |
| 手动同步 | 在自己的终端运行 `forum-rag sync`，按提示输入 | 只在本次运行的内存里使用，不保存 |
| 保存凭据 + 自动同步 | 运行一次 `forum-rag login`，再选择是否开启自动同步 | 保存在系统凭据库 |

```bash
uv run forum-rag login     # 先在线验证，成功才保存，然后单独询问是否开启自动同步（默认否）
uv run forum-rag logout    # 删除凭据并关闭自动同步
```

- **凭据库：** 通过 `keyring` 写入 macOS 钥匙串、Windows 凭据管理器或 Linux Secret Service。
- **明文回退：** 无图形界面的 Linux、WSL、Docker 通常没有凭据库。这时 `login` 先明确警告，再写入 `~/.config/wq-forum-rag/credentials`，文件权限 0600，目录权限 0700。
- **查找顺序：** 先看环境变量 `WQ_BRAIN_USERNAME` / `WQ_BRAIN_PASSWORD`，再看系统凭据库，最后看明文文件。文档提醒了环境变量可能被其他程序或 AI 读到，不建议写进 MCP 配置的 `env` 块。
- **只在终端里输入：** `login` 只能在交互式终端中运行。没有凭据时，`sync` 在终端里当场提示输入；在 MCP 服务或 AI 代为执行的 shell 里，它不会卡住等待，而是立即以 `no_credentials` 失败。
- **不落盘：** 密码不写日志，也不进数据库；cookie 和 JWT 只保存在进程内存里。

### 启动时的自动同步（需要你开启）

开启后，`forum-rag-mcp` 启动时会在后台线程跑一次增量同步，不阻塞服务。需要同时满足：自动同步已开启、数据库已存在且有水位线或导入记录、能找到凭据、距离上次成功同步超过阈值（默认 3 天，可以用 `WQ_FORUM_RAG_SYNC_INTERVAL_DAYS` 或 `--sync-interval-days` 修改）。启动时永远不做全量构建，也永远不提示输入。

开关保存在 `~/.config/wq-forum-rag/settings.json`。设置了 `WQ_FORUM_RAG_AUTO_SYNC` 时，以环境变量为准。

### 数据过期提示：`sync_notice`

数据超过阈值时，`search_forum`、`search_forum_batch` 和 `find_by_exact` 的结果里会多一个 `sync_notice` 字段。它写明上次更新时间、过期天数和原因（`auto_sync_off`、`no_credentials`、`login_failed`、`sync_running`、`sync_failed`、`sync_due`），并建议手动同步或开启自动同步。数据没过期时不出现。这样即使用户从没读过文档，也会在 AI 使用时被告知数据已经旧了。

### `doctor` 显示同步与凭据状态

`forum-rag doctor` 新增两个字段：
- `sync`：自动同步是否开启、开关来源、水位线、是否过期、上次结果；
- `credentials`：能否找到凭据，来源是 `env`、`keyring` 还是 `file`，以及系统凭据库是否可用。

只显示来源，不显示用户名或密码。

---

## 修复

| 问题 | 处理 |
| --- | --- |
| 导出文件名匹配规则写错，检索前的自动导入从未找到插件导出 | 同时匹配 `WQP_CommunityState_*.json` 和旧写法 |
| 修复上面的问题后，较旧的导出可能删掉或覆盖已同步的帖子 | 自动导入跳过修改时间早于最近一次同步的导出；手动 `refresh` 不受影响 |
| 同步被中断后，每次重启都重写一份约 600 MB 的备份 | 上一次同步没跑完时复用那份备份 |
| `--keep-backups` 传负数会删光所有备份 | 负数按默认值 1 处理 |
| `forum-rag-mcp --help` 会触发启动同步 | `--help` 在同步和启动服务之前就退出 |
| 启动同步的日志在 stderr 重复打印 | 只打印一次 |
| 启动同步出错可能影响 MCP 服务 | 同步异常被隔离，服务照常启动 |
| MCP 工具的失败原因到客户端只剩“Error executing tool” | 调用方能处理的错误（参数错误、路径不存在等）带上原因返回；磁盘满、权限不足等基础设施错误照常抛出，不被降级隐藏 |
| MCP `initialize` 返回的服务版本为空 | 返回包版本号 |

---

## 验证结果

| 检查 | 结果 |
| --- | --- |
| 完整 pytest | **453 passed**（约 111 s），网络和凭据库均为 mock |
| `ruff format --check` | 70 个文件，通过 |
| `ruff check` | 62 条，比开发前的 60 条多 2 条 typer 参数默认值写法提示（B008），没有其他新增 |
| `uv lock --check` | 通过 |
| 真实 API 只读验证 | 登录、换取 Support 会话、读取帖子和评论、分类和文章接口都通过 |
| 有界真实同步 | 在临时数据库上跑有上限的全量构建，再在数据库副本上跑增量同步，都成功，结果可以用现有搜索检索 |
| 独立审查 | 两轮功能审查结论均为 APPROVED |

真实接口验证全程只读，没有点赞、发帖或评论，也没有在用户的正式数据库上执行同步。

---

## 已知限制

1. **增量同步可能漏掉部分新评论。** 增量只给 `updated_at` 变化的帖子拉评论。如果帖子有了新评论但 `updated_at` 没变，这条评论要等下一次 `sync --full` 才会入库。抽样中帖子的 `updated_at` 都不早于它的最新评论，但这不能证明总是如此。
2. **增量同步不删除帖子。** 线上删除的帖子只在完整的 `sync --full` 中清理；被知识页引用的帖子始终保留。
3. **全量构建耗时较长。** 请求速率约每秒 3 次，按这个速率估算，1 万个帖子大约要一个小时，这个时长没有实测过。中断后可以续跑。
4. **明文回退的安全性有限。** 以当前用户身份运行的任何程序，包括 AI Agent，都能读取 0600 权限的凭据文件。系统凭据库也挡不住拥有你全部终端权限、并且主动去读的程序。介意的话请用导入导出或手动同步。
5. **登录依赖平台现有流程。** 如果平台对帐号登录要求额外验证，或者 Support 的会话交换方式变了，同步会失败。导入导出这条路不受影响。
6. **没有常驻定时任务。** 自动同步只在 MCP 启动时检查一次，长时间不重启就不会更新。
7. **主要在 macOS ARM 上实测。** Windows 和 Linux 的凭据库路径依赖 `keyring` 自身的支持，没有逐一实机验证。

---

## 升级步骤

```bash
git fetch --tags && git checkout v0.3.0
uv sync --locked                    # Conda：python -m pip install -e .
uv run forum-rag doctor
```

1. 重启或重新连接 MCP 客户端，确认仍是 22 个工具。
2. 想保留的旧备份，先移出 `<数据库目录>/backups`。
3. 想用同步的话，在自己的终端运行 `uv run forum-rag sync-check` 检查连通性，然后按需运行 `login`、`sync` 或 `sync --full`。
4. 不想联网的话什么都不用做，自动同步默认关闭。

## 回退

回退到 `v0.2.0` 不需要迁移数据库：0.3.0 写入的同步元数据只在 `schema_meta` 中新增了键，旧版本会忽略它们。如果回退前跑过同步，同步写入的帖子会保留在库里。要回到同步前的状态，可以用 `backups` 下的 `<库名>-before-sync-*.sqlite3` 替换数据库，替换前请先停止 MCP 服务。保存的凭据可以用 `forum-rag logout` 删除，回退之后也可以直接在系统凭据库里删除名为 `wq-forum-rag` 的条目。

## 完整改动

[`v0.2.0...v0.3.0`](https://github.com/BeastOrange/forum-rag/compare/v0.2.0...v0.3.0)

<details>
<summary>提交索引</summary>

| 提交 | 内容 |
| --- | --- |
| `49ccb4c` | MCP 服务信息返回包版本号 |
| `d096464` | MCP 工具失败原因传给客户端 |
| `ffd134f` | 只转换调用方能处理的错误 |
| `9ebc0da` | Support API 客户端，修复导出文件名匹配 |
| `5a8210e` | 增量与全量同步及 CLI |
| `7d8bf9f` | MCP 启动时后台增量同步 |
| `da6cbc0` | 同步文档，版本升到 0.3.0 |
| `279bd65` | 启动同步日志只打印一次 |
| `b83819b` | 防止旧导出覆盖已同步数据，隔离启动同步异常 |
| `0765328` | 复用未完成同步的备份，`--help` 不触发同步 |
| `56aadd0` | 负数 `keep-backups` 按默认值处理 |
| `537da92` | 系统凭据库存储，明文文件回退 |
| `fc73398` | 启动同步改为需要用户开启，并读取保存的凭据 |
| `1a368db` | `login` / `logout` 命令 |
| `488683e` | `sync` 在终端中提示输入一次性凭据 |
| `f0146d3` | 检索结果中的数据过期提示 |
| `db76288` | `doctor` 显示凭据来源和同步状态 |
| `b5cf8f6` | 凭据存储和自动同步开关的文档 |
| `830d1ba` | README 拆分为英文版和中文版 |
| `15b5644` | 标题图去掉版本号 |

</details>

---

# forum-rag v0.3.0 (English)

**Forum data that updates itself: sync straight from the Support API with your BRAIN account, no manual export needed.**

Until now, the local forum index could only be fed by a JSON export from the browser extension followed by a manual `refresh`. From this release, Python can pull community posts, comments and Help Center articles straight from the Support API with your own BRAIN account and write them into the same SQLite file, as a full build or an incremental sync. You decide whether to share your account, and how much: importing exports, typing credentials per run, and saving credentials are all supported. Auto sync is off by default.

- Pull request: [#4](https://github.com/BeastOrange/forum-rag/pull/4)
- Version: `0.2.0` → `0.3.0` (new features, no breaking changes)
- Size: 36 files, +6,431 / −366 lines since `v0.2.0`
- MCP tool count is unchanged at 22

---

## Upgrade notes

This release has no breaking changes; existing calls, launch commands and databases keep working. These points affect your local setup:

1. **New runtime dependency `keyring`** (`>=25.5,<26`, pinned to `25.7.0` in `uv.lock`), used to store credentials in the OS credential store. Sync dependencies when upgrading.
2. **Auto sync is off by default.** Unless you run `forum-rag login` and turn it on, or set `WQ_FORUM_RAG_AUTO_SYNC=1`, MCP startup never goes online and nothing else changes.
3. **The first sync cleans old backups.** One pre-sync backup is kept by default, and retention matches every `<db>-before-*.sqlite3` under `<db dir>/backups`, including older hand-made `forum-before-*.sqlite3` files. Move any backups you want to keep out of that directory before the first sync, or raise `--keep-backups`.
4. **Export auto-import now finds extension exports.** The old pattern `WQPCommunityState_*.json` never matched the extension's real `WQP_CommunityState_*.json`, so auto-import before searches never worked. After the fix, an extension export in the export directory that is newer than the last import is imported before searches. Set `WQ_FORUM_RAG_AUTO_REFRESH=0` if you do not want that.
5. **Older databases lack Help Center articles.** Imports before 0.3.0 ignored Help Center articles in exports. To fill them in, run `forum-rag sync --full` once, or `refresh` an export with the current version.
6. **The README is now split by language.** `README.md` is English, the Chinese version moved to `README_zh.md`, and `README_AGENT.md` for AI agents is unchanged.

---

## New features

### Sync the forum from the Support API

```bash
uv run forum-rag sync --full    # first full build: all community posts, comments and Help Center articles
uv run forum-rag sync           # incremental sync, starting 2 hours before the last watermark
uv run forum-rag sync-status    # watermark, last result, credential source and backups
uv run forum-rag sync-check     # read-only login and API check; prints no credentials
```

- **Login:** log in with the BRAIN account, then exchange that session for a Support session through `/authentication/support`. Redirects are followed hop by hop and stop before the Cloudflare-protected `/hc/` HTML pages. After that, only `/api/v2` JSON endpoints are called, so no browser is needed.
- **Incremental sync:** pages through posts by `updated_at`, newest first, and stops past the watermark. Comments are refetched only for new or changed posts.
- **Full build:** covers every community and Help Center category. If it stops, run it again to resume; posts whose comment count is unchanged do not refetch comments. The watermark is written only when a build finishes, and only a complete `sync --full` without `--max-pages` / `--community` removes posts deleted online.
- **Request pacing:** follows the extension: at least 300 ms between requests, backoff that honors `Retry-After` on 429, and small concurrency. A full build makes about 3 requests per second.
- **No intermediate files:** each page is written straight to the database, and only changed posts get their FTS rows rebuilt, so search keeps working during a sync. Help Center articles are stored as forum topics in communities named `Help Center / <category>`.
- **Backups:** before writing to an existing database, the SQLite backup API creates `<db>-before-sync-<UTC time>.sqlite3`. One is kept by default; change that with `--keep-backups N` or `WQ_FORUM_RAG_KEEP_BACKUPS`. If the last sync did not finish, its backup is reused and the result JSON shows `backup_reused: true`, so an interrupted sync does not write a full database copy every time.

### Three modes: you decide whether to share your account

| Mode | How | Does this project see your password? |
| --- | --- | --- |
| Import an export | Export JSON with the extension, then run `forum-rag refresh <export>` | No |
| Manual sync | Run `forum-rag sync` in your own terminal and type credentials when asked | Only in memory for that run; nothing is saved |
| Saved credentials + auto sync | Run `forum-rag login` once, then choose whether to enable auto sync | Stored in the OS credential store |

```bash
uv run forum-rag login     # verifies online, saves only on success, then asks about auto sync (default: no)
uv run forum-rag logout    # delete saved credentials and disable auto sync
```

- **Credential store:** `keyring` writes to the macOS Keychain, Windows Credential Manager or Linux Secret Service.
- **Plaintext fallback:** headless Linux, WSL and Docker usually have no credential store. There `login` warns clearly first, then writes `~/.config/wq-forum-rag/credentials` with file mode 0600 in a 0700 directory.
- **Lookup order:** env vars `WQ_BRAIN_USERNAME` / `WQ_BRAIN_PASSWORD`, then the OS credential store, then the plaintext file. The docs warn that env vars can be read by other programs and AI agents, and advise against putting them in an MCP config `env` block.
- **Typed only in a terminal:** `login` runs only in an interactive terminal. Without credentials, `sync` prompts in a terminal. Inside the MCP server or a shell run by an AI, it does not wait for input; it fails at once with `no_credentials`.
- **Never persisted:** passwords are never logged or written to the database. Cookies and JWTs live only in process memory.

### Auto sync at startup (you turn it on)

Once enabled, `forum-rag-mcp` runs one incremental sync in a background thread at startup without blocking the server. All of these must hold: auto sync is enabled; the database exists and has a watermark or an import record; credentials can be found; and the last successful sync is older than the threshold (3 days by default, set with `WQ_FORUM_RAG_SYNC_INTERVAL_DAYS` or `--sync-interval-days`). Startup never runs a full build and never prompts.

The setting lives in `~/.config/wq-forum-rag/settings.json`. When `WQ_FORUM_RAG_AUTO_SYNC` is set, the env var wins.

### Stale data notice: `sync_notice`

When data is older than the threshold, results from `search_forum`, `search_forum_batch` and `find_by_exact` carry a `sync_notice` field. It gives the last update time, the age in days and the reason (`auto_sync_off`, `no_credentials`, `login_failed`, `sync_running`, `sync_failed`, `sync_due`), and suggests a manual sync or enabling auto sync. Fresh data has no such field. Users who never read the docs still learn that their data is old when an AI uses it.

### Sync and credential status in `doctor`

`forum-rag doctor` adds two fields:
- `sync`: whether auto sync is on, where that setting comes from, the watermark, staleness and the last result;
- `credentials`: whether credentials were found, their source (`env`, `keyring` or `file`), and whether an OS credential store is available.

It shows only the source, never the username or password.

---

## Fixes

| Problem | Fix |
| --- | --- |
| The export file pattern was wrong, so auto-import before searches never found extension exports | Match both `WQP_CommunityState_*.json` and the old spelling |
| With that fixed, an older export could delete or overwrite synced posts | Auto-import skips exports modified before the last sync; manual `refresh` is unaffected |
| After an interrupted sync, every restart rewrote a backup of about 600 MB | Reuse the backup when the last sync did not finish |
| A negative `--keep-backups` deleted every backup | Negative values fall back to the default of 1 |
| `forum-rag-mcp --help` triggered a startup sync | `--help` exits before syncing or serving |
| Startup sync logs were printed twice on stderr | Printed once |
| A startup sync error could affect the MCP server | Sync errors are isolated and the server starts normally |
| MCP tool failures reached clients as a bare "Error executing tool" | Errors a caller can fix (bad arguments, missing paths and so on) return their reason; infrastructure errors such as a full disk or denied permission still propagate instead of being hidden |
| MCP `initialize` reported an empty server version | Reports the package version |

---

## Verification

| Check | Result |
| --- | --- |
| Full pytest | **453 passed** (about 111 s), with network and credential store mocked |
| `ruff format --check` | 70 files, clean |
| `ruff check` | 62 findings: 2 more than the 60 before this work, both typer option default findings (B008), nothing else new |
| `uv lock --check` | Passed |
| Live read-only API checks | Login, Support session exchange, posts, comments, categories and articles all passed |
| Bounded live sync | A capped full build on a temporary database, then an incremental sync on a database copy, both succeeded and were searchable with existing search |
| Independent review | Both feature reviews ended APPROVED |

All live checks were read-only: no votes, posts or comments, and no sync against the user's real database.

---

## Known limitations

1. **Incremental sync can miss some new comments.** Comments are fetched only for posts whose `updated_at` changed. A new comment on a post whose `updated_at` did not change waits for the next `sync --full`. In a sample, no post's `updated_at` was earlier than its newest comment, but that does not prove it always holds.
2. **Incremental sync never deletes posts.** Posts deleted online are removed only by a complete `sync --full`; posts cited by knowledge pages are always kept.
3. **Full builds take a while.** At about 3 requests per second, 10,000 posts would take roughly an hour by estimate; this was not measured. An interrupted build can resume.
4. **The plaintext fallback has limited protection.** Any program running as your user, AI agents included, can read the 0600 credentials file. An OS credential store also cannot stop a program that has your full terminal access and deliberately reads it. If that matters to you, import exports or sync manually.
5. **Login depends on the platform's current flow.** If the platform adds extra verification to account login, or changes how the Support session exchange works, sync fails. Importing exports is unaffected.
6. **No resident scheduler.** Auto sync checks only at MCP startup, so without a restart the data does not update.
7. **Mainly tested on macOS ARM.** Windows and Linux credential storage relies on `keyring`'s own support and was not tested on each platform.

---

## Upgrading

```bash
git fetch --tags && git checkout v0.3.0
uv sync --locked                    # Conda: python -m pip install -e .
uv run forum-rag doctor
```

1. Restart or reconnect your MCP client and confirm it still lists 22 tools.
2. Move any old backups you want to keep out of `<db dir>/backups`.
3. To use sync, run `uv run forum-rag sync-check` in your own terminal, then `login`, `sync` or `sync --full` as needed.
4. If you do not want anything to go online, do nothing: auto sync is off by default.

## Rolling back

Rolling back to `v0.2.0` needs no database migration: 0.3.0 only adds keys to `schema_meta`, and the old version ignores them. Posts written by a sync stay in the database. To return to the pre-sync state, stop the MCP server, then replace the database with a `<db>-before-sync-*.sqlite3` file from `backups`. Remove saved credentials with `forum-rag logout`, or after rolling back, delete the `wq-forum-rag` entry from the OS credential store.

## Full changelog

[`v0.2.0...v0.3.0`](https://github.com/BeastOrange/forum-rag/compare/v0.2.0...v0.3.0). The commit index is in the Chinese section above.
