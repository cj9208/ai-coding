# 组卷(exam-builder)设计文档

> 状态:**草案,待 review**。实现前请先读本文;文中的裁决(D-1..D-8)是结论,不要重新推导。
> 语料证据来自对 `~/Downloads/八上课后练习.zip`(53 个 `.docx` + 2 个 `.doc`)的实际拆解,见 §3。

## 0. 一张图看懂

```
                 ┌────────────────────────  入库(一次性/增量)  ────────────────────────┐
                 │                                                                    │
  用户的 Word ──▶│  切题(split)          投影(project)              落库(persist)      │
  (每份=一章)    │  document.xml         ① 纯文本题干 ────────────▶ FTS5 索引          │
                 │   │ 锚点+题号          ② 几何 PNG  ──(原字节)──▶ cache 缩略图        │
                 │   ▼                    ③ 公式 WMF  ──(MVP 不渲染)▶ 仅记"有公式"      │
                 │  每题 = 原始 OOXML 片段 ────────────────────────▶ SQLite(verbatim) │
                 │       + 媒体/嵌入清单                                                │
                 └────────────────────────────────────────────────────────────────────┘
                                                  │
                 ┌────────────────────────  使用(每次)  ──────────────────────────────┐
                 │                                                                    │
  浏览器 ──▶ 浏览/搜索/筛选(只读投影) ──▶ 勾选题目 ──▶ 组卷(compose)                  │
                 │                                              │                     │
                 │                          包级手术:拼 XML 片段 + 搬 PNG/OLE/WMF      │
                 │                          + 重排关系 ID + 重编号                     │
                 │                                              ▼                     │
                 └────────────────────────────────────── 新的 .docx(公式/图保真)     │
                                                                                 └────┘
```

核心不变量:**题目在库里永远是"原始 OOXML 片段",不是纯文本**。搜索和展示读的是从片段派生的*投影*;组卷读的是片段本身。投影可以丢、可以重建,片段不能丢——这是公式和图形能原样保真的唯一原因(§3 证明它们无法用文本表示)。

## 1. 要解决的问题

用户本地有大量 Word(每份≈一个章节),内含数学题。想要:

1. 看到现有文档清单;
2. 按关键词 / 章节等条件**搜索、筛选**题目(模糊搜索是重点);
3. 勾选题目,**生成一份新的 Word 试卷**。

参考对象是组卷网,但范围是**本地单机、个人使用**,不做账号/权限/在线服务。

## 2. 裁决(D-1..D-8,勿重新推导)

| # | 裁决 | 理由 |
|---|---|---|
| D-1 | 题目以 **verbatim OOXML 片段** 存储,不压成纯文本 | 公式是 MathType OLE、图是 PNG,文本表示会丢内容(§3) |
| D-2 | 搜索/展示只读**投影**,组卷只读**片段**;两者不互相污染 | 投影可重建,片段是唯一事实源;同 `ocr_backend` 的"契约+投影"姿态 |
| D-3 | **MVP 不渲染公式**(网页显示占位符),公式搜索不做 | 用户裁决(2026-10-17):渲染属后期可轻松加;公式文本在 `.bin`/WMF 里,索引成本过高 |
| D-4 | 组卷用 **docx 包级手术**(拼 XML + 搬部件 + 重排 rId),不用 python-docx 重建 | python-docx 不处理 OLE/`w:object`,重建必丢公式(§3 验证) |
| D-5 | 切题靠**模板锚点**(`同步练习题目`/`同步练习答案`)+ 题号正则;锚点缺失→该文档标记 `error` 进待处理列表,**不猜** | 语料 53/53 命中锚点(§3);猜错的切题比报错更糟 |
| D-6 | 章/节/课时标签**从文件名解析**,免费获得;难度/题型等人工标签放 M2 | 文件名即 `13.2.1 三角形的边(第2课时)`,零成本结构化筛选 |
| D-7 | 存储:`data/exam_builder/` 放 SQLite(含 verbatim 片段,源 Word 可能移动,故片段是资产);`cache/exam_builder/` 放可重建的缩略图 | 遵循仓库 `data/` vs `cache/` 契约 |
| D-8 | UI = FastAPI + Jinja + 原生 JS,无构建链、无 auth(同 `file_manager`/`ocr_review`) | 单机个人工具,保持一致姿态 |

## 3. 语料证据(2026-10-17 实测)

样本:`八上课后练习.zip`,55 文件 = 53 `.docx` + 2 `.doc`(老二进制格式)。

### 3.1 命名即标签

`13.2.1 三角形的边.docx`、`16.3.2 完全平方公式(第2课时).docx`、`第十七章 小结.docx`。
可解析出:章(13/14/…)、节(13.2.1)、课时(第N课时)、标题。→ D-6。

### 3.2 统一模板(切题契约的依据)

每份 `.docx` 结构一致(襄阳七中「同步练习」系列):

```
同步练习 / 课程基本信息表(学科·年级·学期·课题·编写教师·工作单位·指导专家)   ← 前置元数据,丢弃
同步练习题目                     ← 锚点 A(53/53 命中):题目区开始
   1．五条线段的长分别为…        ← 题号 = ^\d+[．.、]
   2．…
   3．（1）…（2）…               ← 小问不新开一题
同步练习答案                     ← 锚点 B(53/53 含"答案"):答案区开始
   1．3．                        ← 与题号一一对应
   2．5＜a＜9．
```

### 3.3 三种内容表示(决定 D-1/D-3/D-4)

| 内容 | OOXML 表示 | 实测分布 | 结论 |
|---|---|---|---|
| 文字 | `w:t` | 全部 | 直接取文本做投影 |
| 几何图 | **PNG 内联**(`wp:inline` + `a:blip`) | 第13–15章普遍,如 15.1.1 有 7 张 | 字节即浏览器可显示,M1 直接serve |
| 数学公式 | **MathType OLE**(`Equation.DSMT`):`w:object` = `embeddings/oleObjectN.bin` + `media/imageN.wmf` 预览 + 2 个 rId | 第16–18章密集,如 18.1.2(2) 有 29 个;OMML(`m:oMath`) 几乎为 0 | 文本不可索引(D-3);搬运需连 `.bin`+`.wmf`+rId 一起(D-4) |
| 表格 | `w:tbl` | 前置信息表 + 部分排版 | 切题须以"顶层 body 节点"为粒度,跨 `w:p`/`w:tbl` |

已验证:Pillow 经 Windows GDI 可把 WMF 预览转成清晰 PNG(`-15a²b³/25a⁵b⁴` 可读)——**M2 渲染公式无需装 LibreOffice**,此路已通,只是 MVP 不做(D-3)。

### 3.4 工具现状

- venv 已有:`python-docx`、`lxml`、`Pillow`、`fastapi`、`jinja2`。
- 有 `pandoc`(anaconda);**无 LibreOffice / ImageMagick**。
- 故 2 个 `.doc`:MVP 检测后标记 `error: legacy .doc`,不转换、不崩溃;转换放 M2(需 LibreOffice 或用户重存)。

## 4. 契约:Question 是什么

```
Question
├── doc_id, qno(原编号), order
├── stem_xml      原始 OOXML:题目区的顶层节点序列(<w:p>/<w:tbl>),verbatim
├── answer_xml    原始 OOXML:对应答案区节点序列,verbatim(可为空)
├── manifest      引用的部件清单:[{role: figure_png|formula_ole, rel_id, part, sha256}]
└── 投影(可重建)
    ├── stem_text   纯文本(公式位置插占位符 〔公式〕)→ 进 FTS5
    └── n_figure / n_formula  计数,供 UI 徽标
```

- **切题粒度 = 顶层 body 节点**。从锚点 A 之后开始,遇 `^\d+[．.、]` 的段落开新题,直到锚点 B;答案区同理,按题号与题目配对。
- 片段内的 `r:embed`/`r:id` **保持原值入库**,manifest 记录"原 rId → 部件"的映射;重排 rId 只在组卷时做(D-4)。
- 前置元数据表(锚点 A 之前)丢弃,但 `课题` 一行可留作文档标题的兜底。

## 5. 数据模型(SQLite,经 `src/storage`)

```
documents(id, source_path, filename, sha256, chapter, section, lesson, title,
          status[ok|error], error_msg, ingested_at)
questions(id, doc_id, qno, ord, stem_xml, answer_xml, manifest_json,
          stem_text, n_figure, n_formula)
questions_fts   ← storage.FtsTable(stem_text),查询用 storage.match_expr(CJK 折叠)
papers(id, title, question_ids_json, out_path, created_at)
```

- 筛选:章/节/课时/标题 = `documents` 上的普通列,SQL 过滤;关键词 = FTS5。两者 AND 组合。
- 媒体字节**不进 DB**:存 `cache/exam_builder/media/<sha256前2位>/<sha256>.<ext>`,DB 只存 sha + 角色。缩略图同理可重建(D-7)。

## 6. 三条管线

### 6.1 ingest(`exam ingest <dir>`)

1. 遍历 `.docx`(`.doc` → `status=error`);sha256 去重,未变则跳过。
2. 解析文件名 → chapter/section/lesson/title(D-6)。
3. 读 `word/document.xml`,按 §4 切题;锚点缺失/题号答案不配对 → `status=error` + 原因,进待处理列表(D-5)。
4. 抽媒体/嵌入部件到 cache,写 manifest;生成 `stem_text` 投影;写 FTS。
5. 打印入库报告:每文档题数、未配对答案数、error 列表。

### 6.2 search / browse

- `GET /search?q=&chapter=&section=&lesson=` → 题卡片列表(文本 + 几何 PNG 缩略图 + 「含公式×N」徽标 + 勾选框)。
- 模糊搜索 = FTS5 + `fold_cjk`;MVP 不做语义/公式检索(D-3)。

### 6.3 compose(组卷,包级手术)

输入:有序 question id 列表 + 试卷标题。步骤:

1. 骨架不取现成模板文件,而是在代码里生成三个最小部件(`document.xml` 外壳 /
   `styles.xml` / `_rels/.rels` + `[Content_Types].xml`,见 `compose._content_types_xml`):
   骨架里没有需要继承的编号/主题,少一个二进制夹具就少一处会过期的东西。
2. 依次把每题 `stem_xml` 节点注入 body;**重编号**为 1..N(源片段不动)。
   实测坑:题号常被 Word 拆成多个 run(`"3"` + `"．如图…"`),所以定位要按**整段拼接文本**
   匹配 `QNO_RE`、再把被吃掉的 run 清空(`compose._renumber`);只改首个 `w:t` 会得到
   `1．3．如图` 这种双编号。无字面题号(如 `w:numPr` 自动编号的题)则前插。
3. 追加"参考答案"区,按新编号排 `answer_xml`。
4. **搬部件**:对每个引用的 media/embedding,按 sha 去重拷入新包;分配新 rId;改写片段内所有 `r:embed`/`r:id`;更新 `document.xml.rels` 与 `[Content_Types].xml`(wmf/png/bin 的 default/override)。
5. 写出 `.docx`,记 `papers` 行,浏览器下载。

验收:用 Word 打开,公式与几何图**原样可见**;把产物重新 ingest,题数与题干文本应与选择一致(往返测试)。

## 7. UI 线框(D-8)

```
┌ 文档 ────────────────────────────────────────────────────────┐
│ 13 三角形 (12)  14 全等 (10)  16 整式 (9) …   [按章折叠+计数] │
├ 搜索 ────────────────────────────────────────────────────────┤
│ [关键词________] [章▾][节▾][课时]   搜索                      │
│  ☐ 1．五条线段…(13.2.1)        〔图×1〕〔公式×0〕              │
│  ☐ 2．三角形三边长…(13.2.1)                                    │
├ 已选 (3) ───────────────────────────────── [清空] [组卷 →] ──┤
└──────────────────────────────────────────────────────────────┘
组卷页:有序列表(可上移/下移/移除)+ 标题输入 + [生成] → 下载 .docx
```

三页足够:`/docs`、`/search`、`/compose`。无 auth、无构建链。

## 8. 里程碑

- **M0(入库+CLI,无 UI)—— shipped 2026-10-17**:`exam ingest/ls/search/compose/serve`。
  实测语料 `八上课后练习`(55 份):**53 ok / 2 error,共 172 题**;2 个 error 全是
  legacy `.doc`(预期内,归 M2),没有意外 error。抽查 3 份题数与原文件一致。
  媒体缓存 `cache/exam_builder/media/`:76 png / 75 wmf / 75 bin / 3 jpeg / 4 emf
  —— 与 §3.3 的"公式=OLE+WMF 预览、图=PNG 内联"逐条对上。
  golden 夹具:`tests/golden/exam_split.jsonl`(字面题号 / `w:numPr` 自动编号 / 缺锚点三例)。
- **M1(Web + 组卷)—— shipped 2026-10-17**:§7 三页(`/docs` `/search` `/compose`)
  加 compose 包级手术。包完整性已验:真实语料 4 题组卷,产物 13 个部件,
  文档里出现的每个 `r:embed`/`r:id` 都能在 `document.xml.rels` 解析到、且对应部件真在包内
  (missing = []),python-docx 能打开、`zipfile.testzip()` 干净。
  13 个单测通过(切题 golden、CJK 检索、compose 往返 + rId 完整性 + 拆 run 重编号)。
  **欠账(L3)**:产物还需在真 Word 里肉眼确认公式(OLE 双击可编辑)与几何图保真 ——
  代码侧只能证明部件与关系齐全,证明不了 Word 的渲染。
- **M2(后置,已裁决不做进 MVP)**:公式 WMF→PNG 渲染(路已通,§3.3)、难度/题型标签、按题干找同类题、人工校对编辑器、`.doc` 转换。

## 9. 已知限制(诚实记录)

- 公式在网页不可见(MVP),只能看到「含公式×N」;要确认公式内容需打开原 Word 或等 M2。
- 公式文本不可搜索(D-3);"同类题"在 MVP 退化为可见文字 + 结构标签的检索。
- 2 个 `.doc` 不入库(MVP),列在 error 报告里。
- 切题依赖模板锚点;换一批不同模板的文档需重新校准锚点/题号规则(届时 D-5 的 error 列表就是校准入口)。

## 10. 验证门

| 门 | 内容 |
|---|---|
| L1 pre-commit | 常规 black/isort/mypy/flake8 |
| L2 CI | 单测:切题(含锚点缺失/题号变体)、rId 重排、manifest 去重;golden 切题夹具 |
| L3 真机 | `ingest` 全 53 份;compose 产物用 Word 打开肉眼验公式/图 |
| L4 证据 | 往返测试(产物 re-ingest 文本一致)记录进本文 §8 验收 |
