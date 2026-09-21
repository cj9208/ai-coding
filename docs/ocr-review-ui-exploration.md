# OCR 人工校对 UI 探索（调研 + 方案）

> 一句话核心：OCR 跑完先产出契约 JSON，人工校对做成一个**独立的薄 Web UI**（FastAPI + Jinja + 原生 JS + SVG overlay，零构建链），它读 PDF 和 `OcrDocument`、把页栅格到契约声明的像素网格上、用百分比定位做框叠加；人的修改**不写回契约**，而是落在一份稀疏的 review sidecar 里，最终产物由 `apply_review(doc, review)` 生成一份标准 `OcrDocument`。核心权衡是"复用通用标注工具"vs"自建薄 UI"——因为我们的数据模型是**块级 + 带 kind / content_format 的富文本**，而通用标注工具的世界模型是"图片 + 多边形 + 单条文本"，映射成本高于自建。

日期：2026-09-21。状态：**已实现并真机验证（M1 + M2 + M3 的纯函数部分）**——`src/ocr_review/`（review/patch/workspace/render_pages/app/cli + 零构建前端），`ocr-backend parse` 已同步随拷源文件进 out/。浏览器端用 `verify_doc.pdf` 真机 OCR 产物走通：栅格与契约逐像素对齐、内容修正 / kind 改判 / 删块 / 拖框补录 / 署名 / 导出（`apply_review` 产物通过 `OcrDocument` 校验、机器 JSON 原样未动）；`tests/test_ocr_review/` 20 条。真机验证还抓到并修复了一个 Chrome 兼容 bug（`SVGPoint.matrixTransform` 拒绝 DOMMatrix，改用线性换算，见 review.js 注释）。唯一欠账：可见窗口下的真实鼠标拖框未人工复验（自动化环境视口隐藏，用同源派发验证了逻辑）。前置阅读：[ocr-backend-design.md](ocr-backend-design.md)（契约与适配器，已实现）、[service-containerization-exploration.md](service-containerization-exploration.md)（runner 与文件协议，本文沿用）。读法：§2 是要先分清的两类用途；§3 是三条决定成败的技术点；§4 是买/用现成 vs 自建的对照；§5 是推荐形态与数据模型；§6 是范围切分与里程碑。

## 1. 目标场景

已有的链路是 `ocr-backend parse scan.pdf --out results/` → `<stem>.ocr.json`（契约）+ `<stem>.ocr.md`（投影）。缺的是人对结果的介入：OCR 认错字、把表格切成两段、漏掉一栏、把 caption 判成 paragraph——这些错误机器修不了，人眼扫一遍很快。

构思中的流程：

```
ocr-backend parse ──> x.ocr.json ──┐
                                   ├──> [ 校对 UI ]  ──> 人逐页比对/修改
PDF 原文件 ────────────────────────┘                     │
                                                         ├─> 修订版 OcrDocument（喂下游）
                                                         └─> golden 样本（评后端升级）
```

## 2. 先分清两类用途（决定数据形态）

| 用途 | 产出 | 下游 | 对 UI 的要求 |
|---|---|---|---|
| A 生产修订 | 一份内容正确的文档 | pdf_summarizer / file_manager 检索 | 改字方便、导出即标准契约 |
| B 攒 golden 样本 | 「机器输出 + 人工真值」配对 | 评估后端升级（1.6 → 1.7 谁更准）、未来微调 | 原始输出必须**原样保留**，人的修改可 diff、可回放 |

B 是本仓库特别值钱的一条：`tests/test_ocr_backend/fixtures/` 已经在用"真机产物当 golden"的做法（§6 的升级流程靠它），但目前 golden 只覆盖**映射层**（res JSON → 契约），没有覆盖**识别质量**。有了人工真值，"换模型后准确率是升是降"才有可测口径。

这也是本文反对"直接在 `.ocr.json` 上改"的根本原因：一旦原地修改，A 得到了好处，B 的配对数据就永久丢失了。

## 3. 三条决定成败的技术点

### 3.1 坐标配准：栅格化到契约声明的像素网格

契约的 bbox 是 `image_px_top_left`，页尺寸 `width × height` 是 Paddle 管线内 pypdfium2 按 **2× 页面点尺寸**渲染出来的（A4 595.3pt → 1191×1684，≈144 DPI；见 ocr-backend-design §5.3 第 1 条）。人看到的底图和 bbox 必须落在同一个网格上，否则框会漂。

结论（推荐）：**服务端出图，且强制按契约声明的 `width × height` 渲染**，不是"随便选个 DPI 再 hoping 对齐"。

- 渲染器用 **PyMuPDF（`fitz`）**——它已在核心依赖里（pdf_summarizer 在用），校对 UI 因此**不需要装任何 OCR extra**，也不依赖 paddle 环境；Paddle 用 pypdfium2、我们用 fitz，两者都按页面 MediaBox 点数栅格化，只要目标像素尺寸一致，配准就是精确的（同一页矩形、同一原点）。
- 换算：`zoom = 目标显示宽 / page.width`，`fitz` 侧用 `Matrix(zoom_x, zoom_y)`，其中 `zoom_x = page.width / page.rect.width`。落盘 PNG 尺寸与契约 `width/height` 逐页相等。
- 前端定位用**百分比**而不是像素：`left = bbox[0]/page.width*100%`，`top = bbox[1]/page.height*100%`。好处是缩放/响应式完全免费（底图和 overlay 一起缩放），不需要维护 viewport 变换矩阵。

被否掉的方案：**PDF.js 做底图 + 绝对定位 overlay**。可行但要处理 CSS 像素 / PDF point / 契约 px 三套单位和 devicePixelRatio，且它的文字层对扫描件（我们的主要素材）**是空的**——扫描件没有可选文本，PDF.js 相对栅格化的唯一优势（选中原文比对）在此场景不成立。矢量放大更清晰这个优势，在 144 DPI 底图 + 浏览器缩放的实际观感下也不构成决定性差异。

### 3.2 编辑层分离：review sidecar（稀疏补丁）

`OcrDocument` 保持"机器输出的原始事实"。人工动作写在旁边的 `<stem>.review.json`：

```
ReviewDocument
  schema_version: "1.0"
  target:   { ocr_json_path, ocr_json_sha256, pdf_sha256 }   # 钉住它校对的是哪一次运行
  reviewer, created_at, updated_at
  page_status: { page_index: "pending" | "done" }
  blocks:   [ (page_index, block_id) -> ReviewEntry ]        # 只存被碰过的块
ReviewEntry
  state:    kept | corrected | rejected | added              # rejected = 人判定为假块
  content?  kind?  content_format?  bbox?  order?            # 未改的字段 = 继承机器值
  note?     为什么改（B 用途的复盘信息，也是将来喂模型的提示）
  updated_at
```

三条规则：

1. **稀疏**：没动过的块一条记录都不写 → 机器输出重跑一次，补丁还能贴回去。
2. **`apply_review(doc, review) -> OcrDocument`** 是纯函数（放 `ocr_backend` 还是 `ocr_review` 见 §5.4）：产出仍是合法契约，消费方**完全不知道**有没有人工介入过——契约作为唯一交换形状的地位不被削弱。
3. **贴回去要匹配**：换模型重跑后 block_id 可能变，匹配按「页序 + bbox IoU + 内容相似度」做，匹配不上的块标记 `needs_recheck` 而不是静默丢弃。这条是 B 用途的全部价值所在，值得在 v1 就实现一个朴素版本（IoU 阈值 0.5 即可）。

### 3.3 契约要不要加 review 字段？→ 不加

`OcrBlock` 上加个 `verified: bool` 是兼容变更（§6 版本策略允许），但会把关注点混进契约：契约的定义域是"后端产出了什么"（`contract.py` 的模块 docstring 明确写了"What stays OUT"）。人的判断属于工作流，属于 sidecar。如果将来确实需要"这份文档经人工校对"这个信息随文档流动，正确做法是 `apply_review` 时在文档级加一个可选 `provenance`（例如 `revised_by: "human:review-ui@1.0"`），而不是块级布尔位。

## 4. 现成工具 vs 自建

| 候选 | 数据模型 | 与我们的契合度 | 结论 |
|---|---|---|---|
| [PPOCRLabel](https://github.com/PFCCLab/PPOCRLabel) | Qt 桌面 + 图片目录，**行级** quad + 单串文本 | 它不认 PDF、不认块级契约、没有 kind / content_format 概念；导入导出胶水代码 ≈ 自建工作量，还丢掉表格里 HTML 的编辑能力 | 否 |
| [Label Studio](https://labelstud.io/templates/optical_character_recognition) | 自己的任务/用户/SQLite + rectangle + transcription | OCR 模板面向"图 + 框 + 转写"，块级语义、表格 HTML 编辑、markdown 并排预览都要靠自定义模板硬凑；内联 PDF 是 **Enterprise 商业功能**；且它自带一套存储，与本仓 `data/<project>/` 约定冲突 | 否（若将来要训练模型、需要对外交换标注格式，再考虑把 review 导出成它的 JSON） |
| PaddleX / PaddleOCR 自带可视化 | `save_to_img` 画框图 | 只读展示，无编辑 | 不满足 |
| docTR editor / CVAT | 行/多边形 | 同 PPOCRLabel | 否 |
| **自建薄 UI** | 直接就是契约 | 块列表 = `page.blocks`，编辑对象 = `content` + `kind`，无需任何格式转换 | **推荐** |

自建之所以"薄"，是因为契约已经把最难的部分（结构、坐标、语义类型）定好了，UI 只是它的一个视图。

## 5. 推荐落地形态

### 5.1 位置与入口

新包 `src/ocr_review/`，与 `file_manager` 同构（FastAPI + `Jinja2Templates` + `/static`，仓库已有现成范式），注册 `[project.scripts] ocr-review` 并加入 `packages` 列表后重装 editable。

```
ocr-review serve --inbox data/ocr_backend/out [--port 8765]
# → 列表页自动扫描 inbox 里的 *.ocr.json：空闲时 runner 批量解析产出的文档，
#   人打开浏览器时就能看到；首次点开某文档时才建工作区（栅格化页图）。
ocr-review add <pdf> --ocr <x.ocr.json>    # 备用：登记不在 inbox 约定路径下的一对文件
```

识别与审阅的解耦是本设计的骨架：**OCR 按机器的空闲时间跑，审阅按人的时间做**，两边唯一的交接物就是 `<stem>.pdf + <stem>.ocr.json` 这对文件（runner 已有的输出形态，见 containerization 探索的 out/ 约定）。审阅端从不等待推理，也不需要知道文档是怎么产出来的。

一个小的协议缺口：`ocr-backend parse` 目前只写 `.ocr.json` / `.md`，不带源 PDF。审阅要底图就得能拿到原文件——两条路：runner 把源 PDF 同拷进 out/（交接物自包含，推荐），或审阅端按契约里的 `source.path` 去解析（跨机器时路径多半失效）。前者是 runner 的一行改动 + out/ 目录约定的文档化。

**不在 UI 里触发 OCR 推理**（v1）：跑 OCR 需要 paddle extra 和 GPU，校对端只需要 fastapi + pymupdf + jinja2（全是核心依赖）。保持 §3.1 那样的文件协议，也意味着校对 UI 可以跑在没有 OCR 环境的机器上（甚至容器的 runner 产完物就退出，UI 单独起）。"UI 内一键重跑"列为后续按需项。

**使用者与部署形态（已明确）**：会给业务同事用，但协作模型是"一份文档同一时刻只有一个人审"——不存在两人共审一份的合并问题。工作流是：你们这边空闲时跑 OCR（本机或 Docker runner，产物落 `data/ocr_backend/out/`），业务同事任何时候打开浏览器，**列表页 → 校对 → 保存 → 导出**，全程不碰命令行。因此：

- 服务是长驻的（uvicorn 起在部署机上，同事浏览器访问），不是一次性命令——但**仍然不做鉴权**（内网可信同事，与 file_manager 的既有取舍一致）；
- `ocr-review` 命令只是启动器（`serve --inbox …`），列表页 = inbox 扫描结果 + 已建工作区的进度（几页 done / 共几页、最后修改时间、修改人）；文档间的"排队"就是 out/ 目录本身，不引入任务表；
- 工作区按文档（PDF sha256）组织、不按用户——同一文档天然只有一个 `review.json`，双人同时开同一页靠 §5.5 的乐观写检冲突提醒。

### 5.2 工作区目录

```
data/ocr_review/<pdf-sha256[:12]>/
  review.json                 # 唯一的可编辑真相（原子写：tmp + os.replace）
  pages/page-000.png          # 派生缓存，按契约 width×height 渲染，可随时重生成
  export/                     # apply_review 产物：<stem>.ocr.json / .md
```

`data/` 整体 gitignored，与既有约定一致；工作区按 PDF 哈希命名，天然避免同名冲突与"换机器找不到"。

### 5.3 界面

```
┌───────────────────────────────┬──────────────────────────────┐
│  页 <2/5>   [−] 缩放 [+]  [✓ 本页已校对]                       │
│  ┌──────────────────────────┐ │  块列表（阅读顺序）             │
│  │  PNG 底图                 │ │  ├ 1 title      ·已修正 ✎     │
│  │   ▢ 悬停高亮，点击选中      │ │  ├ 2 paragraph              │
│  │   选中块描边 + 8 个手柄     │ │  ├ 3 table       ⚠ kind?     │
│  │   空白处拖拽 = 新增块       │ │  └ …                        │
│  └──────────────────────────┘ │ ┌──────────────────────────┐ │
│                               │ │ content 编辑器（textarea） │ │
│                               │ │ [原文] [编辑] [预览]       │ │
│                               │ └──────────────────────────┘ │
└───────────────────────────────┴──────────────────────────────┘
```

要点：

- overlay 用一个绝对定位的 `<svg viewBox="0 0 width height" preserveAspectRatio="none">`，坐标直接写契约像素——**零手算缩放**；块列表与框双向联动（hover 高亮、点选定位）。
- "原文/编辑"切换用左右并排（原文只读、编辑区可改），比对错误主要靠这个而不是靠记忆。
- 预览按 `content_format` 分派：`html` → `innerHTML`（**必须 sanitize**，DOMPurify；OCR 内容是不可信输入，表格 HTML 直渲会有 XSS 面）、`latex` → KaTeX、`markdown` → marked，全部 CDN 引入、拿不到时退化为纯文本。
- 键盘流：`j/k` 上下块、`Enter` 进编辑、`Esc` 退出、`a` 接受、`d` 删除——校对是高频重复动作，鼠标流会让人放弃用它。

### 5.4 包内结构（预测）

```
src/ocr_review/
  app.py        # create_app(workspace) → FastAPI
  workspace.py  # 目录约定、review.json 读写（原子）、sha256 校验
  render_pages.py  # PyMuPDF 按契约页尺寸栅格化 + 缓存失效
  patch.py      # apply_review(doc, review) -> OcrDocument；reanchor(旧doc,新doc) 匹配
  templates/ static/
tests/test_ocr_review/   # patch/reanchor 纯函数测试为主，UI 只测路由装配
```

`apply_review` 放 `ocr_review` 而非 `ocr_backend`：契约层继续保持"纯数据、无工作流概念"（`contract.py` 的设计规则），补丁语义是校对层的知识。

### 5.5 并发与身份

协作模型是"一人一份、串行审阅"，所以只需要两件事：

- **原子写 + 乐观冲突检**：`review.json` tmp + `os.replace` 落盘；每次保存带上读到的 `updated_at`，服务端发现盘上更新则返回 409，UI 提示"他人已保存，请刷新"。不做行级锁、不做实时协同。
- **手工身份**：`ReviewEntry.updated_by` 记录修改人。参照 `file_manager/identity.py` 的既有做法（无鉴权、首次进入选/输入名字存 cookie），不引入登录体系。

v1 不建 DB——单文件 JSON 足够，且 diff 可读、能直接进 fixtures。若将来工作区数量大到列表页需要检索/统计，再把 `review.json` 灌进 `src/storage/sqlite.py` 做索引（文件仍是交换格式与唯一真相）。

## 6. 范围切分与里程碑

v1 做：工作区列表页（进度一目了然，业务同事的唯一起点）；编辑 `content`；改 `kind`（12 值枚举下拉，带 `raw_label` 作提示）；`reject` 假块；拖框 `add` 漏检块；改 `order`（数字输入，不做拖拽排序）；页级 `done` 状态；导出契约 + markdown；`reanchor` 朴素版。

v1 明确不做：块合并/拆分（表格跨页尤其麻烦，是 Paddle `restructure_pages` 的领域）；行级/字符级编辑（契约没有行级，见 ocr-backend-design §4.4）；批量任务队列与登录鉴权（内网可信同事，身份只用于署名，见 §5.5）；UI 内触发推理；gold 准确率报表。

| 里程碑 | 内容 | 验收 |
|---|---|---|
| M1 | 工作区登记/列表 + 页栅格化 + 只读展示（框叠得上） | 拿 `data/ocr_backend/verify/materials/verify_doc.pdf` 的既有产物跑通，逐页目测 5 页框不漂 |
| M2 | 编辑层：`review.json` 读写 + 块操作 + 键盘流 + 署名/冲突提醒 | 业务同事视角走通：打开列表 → 选文档 → 校对保存 → 导出；关掉服务再打开，改动还在；导出文件能被 `OcrDocument.model_validate` 通过 |
| M3 | `reanchor` + 评测闭环 | 换 `--pipeline-version` 重跑同一 PDF，旧人工补丁贴回新输出，未匹配块被标出 |

## 7. 决策记录

| 决策 | 结论 | 原因 |
|---|---|---|
| 复用现成标注工具 | 自建薄 UI | 通用工具的世界模型是"图+多边形+单串文本"，块级契约/表格 HTML/markdown 预览都要硬凑，胶水成本高于自建 |
| 底图 | 服务端 PyMuPDF 按契约页尺寸出 PNG | 扫描件无文字层，PDF.js 的选择文本优势不成立；栅格化到契约网格 = 配准零换算；PyMuPDF 在核心依赖里，校对端不必装 paddle |
| 修改落在哪 | 独立 review sidecar，不动契约 JSON | 保住 B 用途的「机器输出 ↔ 人工真值」配对；原地改一次就永久丢失 |
| 契约是否加 review 字段 | 不加 | 契约定义域是"后端产出了什么"；人工判断属工作流 |
| 常驻服务/DB | 长驻 uvicorn + 单 JSON 文件 | 给业务同事用 = 部署机常驻、浏览器访问；但一人一份的协作模型不需要 DB，文件 diff 可读、可进 fixtures（见 §5.5） |
| 在哪跑 OCR | UI 不触发推理，走文件协议 | 环境依赖分离（校对端零 OCR 依赖），与 containerization 探索的 runner 边界一致 |

## 8. 未决问题

1. **B 用途的消费口径**：golden 攒到什么格式才算可用？候选是「契约 JSON + review sidecar」双份进 `tests/test_ocr_review/fixtures/`，加一个"新旧后端各自 `apply_review` 后与真值比 block 级 CER / 框 IoU"的评测脚本——需要单独设计指标，别在本文里顺手定。
2. **图片输入**：非 PDF 的照片/扫描件是 `source.kind="image"`，此时底图应直接用原图而非重栅格化；`reanchor` 的页序匹配也更弱。v1 先支持 PDF，图片列为 follow-up。
3. **旋转/畸变页**：若将来开 `use_doc_unwarping`，Paddle 输出坐标在**去畸变后**的栅格上，与原始 PDF 页面对不上——那时底图必须同样取管线内的预处理结果。目前默认关，不影响 v1。
4. **部署方式**：校对 UI 只有核心依赖（fastapi/pymupdf/jinja2），比 OCR runner 轻得多——部署机上 `uv sync` 后 `ocr-review serve` 即可，未必要做成容器；与 Docker 化 OCR runner 在同一台部署机上就是共享 `data/ocr_backend/out/`（runner 写入 = 待审队列）。待定的是部署机是哪台机器、同事怎么拿到地址，以及是否需要"同事自己拖 PDF 上传、排队等空闲时 OCR"的形态（那会把 §5.1 刻意排除的推理调度重新引进来，需单独立项评估）。

## 9. 参考资料

- 仓库内事实：[OCR 后端层设计](ocr-backend-design.md)（契约 §4.2、真机坐标 §5.3、版本策略 §6）、[服务 Docker 化探索](service-containerization-exploration.md)（runner 文件协议）、`src/file_manager/app.py`（FastAPI + Jinja + static 的现成范式）、`src/pdf_summarizer/extractor/paddle_vl.py`（契约消费方样例）。
- 现成工具：[Label Studio OCR 模板](https://labelstud.io/templates/optical_character_recognition)、[Label Studio Enterprise 的内联 PDF 标注](https://humansignal.com/blog/inline-pdf-labeling-in-label-studio-enterprise-for-ocr/)、[PPOCRLabel](https://github.com/PFCCLab/PPOCRLabel)、[PaddleX OCR 标注说明](https://paddlepaddle.github.io/PaddleX/3.3/en/data_annotations/ocr_modules/text_detection_recognition.html)。
