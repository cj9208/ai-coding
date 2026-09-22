# 家庭照片管理（photo_desk）设计

> 一句话核心：照片已经在 NAS 上，系统只做三件事——**读懂**（EXIF / 清晰度 / 连拍归组）、**筛选**（连拍废片自动移入隔离区，账本可一键放回）、**回看**（无账号的时间线 + tag 检索，查看/编辑双模式）。原件字节永不改写，所有系统产出走可迁移的外部目录。

日期：2026-09-22。状态：**M0 已落地（2026-09-22）、M1 已落地（2026-09-23，含真实演练）**，见 `src/photo_desk/`；M2 未开工。读法：§1–§2 是需求与已定裁决（背景）；§3–§4 是实现契约（开工后以本节为准）；§5 是里程碑与验收标准；§6–§7 是风险与依赖。

## 1. 背景与目标

原始诉求两条：

1. **连拍模糊筛查**：拍照时习惯连拍多张保证有清晰的，但事后人工挑废片太难；
2. **存储与查看**：家里各设备的照片汇聚到群晖 NAS，希望自动识别、分类、打 tag，并方便回看。

调研结论（2026-09-22，详见对话记录）：现成自托管方案（Immich / PhotoPrism / 群晖自带 Photos）覆盖诉求 2 约八成，但诉求 1 零覆盖——PhotoPrism 的 quality 只是不可逆的降权展示，Immich 只有精确重复检测，均无"连拍组内排名 + 可逆隔离 + 一键放回"。而诉求 2 借用现成方案会与自建的隔离区形成两套系统、tag 各存一份必然漂移。故选择**全部自建**，且系统规模在裁决后已大幅收缩（同步出局、无账号）。

目标：

- 一个跑在 PC 上的 FastAPI + Jinja 应用，读挂载的群晖目录，提供时间线 / 连拍组 / tag 三种回看视角；
- 一条确定性筛查管线：扫描 → EXIF → 清晰度分 → 连拍归组 → 组内排名 → 自动隔离，产出全可逆；
- tag 体系区分机读与人写，重跑模型不覆盖人工成果。

非目标（刻意排除）：

- **不做设备同步**：照片进 NAS 由现有方式负责，系统只读已落盘文件；
- **不做账号体系**：进入即匿名查看，操作时切编辑模式（同 `file_manager` / `ocr_review` 的无 auth 姿态）；
- **v1 不做人脸识别 / CLIP 语义搜索**：M3 显式 gated on M2 使用后的真实缺口；
- **不改写原件字节**：系统对照片根目录只有两类动作——读、移动（带账本）。

## 2. 已定裁决（owner 拍板，勿在实现中重新推导）

| # | 裁决 | 影响 |
|---|---|---|
| D-1 | 同步不做，数据源就是 NAS 上已有目录 | 砍掉上传/断点/设备兼容全部工作量 |
| D-2 | 群晖为 ARM / j 系列，**不能跑 Docker** | 系统跑在 PC 上，NAS 目录经 SMB 挂载访问；产出目录后期可能迁 NAS → 见 §4.1 |
| D-3 | 废片自动移入 `blurred/`，支持一键放回（含整组放回） | 隔离必须走账本，见 §4.2 |
| D-4 | 无账号；默认查看模式，操作切编辑模式 | UI 姿态同 file_manager |
| D-5 | 原件字节不可变是硬约束 | 移动可、写内容不可；HEIC→JPEG 只做缩略图派生，不回写 |

## 3. 总体架构

```
                PC（uv 环境，本仓唯一运行位置）
 ┌───────────────────────────────────────────────────┐
 │ photo serve ── FastAPI + Jinja（查看/编辑双模式）  │
 │   ▲ 时间线 · 连拍组 · tag 检索(FTS5) · 缩略图      │
 │ photo triage / restore ── 筛查与隔离的唯一写通道   │
 │ photo scan ── 增量清点                             │
 └──────┬───────────────────────────────┬────────────┘
        │ 只读（stat/解码）              │ 只写派生物与账本
 ┌──────▼──────────┐          ┌─────────▼─────────────────┐
 │ <PHOTO_ROOT>/    │          │ <DATA_DIR>/photo_desk/     │
 │  NAS 挂载目录    │          │  ├─ photo_desk.db          │ ← 照片表/tag/隔离账本
 │  （原件，不写）  │          │  └─ thumbs/                │ ← 派生缩略图缓存
 │  └─ blurred/     │◄─移动────│                            │
 └─────────────────┘          └────────────────────────────┘
```

模块划分（tests 按 `tests/test_photo_desk/test_<module>.py` 镜像）：

| 模块 | 职责 |
|---|---|
| `config.py` | 根地址解析（§4.1），唯一的绝对路径拼接点 |
| `scan.py` | 增量清点：mtime+size 先筛、sha256 后算（quantdesk diff-sync 姿势） |
| `exif.py` | 拍摄时间（含毫秒）/ GPS / 设备 / burst 标识提取 |
| `quality.py` | 清晰度分：**纯函数**，输入图像数组、输出标量分，不碰文件系统 |
| `groups.py` | 连拍归组（§4.3 三级兜底） |
| `triage.py` | 组内排名 → 隔离建议 → 执行移动 + 写账本（§4.2） |
| `library.py` | SQLite：photo / tag / move_ledger 表 + `storage.FtsTable` 索引 |
| `web/` | routers（pages/api）+ templates + static；`identity.py` 无（D-4） |
| `cli.py` | `photos scan / triage / restore / serve` 入口 |

## 4. 核心契约

### 4.1 地址配置：DB 只存相对路径，迁移 = 改一个环境变量

D-2 的后期迁移动机决定这不是"路径写不写死"的问题，而是**存储格式问题**：

- `config.py` 是两个根地址的唯一来源，env 覆盖、默认值锚 repo root（`utils.paths` 姿势）：
  - `PHOTO_ROOT` —— 照片源根（默认指向本地一个占位目录，真实值是本机映射的 NAS 盘符，Windows 路径含反斜杠与盘符，**只在 config 里出现一次**）；
  - `PHOTO_DESK_DATA_DIR` —— 产出目录（默认 `data/photo_desk/`，后期迁 NAS 改此值）。
- **数据库与账本一律存相对 `PHOTO_ROOT` 的 POSIX 风格路径**，任何查询/渲染需要绝对路径时经 `config.photo_path(rel)` 现拼。产出迁走、盘符变化、换机器，都只是改 env 后全表重定位，无数据改写。
- `blurred/` 在 `PHOTO_ROOT` 之下（与原件同盘才能秒级 rename 移动；跨盘挂载时 config 校验并警告）。

### 4.2 隔离账本：移动即记账，放回先验身

`move_ledger` 表一行为一次移动：

```
id | photo_id | rel_from | rel_to | sha256 | reason | group_id | moved_at | restored_at | restore_error
```

- `reason` 目前只有 `blur_rank`（组内排名垫底）与 `manual`（编辑模式人手），留枚举余地；
- **放回 = 照账本走**：目标位置空闲 + 现文件 sha256 与账面一致 → 移回；sha256 不符（隔离后被人动过）→ 拒绝并标 `restore_error`，原件留在 `blurred/` 不猜；
- **整组放回**是 `restore --group <id>` 的一等操作（连拍"要么整体回来、要么只回最好那张"两种都是合理动作）；
- 孤儿账本（放回时原路径的父目录已被设备同步侧删掉）：重建目录并放回，账本记 `recreated_parent`，不静默；
- 账本永不删除行——放回是 `restored_at` 置位，操作历史本身就是审计面。

photo 表的 `state` 派生自账本最新一行：`in_place / quarantined / orphaned`。查看模式下 `blurred/` 里的照片在时间线**降权可见**（灰色 + 一键放回按钮），不是消失——废片判断允许出错，出错允许纠正。

### 4.3 连拍归组：三级兜底

设备千差万别，归组信号按可靠性依次取：

1. iOS：EXIF maker-note / XMP 的 `BurstIdentifier`（苹果连拍组权威标识）；
2. EXIF `DateTimeOriginal` + `SubsecTimeOriginal` 毫秒级连续（间隔 < 1s）且文件名 stem 前缀一致；
3. 文件名前缀 + 序号（`IMG_1234~1240` 一类）。

组以 `group_id`（首张 sha256 前 12 位）入库；**归组规则改动必须过 golden 样例**（`tests/golden/photo_groups.jsonl`，仿 orchestrator golden 姿势），防止"重新解释历史"的静默漂移。

### 4.4 清晰度分：组内相对排名，不做绝对阈值

- 算法：灰度缩图（长边压到 1024）上的拉普拉斯方差，经典方法（PyImageSearch 路线）。用 numpy 手写 3×3 卷积核，**不为此引 opencv 重依赖**；
- 判定只发生在**组内**：N 张连拍按分数排序，垫底的 `N-1` 或低于组最优一定比例（默认：`score < 0.5 × best`）才进隔离建议——绝对阈值在跨设备下漂移严重，组内排名几乎不会错（同 quantdesk"横截面排名优于绝对信号"的结构）；
- 非连拍单张**不自动隔离**（D-3 只授权了对连拍动的手），只在详情页显示分数，编辑模式可手动隔离。

### 4.5 tag：机读与人写两张皮

- `tag` 表带 `source` 列：`manual` / 机读来源名（将来 `clip` 等）；**重跑任何模型只允许覆盖同来源的行**，`manual` 永不动——`ocr_review` sidecar 哲学的表内版；
- M2 人写 tag 进 FTS5（`storage.FtsTable` + `fold_cjk`，中文 tag 检索直接继承本仓已解决的 CJK 坑）；
- 事件名（"生日聚会 2025"）与物体/场景名同级存放，不做 v1 层级分类树——MECE 的树是维护负担，扁平 tag + 时间线已覆盖家庭场景检索。

## 5. 里程碑与验收标准

每个里程碑可独立碎掉，验收均为"真机跑通 + 证据入库"，不是"代码写完"。

| 里程碑 | 内容 | 验收 |
|---|---|---|
| **M0 读懂** ✅ | config / scan / exif / library / 时间线页 | 对一个含数百张真实照片的测试目录：增量扫描二次运行只 stat 不改的行；时间线按 EXIF 拍摄日（非文件 mtime）分组正确；HEIC/JPG 混排可读。**验收记录（2026-09-22）**：54 张演示树全绿、幂等复扫 unchanged-only；HEIC 写固件本机不可用，JPG/PNG 覆盖管线，HEIC 真读欠账在 TODO.md |
| **M1 筛选**（诉求①）✅ | quality / groups / triage + restore CLI | 构造 golden 连拍组（含人为模糊样张）：组识别全对、垫底样张被隔离且账本可放回；`restore --group` 与 sha256 不符拒绝路径均有测试；真实目录演练一次，隔离清单先出、人工确认后执行（首次执行保留 `--dry-run` 默认开）。**验收记录（2026-09-23）**：`tests/golden/photo_groups.jsonl` 7 例锁死归组规则；`test_triage.py` 覆盖 dry-run 不落盘、账本落行、单张/整组放回、篡改拒放、占位拒覆盖、孤儿重建父目录、无分数不错杀；演示树真实演练 scan→triage(3 张垫底全中)→--apply→restore --group+--photo 全回原位、sha256 验身通过。**落地偏离两处**：① 时间线只灰显 quarantined，放回按钮归 M2 编辑模式（CLI `photos restore` 已给全）；② golden 连拍用"同秒不同 SubSecTime"表达 <1s 间隔——EXIF DateTimeOriginal 秒级粒度，同秒多张必须靠毫秒字段 |
| **M2 回看**（诉求②） | tag 体系 / FTS 检索 / 查看·编辑双模式 UI / 缩略图缓存 | 浏览器实测：中文 tag 搜索命中、编辑模式打 tag、重启后持久；缩略图缓存重生成幂等 |
| **M3 机读 tag** | CLIP/人脸，独立立项 | gated：仅当 M2 用起来后确认"找不到照片"是真实痛点才启动 |

## 6. 已知风险（正面记录，不掩盖）

1. **SMB 元数据延迟**：数万文件全量 walk 在 SMB 上会很慢。对策：增量设计（mtime+size 命中即跳过）是唯一出路，首扫慢一次可接受；扫描进度要打点日志（仿 `quant download`）。
2. **NAS 可达性**：系统跑 PC、根在 NAS，盘符未挂载时 serve/triage 必须**硬报错**而非静默扫空目录把全库标 missing——"网络连得上但没数据"按缺口处理，同 quantdesk silence-is-a-gap 姿势。
3. **HEIC**：iPhone 默认格式。解码走 `pillow-heif`，EXIF 读取同样依赖它注册格式；HEIC 内嵌 burst 标识的字段位置需真机样例核实（M1 前完成，样例来自自家照片）。
4. **设备侧漂移**：手机可能删掉已同步照片的原件、或 iCloud 优化存储导致 NAS 上只剩缩略图。v1 姿态：文件没了就标 `missing`，不做任何跨设备一致性修复——那是同步系统的职责，D-1 已排除。

## 7. 依赖与接入清单

- 新依赖（`uv add`，extras 化以控制核心安装体积）：`Pillow`、`pillow-heif`（HEIC）、`piexif` 或纯 Pillow `getexif`（先试后者，不够再加）；numpy 已随 pandas 在。明确**不引入 opencv**（§4.4）。
- 新顶层包 gotcha：`src/photo_desk/` 建好后须加入 `pyproject.toml` 的 `[tool.hatch.build.targets.wheel] packages` 并 `uv pip install -e .`（AGENTS.md 记录的坑）。
- `[project.scripts]` 增 `photos = "photo_desk.cli:main"`。
- 数据目录 `data/photo_desk/` 依 repo-root 锚定约定，测试不碰真实 `PHOTO_ROOT`（tmp_path + fake 目录树）。

## 8. 待定项

1. `PHOTO_ROOT` 默认值形态：盘符（`Z:/photo`）还是 UNC 路径（`\\\\nas\\photo`）——买回群晖挂载后实测定；
2. ~~缩略图尺寸策略~~ **已定（M0 实施时）**：两档 WebP（列表 512 / 详情 1600），缓存键 `content_hash[:12]`，派生前先 `exif_transpose`；未白嫖 HEIC 内嵌预览——两档都要重采样，统一走 Pillow 更简单；
3. 编辑模式的进入方式（无账号前提下是纯 UI 开关；是否需要环境变量层面的只读保护，家人共用 PC 时再评估）；
4. `blurred/` 自动清理策略（隔离满 N 天未放回是否提示归档/删除——v1 只提示不执行）。
