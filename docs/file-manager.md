# 文件管理器（file_manager）

面向 100 人以内小组的文件管理应用：按项目归档文件（邮件、文档等），追踪上传者和上传团队，支持可插拔的分层搜索。无身份系统，手动输入名字即可（假设人是诚实的，后续可平滑接入真实认证）。

## 运行

```bash
uv run file-manager          # 默认 http://127.0.0.1:8000
```

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `FM_DATA_DIR` | `./data` | 数据目录（SQLite 库 + 文件存储） |
| `FM_DATABASE_URL` | `sqlite:///{data_dir}/file_manager.db` | 数据库连接串 |
| `FM_HOST` / `FM_PORT` | `127.0.0.1` / `8000` | 监听地址 |
| `FM_MAX_UPLOAD_MB` | `50` | 单文件上传上限 |

首次访问页面会提示输入名字，之后写入 `fm_user` cookie；API 也可用 `X-User` 请求头。

## 功能

- 项目（Project）/ 团队（Team）/ 成员（Member）CRUD；成员可换团队，历史文件保留当时的团队快照
- 文件上传：`.eml`、`.pdf`、`.docx`、`.xlsx`、`.txt` / `.md` / `.csv` / `.json` / `.log`；自动提取标题与正文文本入库（提取失败不阻塞上传）
- 同项目内按内容哈希（sha256）幂等去重：重复上传返回已有记录
- 浏览与检索同一个页面：进去即全部文件，填关键词或选项目 / 团队 / 上传者 / 扩展名就地缩窄
- 排序：创建时间、文件名、大小、标题各支持升/降序（`sort` 参数，统一作用于列表与搜索结果）
- 分页与删除回跳都会保留当前筛选和排序条件；排序一律附加 id 兜底，同值行不会翻页跳行
- 下载（中文文件名走 RFC 5987）与删除（同时清理磁盘文件和 FTS 索引；同名磁盘文件被多条记录引用时不删除）
- 关键词命中时表格多出一列"匹配来源"，标出命中的是文件名 / 标题 / 备注 / 标签（可多个）；无匹配给出明确空状态文案
- 关键词匹配分三级递进，永不返回空手：FTS 词/前缀命中 → ASCII 中缀由 LIKE 兜底（该列标"·模糊"）→ 多词全不命中时放宽为"任一词"并在页面给出提示。大小写天生不敏感（FTS 折叠 + SQLite LIKE 对 ASCII 不敏感）
- 搜索模式可插拔：`metadata` 已可用；`fulltext`（全文检索）、`semantic`（语义检索）已注册占位，API 返回 501，前端显示"规划中"

## 架构

```
src/file_manager/
├── config.py       # Settings（环境变量加载）
├── database.py     # Engine（WAL、外键开关）、session、get_db 依赖
├── models.py       # Team / Member / Project / FileMeta（含 uploader/team 快照字段）
├── identity.py     # resolve_current_user() —— 手动名字身份，唯一替换点
├── fts.py          # FTS5 索引 + fold_cjk()（CJK 折叠 uni+bigram）
├── extractors/     # 按扩展名的提取器注册表（eml/pdf/text/office）
├── search/         # SearchBackend 策略 + 注册表；metadata / 占位 backend
├── services/       # 上传存储、列表过滤 + 排序（order_by_for）、删除、rebuild_fts
├── schemas.py      # pydantic v2 出入参模型
├── routers/api.py  # REST API
├── routers/pages.py# Jinja2 服务端渲染页面
├── templates/ static/
└── cli.py          # uvicorn 启动入口
```

上传管线：校验（项目存在、非空、大小）→ 写磁盘 → 计算哈希 → 同项目查重 → 提取器 enrichment → 数据库 + FTS 同事务提交。

身份：cookie `fm_user` 或 `X-User` 头（两处取值都 percent-decode，头无法承载原始中文），未知名字自动注册为 Member。接入真实身份系统时只需替换 `identity.py` 中的 `resolve_current_user()`。

界面：暖白文档风，纯手写 CSS（无框架、无构建步骤），配色/圆角/阴影/字体全部集中在 `static/style.css` 的 `:root` 变量里，改一处即可整体换肤。吸顶窄顶栏 + 面包屑；上传表单收在默认折叠的 `<details>` 面板里，不占首屏；当前身份是右上角头像菜单，输入姓名用页内模态框而不是 `window.prompt`；项目与团队列表用卡片墙而非表格。注意：模板里的若干文本与类名（`匹配来源`、`match-tag fuzzy`、`已放宽为任一命中`、`共 N 个`、`下一页`）是测试的断言锚点，改样式时可以挪位置但不要改名。

## API 摘要

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/modes` | 搜索模式列表（含 available 标记） |
| GET/POST | `/api/teams` · `/api/members` · `/api/projects` | 列表与创建（重名 409） |
| PATCH | `/api/members/{id}` | 改名 / 调团队（只更新传入字段） |
| GET | `/api/projects/{id}` | 项目详情（含文件过滤分页） |
| POST | `/api/files` | multipart 上传（`file`、`project_id`、可选 `tags`/`notes`） |
| GET | `/api/files` | 文件列表（过滤 + `sort`，同页面） |
| GET | `/api/files/{id}/download` | 下载 |
| DELETE | `/api/files/{id}` | 删除（磁盘 + FTS 一并清理） |
| GET | `/api/search` | `q` + `mode` + 项目/团队/上传人/扩展名过滤 + `sort` + 分页；`q` 留空即全部文件 |

页面路由：`/` 与 `/search` 是同一个文件页（浏览 + 检索，模板 `browse.html`）、`/projects`、`/projects/{id}`、`/teams`、`/teams/{id}`。项目页与团队页各自的文件表同样支持 `sort` 与条件保持。

## 中文搜索与 FTS5 的坑

本机 SQLite 3.47.1 的 FTS5 `unicode61` 分词器会丢弃所有 CJK 字符，直接建索引会导致中文搜索静默失效。`fts.py` 的 `fold_cjk()` 在写入（fts_insert/update）和查询（build_match）两端把 CJK 连续段折叠为 unigram + bigram（空格分隔）来绕过，两端必须保持对称。代价：相邻性靠 bigram 表达，不连续搭配（如"季财"）自然 miss——这正是期望行为。若修改索引字段或升级 SQLite，需调用 `services/files.py` 的 `rebuild_fts()` 重建索引。

第二个坑：折叠出来的一个关键词是多个单元（"财报"→ `财 财报 报`），拼进 MATCH 表达式时必须整组加括号（`token_expr()`）。FTS5 的隐式 AND 与显式 OR 混在一起时，`A B OR C D` 会按 `A (B OR C) D` 之类的优先级求值，只含单个"报"字的文件会被别人家的词拽进结果。

表单的坑（真机点出来的）：HTML 表单提交的是整个 form，没动的下拉框会以空串出现在 query string / form data 里（`?q=coach&uploader_id=`）。页面路由的 `project_id`/`team_id`/`uploader_id`/`member_id`/`page` 因此一律按字符串接收、用 `_opt_int()`/`_page()` 兜成 `None` 或 `1`；若直接声明成 `int | None`，FastAPI 会在进入函数前抛 422，浏览器就跳到一页 JSON。REST API 保持严格类型（422 对程序化调用方是有用的信号）。

## 测试

```bash
uv run pytest tests/test_file_manager.py -q
```

覆盖：上传 + 项目视图、无身份 401、`X-User` 头的 percent-decode、幂等重复上传、团队快照在成员调岗后仍然正确、中文搜索命中与不相邻 miss、ASCII 中缀兜底（标"·模糊"）与大小写不敏感、严格 0 命中才放宽且放宽后仍要求整词（`财报 运维` 不会带进"周报"）、跨列归因（标题 + 标签 分别标出）、命中来源标签（文件名/标题/备注/标签）、浏览与检索共用一页（无关键词不出现"匹配来源"列）、表单整表提交时空下拉框不触发 422、页面与 API 的排序取值及未知值回退、翻页链接保留筛选条件、团队页下拉添加成员、模式注册（semantic → 501）、eml 标题提取、删除清理（记录/磁盘/FTS）、页面冒烟。
