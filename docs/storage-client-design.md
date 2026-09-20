# Storage 共享层设计

> 一句话核心：`src/storage` 是通用存储层——按数据库类型分模块（一类一个，目前用到的类型只有 SQLite），只收"怎么正确访问这类库"的知识（engine/PRAGMA/FTS5+CJK 折叠/补列式 schema 补丁/去重哈希）；各项目自己的表、ORM 模型、业务 store（如 `SessionStore`）留在原项目，不搬进来。

日期：2026-09-20（v3）。状态：**三批迁移全部落地并已 push——`1b6438c`（共享层）、`a84f3e0`（file_manager + ai_market_radar 接入）、`ea307ad`（research_agent 接入，其 storage 子包同时改名 persistence）**。同日结构收尾：默认数据目录统一锚定 repo root、radar/file_manager 的去重哈希改用 `storage.sha256_hex`。日常怎么用见 [docs/storage-usage-guide.md](storage-usage-guide.md)，本文只管设计与现状。

## 1. 背景：现状为什么需要这个层

仓库里曾有三套彼此独立的存储实现，重复的都是与业务无关的连接知识：

| 能力 | file_manager（接入前） | research_agent | ai_market_radar（接入前） |
|---|---|---|---|
| engine + PRAGMA（WAL/外键） | `src/file_manager/database.py` | `src/research_agent/storage/db.py`（近乎复制前者） | `src/ai_market_radar/store.py` 裸 sqlite3 又写一遍 |
| FTS5 + CJK 折叠 | `fts.py`（已实现，本机验证过 unicode61 丢 CJK、trigram/editdist3 不可用） | 尚无；05 号文档点名复用 `fold_cjk` | — |
| schema 补丁 | 无 | 无 | `PRAGMA table_info` + 手写 `ALTER TABLE` |
| 内容哈希去重 | sha256 全量 | sha256 前 16 位 | sha256 全量（文本） |
| 已知 bug | PRAGMA 监听挂在 `Engine` **类**上，每次 `make_engine` 重复注册、污染同进程其他 engine | 写法正确（挂实例） | — |

另外两处依赖方向问题：`file_manager/database.py` 为了放 `get_db` 而 import fastapi；折叠规则将来被跨项目需要时只能复制源码。

## 2. 方向：通用层，一类数据库一个模块

这是三轮讨论后的定稿方向：

- 共享层是**通用的**（generic），不是各项目存储代码的收容所。判据一句话：**换掉业务、这个代码片段还成立，它才进 `src/storage`**。engine 工厂、PRAGMA 策略、CJK 折叠、补列工具——成立，进；`FileMeta`、`CaptureRow` 表定义、`SessionStore` 的事务语义——不成立，留在项目里。
- **一类数据库一个模块**。目前用到的类型只有 SQLite（三种用法——SQLAlchemy ORM、裸 sqlite3、FTS5 虚表——都是它的用法，不算不同类型）。所以包内现在就一个 `sqlite.py` 加一个 `fts.py`；将来真需要 Postgres 时是**新增兄弟模块**，不抽公共基类、不做后端协议。
- **不发明配置约定**。`db_url` 由调用方从自己的 Settings 传入（`FM_DATABASE_URL` 等环境变量留在各项目），与 llm_client"各项目自有 Config 转换成共享 Settings"的同构做法。
- 复用靠**直接 import 具体能力**：`from storage import SqliteClient, FtsTable, fold_cjk`。

## 3. 包结构（已实现）

```
src/storage/
  __init__.py     # 定位 + 用法示例 + "什么该进什么不该进"判据
  sqlite.py       # SQLite 类型的 client：
                  #   to_db_url(str|Path)   同时兼容 URL 注入和路径注入两种既有习惯
                  #   make_engine(url)      实例级 PRAGMA 监听（WAL + foreign_keys）——修掉类级 bug 的写法
                  #   session_factory(url)
                  #   SqliteClient(db)      一个库一个实例：session() 上下文 / connect() 裸 DBAPI /
                  #                         init_schema(metadata) / ensure_columns / dispose
                  #   ensure_columns(engine, table, {col: ddl})  收编 ai_market_radar 的手写补列迁移
                  #   sha256_hex(data, length=None)              去重哈希配方（截断策略由项目自选）
  fts.py          #   fold_cjk                              单字+双字滑窗折叠，写查两端共用
                  #   token_expr(token, prefix=False)       每词一个括号组；prefix=True 时 ASCII 单元加 * 通配
                  #   match_expr(tokens, joiner, prefix)    词间 AND/OR 组合
                  #   FtsTable(name, columns)               create/upsert/delete/rowids_for，显式同步、无触发器
```

设计注记：

- `SqliteClient.connect()` 走 `engine.raw_connection()`，让 ai_market_radar 那种"拿 DBAPI 连接直接执行 SQL"的既有风格也能吃到同一套 PRAGMA——这是"通用层服务所有用法、而不是逼所有人换成 ORM"的体现。
- FTS 折叠的写端（`FtsTable.upsert`）和查端（`token_expr/match_expr`）出自同一模块、共用同一 `fold_cjk`，从结构上杜绝两端漂移——这正是它该进共享层而非留在项目里的原因。`prefix` 参数是接入 file_manager 时上收的：ASCII 词前缀通配（`repo*`）、CJK 单元精确匹配、词内单元显式 `AND`，全部来自其 metadata 搜索的实战验证。
- 无迁移框架。`init_schema(create_all)` + `ensure_columns`（只加列）覆盖目前全部需求；出现改列/回填需求时再议 Alembic。

## 4. 接入现状

**file_manager（M1，已完成）**：

- `database.py` 瘦身为"域 Base + get_db"两样，engine/session 工厂删除；`app.py` 用 `SqliteClient(settings.db_url)` 建 `app.state.storage`。
- `fts.py` 从约百行手写 SQL 缩为索引声明：`FILES_INDEX = FtsTable("files_fts", (...))` + `fts_values(meta)` 映射；services 三个调用点（store_upload/delete_file/rebuild_fts）改走 `upsert/delete`。
- `search/metadata.py` 的 `token_expr/build_match/build_match_any` 改为共享层的薄封装（`prefix=True`）。
- 包内不再 import fastapi（除本该 import 的 app 层），类级监听器 bug 消除。

**ai_market_radar（M3，已完成）**：

- `KnowledgeBase` 对外签名与裸 SQL 风格不变；内部 `sqlite3.connect` + 手工 WAL 换成 `SqliteClient(path).connect()`，手写 tag 补丁换 `ensure_columns`，`close()` 归还连接并 `dispose()`。

**research_agent（M2，按决定推迟）**：等 MVP 开发告一段落再接入；届时删其 `storage/db.py` 内引擎三件套、`SessionStore` 不动，并顺手把 `research_agent/storage` 子包改名避让顶层 `storage` 包（已确认"等修改的时候再改"）。

## 5. 测试与登记

- `tests/test_storage/`：14 个用例覆盖 URL/Path 双注入、WAL+foreign_keys、**监听器不跨 engine 泄漏的回归测试**、raw connect、补列幂等、CJK 双字命中不误报散字、prefix 通配、upsert/delete、未知列拒绝。**已验证**：test_storage + test_file_manager + test_ai_market_radar 三套件 56 例全过（2026-09-20，本机 `uv run pytest`）。
- 全量回归（迁移前基线）：132 通过；唯一失败 `test_pdf_summarizer::test_load_config_defaults` 为既有环境问题——本机 `.env` 设了 `LLM_MODEL=deepseek-flash`，与该测试断言的默认值冲突，和存储层无关。
- `pyproject.toml` `packages` 已登记 `src/storage` 与补登记 `src/research_agent`，并重装 editable。

## 6. 遗留事项

1. ~~误建目录清理~~ 已由用户删除；~~测试重跑~~ 已全过（§5）。
2. M2 research_agent 接入时机：等其 MVP 开发告一段落。
3. `file_manager` 的更新文件元数据路由目前不改 FTS 行（迁移前即如此，非本次引入）；若确认要改，用 `FILES_INDEX.upsert` 一行即补上。
4. 可选：三套件之外的全量回归（基线 132 通过，唯一失败为 `.env` 的 `LLM_MODEL=deepseek-flash` 环境问题）。
