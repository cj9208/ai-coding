# Storage 共享层使用指南

> 面向"明天要建库"的你：怎么把 `src/storage` 用起来。设计缘由见 [storage-client-design.md](storage-client-design.md)，这里只有怎么用。

```
你的项目 (Settings: db_url 或 data_dir)
        │  传 URL / 传 Path 都行
        ▼
SqliteClient ──┬── session() ──────► SQLAlchemy ORM（file_manager 风格）
               ├── connect() ──────► 裸 DBAPI SQL（ai_market_radar 风格）
               ├── init_schema(md) ─► create_all 建表
               └── ensure_columns() ─► 加列式小补丁
FtsTable + fold_cjk/match_expr ─────► 中文全文索引（写查两端同一模块，防漂移）
```

## 0. 什么该进共享层，什么不该

一句话判据：**换掉业务后这段代码还成立，才进 `src/storage`**。

| 放哪 | 例子 |
|---|---|
| `src/storage`（通用） | engine + WAL/外键 PRAGMA、FTS5 建表同步、CJK 折叠、补列迁移、sha256 去重 |
| 各项目（业务） | ORM 模型/表定义（`FileMeta`、`CaptureRow`）、store 类（`SessionStore`、`KnowledgeBase`）、搜索编排逻辑、`get_db` 这类框架胶水 |

## 1. 新项目接入：三步

```python
# ① 配置照旧自己管——共享层不发明环境变量
@dataclass(frozen=True)
class Settings:
    db_url: str = os.environ.get("MYAPP_DATABASE_URL", "sqlite:///data/myapp/myapp.db")

# ② 建 client（一个库一个实例；URL 或 Path 都接受，父目录自动创建）
from storage import SqliteClient
storage = SqliteClient(settings.db_url)          # 或 SqliteClient(Path("data/myapp/myapp.db"))

# ③ 建表 + 用
storage.init_schema(Base.metadata)               # create_all，幂等
with storage.session() as db:
    db.add(MyRow(name="x"))
    db.commit()                                  # 事务由调用方管，client 不代劳
```

进程退出前 `storage.dispose()`；常驻服务（FastAPI）挂在 `app.state` 上即可，见 file_manager。

## 2. 两种用法，按项目脾气选

**ORM 风格**（file_manager 现在这样）：只用 `session()`。FastAPI 依赖注入的写法照抄 `file_manager/database.py` 的 `get_db`——它三行，从 `request.app.state.storage` 拿会话。

**裸 SQL 风格**（ai_market_radar 现在这样）：`conn = storage.connect()` 拿 DBAPI 连接，`?` 占位符、`executescript`、`cursor.rowcount` 都是你熟悉的 sqlite3；PRAGMA 已由 engine 施加，不用再手写。用完 `conn.close()`（归还连接池）。适合"确定性解析、不想引 ORM"的场景——不用为了接入共享层换成 ORM。

## 3. 中文全文检索（本机最大的坑在这）

已验证：这台机器的 FTS5 `unicode61` 分词器**直接丢弃中日韩字符**，`trigram`/`editdist3` 扩展不可用。通用层的对策是对称折叠：写入和查询两端都调同一个 `fold_cjk`（CJK 段展开为单字+双字滑窗），所以"财报"按相邻双字命中、不会被散落的"报"字误伤。

你只需要声明索引哪些列，机制都在共享层：

```python
from storage import FtsTable

DOCS_INDEX = FtsTable("docs_fts", ("title", "body"))   # 你的域索引定义，放你自己项目里

# 建表（可并入 create_app / 初始化处）
with storage.session() as db:
    DOCS_INDEX.create(db)
    db.commit()

# 写端：与业务行同一个会话、同一个事务内同步（无触发器，显式调用）
DOCS_INDEX.upsert(db, row.id, {"title": row.title, "body": row.body})
DOCS_INDEX.delete(db, row.id)

# 查端：每个关键词一个括号组（load-bearing，别去括号）；
# prefix=True 让纯 ASCII 词获得前缀通配（repo → repo*），CJK 精确匹配
from storage import match_expr
ids = DOCS_INDEX.rowids_for(db, match_expr(tokens, prefix=True))         # 严格级：词间 AND
loose = DOCS_INDEX.rowids_for(db, match_expr(tokens, joiner=" OR ", prefix=True))  # 放宽级
```

全量重建参照 `file_manager/services/files.py` 的 `rebuild_fts`：清空虚表 + 逐行 `upsert`。改了折叠规则后必须重建一次。

## 4. 零散工具

```python
from storage import ensure_columns, sha256_hex

# 加列式补丁（等价于 PRAGMA table_info + ALTER TABLE，幂等，返回实际新增列）
ensure_columns(client.engine, "items", {"tag": "TEXT NOT NULL DEFAULT 'feature'"})

# 去重哈希：截断策略项目自选（文件字节用全量、抓取文本用前 16 位，均有先例）
sha256_hex(data)                # 64 位十六进制
sha256_hex(text, length=16)
```

## 5. 测试注入约定

共享层对测试友好全靠两点：client 接受任意 URL/Path，且**不读任何环境变量**。照既有习惯注入临时库：

```python
def test_xxx(tmp_path):
    storage = SqliteClient(tmp_path / "test.db")        # 路径风格（radar 习惯）
    storage = SqliteClient(f"sqlite:///{(tmp_path/'t.db').as_posix()}")  # URL 风格（FM 习惯）
```

file_manager 的测试则完全不感知：它给 `create_app` 传 `Settings(db_url=…)`。

## 6. 各项目现状速览

| 项目 | 状态 | 入口 |
|---|---|---|
| `file_manager` | 已接入 | `app.state.storage`；索引声明在 `fts.py`（`FILES_INDEX`） |
| `ai_market_radar` | 已接入 | `KnowledgeBase` 内部（`store.py`） |
| `research_agent` | 已接入 | `SessionStore.open(db_url)`（内部建 `SqliteClient` + create_all）；表定义在其 `persistence/db.py` |

## 7. 注意事项

- **同一个 .db 文件别开两个 client**。各自有池、各自有连接，WAL 下能跑但等于放弃了共享层的单入口约定；跨项目要读别人的库，就 import 对方的 store/client。
- WAL 会在库旁产生 `-wal`/`-shm` 边文件，属正常，`data/` 已在 .gitignore。
- 外键约束现在对所有项目生效（原先 file_manager/research 有、radar 无）；radar 的库没有 FK 定义，无行为变化。
- `FtsTable` 用外部 rowid 与业务表 id 对齐：删业务行前记得先 `delete` 索引行（file_manager 的 `delete_file` 是范例）。
- research_agent 的子包已按建议改名：`research_agent/storage` → `research_agent/persistence`（与顶层 `storage` 避免阅读混淆；Python 绝对导入下本不冲突）。
- 将来若支持 Postgres：新增 `src/storage/postgres.py` 兄弟模块，**不要**给现有模块抽基类——抽象要等第二个真实后端出现才值得。
