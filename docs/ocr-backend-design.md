# OCR 后端层设计（调研 + 实现）

> 一句话核心：OCR 这层先定"输出契约"而不是先写 wrapper——用一份版本化的 `OcrDocument`（页 → 块，带阅读顺序、版面坐标、来源与版本元数据）做两端同步点；PaddleOCR-VL 最新版（1.6）作为首个后端，适配器只做"原生输出 → 契约"的单向映射，消费方只认契约。这样后端适配器与消费方各自迭代，互不阻塞。

日期：2026-09-21。状态：§5.3 的六项真机验证已完成（结论在 §5.3，原始产物在 `data/ocr_backend/verify/`）；契约、投影与 PaddleOCR-VL 适配器已实现在 `src/ocr_backend/`（含测试）；消费方迁移（pdf_summarizer / file_manager）尚未开始。读法：§2–§3 是调研事实；§4–§6 中标注"已实现"的即当前代码，未标注的仍是设计意图；§7 是剩余步骤与已决 / 待定项。

## 1. 背景与目标

仓库现状里有三件事指向这次探索：

- `src/pdf_summarizer/extractor/paddle_ocr.py` 还是 PaddleOCR 2.x 风格（`PaddleOCR(...)` + `.ocr()`），把整页结果拼成一串纯文本，版面、阅读顺序、表格结构全部丢弃；
- `shell_scipts/run_paddle_download.sh` 已把 `PaddlePaddle/PaddleOCR-VL-1.6` 模型快照拉到本地（`.gitignore` 已忽略 `paddleocr-vl-1.6/`），方向已经押在 VL 路线上；
- `pyproject.toml` 已有可独立安装的 OCR extras：前端栈 `ocr`（`paddleocr[doc-parser]` + `pdf2image`）与引擎 `paddle-cpu` / `paddle-gpu`，三个都不进核心安装，装法已就位。

为什么先定输出契约，而不是先写 wrapper：wrapper 的复杂度约等于各家原生输出的差异量。差异如果先被一份两端都认的契约吸收，适配器与消费方（pdf_summarizer、file_manager）就能并行推进；Paddle 侧升级（1.5 → 1.6 官方就是"零成本兼容"的例子）只需要改适配器、更新 golden 样本，契约不动。这就是"两边同步开发升级"的抓手。

目标：

- 定一份本仓库的 OCR 规范输出：JSON 可序列化、带版本号、可投影为纯文本 / markdown；
- 给一个最小后端接口，PaddleOCR-VL（只收 1.6+）是第一个实现；
- 现有 pdf_summarizer 的 `Document` / `Page` 暂时不动，只留一条桥接路径。

非目标（本次刻意排除）：

- 不做插件注册表、多后端编排、统一异步框架；
- 不兼容 PaddleOCR 2.x 旧 API——现有 `paddle_ocr.py` 后续整体替换，不是改造；
- 不把后端原生输出做完全保真建模——原生 JSON 走 sidecar 留存，契约不背这个包袱。

整体分层：

```
┌────────────────────────────────────────────────────────────┐
│ 消费方　pdf_summarizer / file_manager / …                    │
└──────────────▲─────────────────────────────────────────────┘
               │ OcrDocument（本仓契约：页 → 块；JSON 可序列化）
┌──────────────┴───────────────┐
│ src/ocr_backend/contract.py  │ ← 两端同步点（版本化 + capabilities）
│            render.py         │ ← 投影：纯文本 / markdown
└──────────────▲───────────────┘
               │ 差异只在这里被吸收（单向映射 + golden 测试）
┌──────────────┴───────────────────────┐
│ backends/paddleocr_vl.py（适配器）    │
└──────────────▲───────────────────────┘
               │ 原生：Result.res → parsing_res_list / layout_det_res
┌──────────────┴───────────────┐
│ paddleocr.PaddleOCRVL        │  pipeline_version 锁定 "v1.6"
└──────────────────────────────┘
```

## 2. 调研：PaddleOCR-VL 1.6

### 2.1 版本与使用形态

| 事项 | 事实 |
|---|---|
| 模型 | PaddleOCR-VL-1.6（0.9B，ERNIE 4.5 架构，Apache 2.0，在 1.5 上升级，官方称"零成本即插即用"） |
| 发布 | 2026-05-28（随 PaddleOCR 3.6.0）；技术报告 2026-06-03（arXiv 2606.03264）；HF 仓库最后更新 2026-08-08 |
| 成绩 | OmniDocBench v1.6 96.33%（官方宣称 SOTA，同时在 v1.5 与 Real5-OmniDocBench 刷新记录） |
| Python 包 | `paddleocr[doc-parser]`（本仓库已锁 >=3.7.0；PaddleOCR 最新为 3.7.0，2026-06-11） |
| 运行时 | `paddlepaddle >= 3.2.1`；x86_64；macOS 官方建议走 Docker；GPU 轮子限 Windows / Linux |
| 本地模型 | `paddleocr-vl-1.6/`（HF 快照已下载；`inference.yml` 模型名 `PaddleOCR-VL-1.6-0.9B`） |

装依赖的坑（本机实测，已解）：PyPI 上 `paddlepaddle-gpu` 最新只到 2.6.2，与 `paddlex` 3.x 不兼容（跑 OCR 直接 AttributeError）；解法是 pyproject 里声明 Paddle 官方 cu126 源并挂到 `paddle-gpu` extra（`[[tool.uv.index]]` + `[tool.uv.sources]`），`uv lock` 可直接解析出 3.3.1（详见 §7 "OCR 依赖拆分"）。

三种调用形态（选哪种属于适配器内部决定，不影响契约）：

1. 本地 pipeline：`PaddleOCRVL(pipeline_version="v1.6").predict(path)`；
2. CLI：`paddleocr doc_parser -i <input> --pipeline_version v1.6`；
3. VLM 推理服务：vLLM / Docker 起服务后 `vl_rec_backend="vllm-server"` + `vl_rec_server_url`。

另有一条 `transformers` 直连路线，只支持元素级识别、不支持整页拆解，官方不推荐做主线。

### 2.2 原生输出：字段级结构（真机核对）

`predict()` 对每个输入页返回一个 Result 对象。`Result.json` 在内存里包一层 `{"res": …}` 外壳；`save_to_json` 落盘写的却是**不带外壳**的 res dict（本仓 sidecar 存的就是后者）。结构如下，节选自真机 runA 第 0 页输出（数值为实数，个别字段有截断）：

```json
{"res": {
  "input_path": "...\\materials\\verify_doc.pdf", "page_index": 0, "page_count": 5,
  "width": 1191, "height": 1684,
  "model_settings": {"use_doc_preprocessor": false, "use_layout_detection": true,
                     "use_chart_recognition": false, "use_seal_recognition": false,
                     "use_ocr_for_image_block": false, "format_block_content": false,
                     "merge_layout_blocks": true, "return_layout_polygon_points": true,
                     "markdown_ignore_labels": ["number", "footnote", "header", "header_image",
                                                "footer", "footer_image", "aside_text"]},
  "layout_det_res": {"boxes": [
      {"cls_id": 17, "label": "paragraph_title", "score": 0.7884, "order": 1,
       "coordinate": [95.0, 129.0, 825.0, 171.0], "polygon_points": [[95.0, 129.0], "…"]}]},
  "parsing_res_list": [
      {"block_label": "header", "block_content": "AI Coding 内部技术报告 · 第 3 期",
       "block_bbox": [97.0, 54.0, 361.0, 78.0], "block_id": 0, "block_order": null,
       "group_id": 0, "block_polygon_points": [[97.0, 54.0], "…"]},
      {"block_label": "paragraph_title", "block_content": "PaddleOCR-VL 1.6 Output Verification",
       "block_bbox": [95.0, 129.0, 825.0, 171.0], "block_id": 1, "block_order": 1,
       "group_id": 1, "block_polygon_points": [[95.0, 129.0], "…"]}]}}
```

| 字段 | 含义 |
|---|---|
| `width` / `height` | 本页栅格尺寸（像素）——实测存在，契约页尺寸直接取这里（§5.3 #1） |
| `page_index` / `page_count` | 页序 / 总页数；图片输入时两者为 `null` |
| `layout_det_res.boxes[]` | 版面检测框：`cls_id` 类号、`label` 标签、`score` 检测分、`coordinate` 为 `[x1,y1,x2,y2]`、`polygon_points` 四点、`order` 检测框顺序（与块的 `block_order` 无关，见 §2.3 #2） |
| `parsing_res_list[]` | 识别结果块：`block_label` 类型、`block_content` 内容、`block_bbox` 框、`block_id` 列表内序号（0 起整数）、`block_order` 阅读顺序（可为 `null`）、`group_id`、`block_polygon_points` |
| `model_settings` | 本次推理的开关回显（含 `markdown_ignore_labels`、`return_layout_polygon_points` 这类内部开关） |

结果对象自带 `save_to_json` / `save_to_markdown`（逐页 .md 文件）/ `save_to_img`（可视化）；`restructure_pages()` 可做跨页表格合并等页面重建。内存里的 `Result.json` 与落盘文件都会把数组转成普通 list；但 `block_bbox` 的元素实测是 numpy 标量，适配器统一 `float()` 归一（§4.3）。

### 2.3 对契约设计有影响的六个事实（实测校准）

1. 识别侧没有置信度。维护者在 issue #16899 里的原话：*"The VLMs are almost unable to provide confidence scores."* 只有版面检测有 score。→ 契约里块级分数必须可空，语义要写清是"版面分"。
2. `block_order` 实测形态：主阅读流上是从 1 起的连续整数；被判定为"页面附属 / 独立元素"的块（header、footer、number、table、figure_title、chart、image）为 `null`。`block_id` 是 `parsing_res_list` 里的 0 起序号，与 `block_order` 不互为反函数（实测第 0 页：header 的 id=0/order=null，paragraph_title 的 id=1/order=1）。→ 阅读顺序字段必须可空；适配器保留原值。
3. `block_content` 的格式随 label 和开关变化，实测：正文默认纯文本；`format_block_content=True` 时标题带 `### ` 前缀、表格 HTML 变成带 `border=1 style=…` 内联样式；表格无论开关都是 `<table>` HTML；`display_formula` 形如 ` $$ … $$ `（前后带空格），`inline_formula` 嵌在段落文本里以 ` $ … $ ` 出现。→ 契约需要 `content_format` 判别字段；包裹符归一化在适配器做（§4.3）。
4. 页尺寸字段**存在**（`width` / `height`）：PDF 由管线内 pypdfium2 按 2× 页面点尺寸栅格化（A4 595.3pt → 1191×1684px，≈144 DPI），可用环境变量 `PADDLE_PDX_PDF_RENDER_SCALE` 覆盖；图片输入则等于图片原生像素（150dpi 的 A4 PNG = 1241×1754）。→ 适配器不需要自己栅格化，px → pt 换算系数 = `width_px / 页面点数宽`；契约仍把坐标空间与页尺寸写成必填。
5. PDF 逐页推理，跨页合并不是默认行为（要显式调 `restructure_pages`）。→ v1 契约保持页级；跨页重建列为后续能力。
6. markdown 渲染受 `markdown_ignore_labels` 影响：默认丢弃 `number`、`footnote`、`header`、`header_image`、`footer`、`footer_image`、`aside_text` 七类。→ 渲染放到我们这边做，后端 markdown 只当参考产物。

版面标签：实测运行时用的是 **PP-DocLayoutV3**——此前按 V2 文档记的 `contents` / `table_title` / `chart_title` / `formula` 等标签实际并不出现，25 类为：`abstract`、`algorithm`、`aside_text`、`chart`、`content`、`display_formula`、`doc_title`、`figure_title`、`footer`、`footer_image`、`footnote`、`formula_number`、`header`、`header_image`、`image`、`inline_formula`、`number`、`paragraph_title`、`reference`、`reference_content`、`seal`、`table`、`text`、`vertical_text`、`vision_footnote`。另有 `ocr` 一个非版面标签：`use_layout_detection=False` 时整页块用它（§5.3 #4）。标签集会随版本微调，契约按"未知标签保留原文"处理（见 §4.3）。

## 3. 调研：已有输出契约横向对照

参考对象分两组：印刷时代的交换格式（hOCR / ALTO / PAGE / Tesseract TSV）和现代的文档模型（docling / MinerU），加上 Paddle 原生输出做基线。

| 方案 | 层级 | 阅读序 | 坐标 | 序列化 | 借鉴 / 避开 |
|---|---|---|---|---|---|
| hOCR | page → carea → par → line → word | DOM 序 | 引用图内像素 | HTML class 约定 | 层级干净；但表述靠 class 约定、无语义块类型 |
| ALTO XML | Page → PrintSpace → TextBlock → TextLine → String | 约定序 | 显式单位声明 | XML | 为典藏扫描而设，重；"测量单位显式"这点值得学 |
| PAGE XML | Page → Region → TextLine → Word | 显式 ReadingOrder | 引用图内像素 | XML | 阅读顺序显式表达值得学 |
| Tesseract TSV | 扁平表（level + 每级编号） | 隐含 | 像素 | TSV | 简单但丢层级语义，只适合速查 |
| docling DoclingDocument | body / furniture 双树 + items | 树序 | `BoundingBox{l,t,r,b,coord_origin}` | JSON（自带 schema 版本） | 最接近的现代参照：provenance（page_no + bbox + charspan）、pages[].size、页眉页脚单独成为 furniture |
| MinerU | content_list 平铺块 + middle.json 细粒度 | 列表序 | 块 bbox | JSON | 双档输出（粗用 / 细排障）的思路；表格 HTML、公式 LaTeX |
| PaddleOCR-VL 原生 | 页 → parsing_res_list 块 | block_order | 栅格像素 | res dict | 直接可用但绑实现：numpy、无尺寸字段、无版本元数据 |

归纳出五条设计输入：

1. 分层到"块"就够。VL 的识别粒度本来就是块，行 / 词级没有稳定来源，不做假想建模。
2. 阅读顺序用显式整数，允许为空。
3. 坐标必须带"空间声明 + 页尺寸"，否则消费方无法把框回投到 PDF 页面上。
4. 语义类型用小枚举 + 保留原生标签原文，这是后端换标签时契约不破的缓冲垫。
5. 文档级必须带来源与版本元数据——这是两端能各自升级的前提。

## 4. 提案：规范输出契约 OcrDocument（v1）

### 4.1 设计原则

- 够用最小：只到块级；只保留消费方会真正用到的字段（类型、内容、顺序、坐标、来源）。
- 可投影：纯文本、markdown 都是契约的投影函数，不是契约本身；消费方不直接吃后端原生的 markdown 产物。
- 可追溯：每个文档带后端 / 模型 / 管线版本与源文件哈希；原生 JSON 走 sidecar，不混进契约。
- 零 numpy、零特殊类型：契约必须能直接 `json.dumps`，和 storage / llm_client 一样，数据先落地成普通类型再流动。

### 4.2 数据模型（pydantic v2，已实现）

已实现于 `src/ocr_backend/contract.py`。pydantic 已显式加入核心依赖（`uv add pydantic`，不再只靠 fastapi 传递带入）；模型提供校验，还能导出 JSON Schema——正好拿来做契约快照测试。字段（与代码一致）：

```python
SCHEMA_VERSION = "1.0"

class BlockKind(StrEnum):
    title = "title"; paragraph = "paragraph"; table = "table"; formula = "formula"
    figure = "figure"; caption = "caption"; list_item = "list_item"
    header = "header"; footer = "footer"; page_number = "page_number"
    footnote = "footnote"; other = "other"

class ContentFormat(StrEnum):
    text = "text"; markdown = "markdown"; html = "html"; latex = "latex"

class OcrBlock(BaseModel):
    id: int                      # 后端块序号（Paddle block_id）
    kind: BlockKind              # 规范化类型（映射见 §4.3）
    raw_label: str               # 后端原始标签，原样保留
    content: str                 # 内容原文（正文文本 / 表格 HTML / 公式 LaTeX）
    content_format: ContentFormat
    bbox: tuple[float, float, float, float]   # x1,y1,x2,y2，页内坐标
    order: int | None            # 阅读顺序；后端不提供则为 None
    score: float | None          # 可用的质量分；Paddle 场景填版面分，识别无分

class OcrPage(BaseModel):
    page_index: int              # 0 基
    width: int                   # 本页栅格宽（像素，即本页坐标空间的界）
    height: int
    blocks: list[OcrBlock]

class OcrSource(BaseModel):
    kind: Literal["pdf", "image"]
    path: str
    sha256: str                  # 复用 storage.sha256_hex
    page_count: int

class OcrBackendInfo(BaseModel):
    name: str                    # "paddleocr_vl"
    library_version: str         # paddleocr 包版本
    model: str                   # "PaddleOCR-VL-1.6-0.9B"
    pipeline_version: str        # "v1.6"
    options: dict[str, Any]      # 本次生效的开关

class OcrDocument(BaseModel):
    schema_version: str = SCHEMA_VERSION
    coordinate_space: Literal["image_px_top_left"] = "image_px_top_left"
    source: OcrSource
    backend: OcrBackendInfo
    created_at: str              # ISO8601
    pages: list[OcrPage]
```

对应的 JSON 形态（单页片段）：

```json
{
  "schema_version": "1.0",
  "coordinate_space": "image_px_top_left",
  "source": {"kind": "pdf", "path": "docs/demo.pdf", "sha256": "…", "page_count": 3},
  "backend": {"name": "paddleocr_vl", "library_version": "3.7.0",
              "model": "PaddleOCR-VL-1.6-0.9B", "pipeline_version": "v1.6",
              "options": {"use_layout_detection": true}},
  "created_at": "2026-09-21T10:00:00+08:00",
  "pages": [{
    "page_index": 0, "width": 1191, "height": 1684,
    "blocks": [{"id": 1, "kind": "title", "raw_label": "paragraph_title",
                "content": "PaddleOCR-VL 1.6 Output Verification", "content_format": "text",
                "bbox": [95.0, 129.0, 825.0, 171.0], "order": 1, "score": 0.7884}]
  }]
}
```

投影函数（`render.py`，已实现）：

- `page_text(page) -> str`：块按阅读顺序拼接，`order` 为 `null` 的按后端列表位置垫后；跳过 header / footer / page_number（跳过集是函数参数，可覆盖）；
- `document_markdown(doc) -> str`：`doc_title` → `#`、其余 title → `##`（适配器已剥掉 `### ` 前缀），LaTeX 公式补 `$$` 包裹，表格 HTML 与正文直出——和 Paddle 自己的 markdown 输出解耦。

与 pdf_summarizer 现有模型的桥接：`Document` / `Page` 不动，需要时一行转换 `Document(pages=[Page(n, text=page_text(p)) ...])`；等新层稳定后再评估让 pdf_summarizer 直接消费 `OcrDocument`。

### 4.3 PaddleOCR-VL → 契约的映射（已实现，经真机校准）

| Paddle `block_label` | 契约 `kind` | 说明 |
|---|---|---|
| doc_title / paragraph_title | title | markdown 投影时按 raw_label 分级；`format_block_content=True` 时带的 `### ` 前缀由适配器剥掉 |
| text / vertical_text / abstract / reference / reference_content / content | paragraph | `content` 是 V3 的正文标签（旧文档里的 `contents` 不存在） |
| ocr | paragraph | 仅出现在 `use_layout_detection=False`：整页 markdown，`content_format=markdown` |
| table | table | content 为 `<table>` HTML 原样（`format_block_content=True` 时带内联样式），`content_format=html` |
| display_formula / inline_formula | formula | ` $$ … $$ ` / ` $ … $ ` 包裹由适配器剥掉，`content_format=latex` |
| image / chart / seal | figure | 图表、印章归图像类 |
| figure_title / vision_footnote | caption | 图表注记（V3 没有 table_title / chart_title） |
| formula_number | caption | 公式编号按注记处理 |
| header / header_image | header | raw_label 区分图像页眉 |
| footer / footer_image | footer | |
| number | page_number | |
| footnote | footnote | |
| algorithm / aside_text | other | v1 不细分；消费方可按 raw_label 分支 |
| 未知标签 | other | raw_label 原样保留，契约不因新标签而破 |

其余字段映射与归一化（都在适配器内，实测规则）：`block_bbox → bbox`（numpy 标量转 `float`）；`block_id → id`；`block_order → order`（保留 `null`）；`score` 取 `layout_det_res` 中与该块 bbox IoU 最大的检测框分数，匹配不到（或无 layout 模式）为 None——VL 识别本身没有置信度；图片输入 `page_index = null` 时按 predict 顺序回填；`width` / `height` 直接取 res。

### 4.4 关键取舍

- 块级为止：VL 的识别粒度就是块，不虚构行级。将来需要行级时以"新增字段"扩展（属兼容变更，见 §6），不是现在就建行级模型。
- 坐标用"像素 + 页尺寸"而不是归一化 [0,1]：Paddle 原生就是像素，无损；归一化对消费方只是一行除法，反推像素则要引入精度假设与尺寸猜测。坐标空间在文档级显式声明，未来支持 quad / poly 时加枚举值即可。实测页尺寸直接来自 res：PDF 由管线内 pypdfium2 按 2× 点尺寸栅格化（A4 → 1191×1684，≈144 DPI，环境变量 `PADDLE_PDX_PDF_RENDER_SCALE` 可覆盖），px → pt 换算只剩一行 `width_px / 页面点数宽`。
- `content_format` 判别而不在 v1 拆表格 / 公式内部结构：把 HTML 表拆成行列是消费方的职责，塞进契约只会把契约做大。
- score 可空、语义从宽：契约只承诺"后端能给的质量分"，并注明 Paddle 场景下是版面检测分，避免被误当识别置信度。
- 原生 JSON 走 sidecar：适配器提供 `raw_dir` 选项，设置后逐页调 Paddle 自带的 `save_to_json`；契约本身不吸收 numpy，也不背保真包袱。升级排查靠 sidecar + golden 样本。
- markdown 渲染归我们：Paddle 的 `markdown_ignore_labels` 默认丢七类块，我们只要保留全部块、渲染时再决定丢什么。

## 5. 薄后端接口与包结构（已实现）

### 5.1 接口（已实现：一个 Protocol、一个方法）

`OcrBackend` Protocol 在 `backends/__init__.py`（`runtime_checkable`，便于测试里 `isinstance` 断言）；适配器对外只有 `.name` / `.capabilities` / `.parse()` / `.close()`：

```python
@runtime_checkable
class OcrBackend(Protocol):            # backends/__init__.py
    name: str                          # "paddleocr_vl"
    capabilities: frozenset[str]       # {"block_bbox","reading_order","table_html","formula_latex"}
                                       # 无 layout 检测时降级为空集
    def parse(self, source: str | Path) -> OcrDocument: ...

@dataclass
class PaddleOCRVLConfig:               # 只暴露常用开关，其余透传 extra_params
    pipeline_version: str = "v1.6"
    device: str | None = None
    model_dir: str | Path | None = None    # 本地 HF 快照 → vl_rec_model_dir
    use_layout_detection: bool = True
    use_doc_orientation_classify: bool = False
    use_doc_unwarping: bool = False
    use_chart_recognition: bool = False
    use_seal_recognition: bool = False
    use_ocr_for_image_block: bool = False
    format_block_content: bool = False
    vl_rec_backend: str | None = None      # 接 vLLM 服务用（暂缓，见 §7）
    vl_rec_server_url: str | None = None
    raw_dir: Path | None = None            # 存 sidecar 原生 JSON（无 res 外壳）
    extra_params: dict[str, Any] = field(default_factory=dict)

class PaddleOCRVLBackend:              # 懒加载、可 close
    name = "paddleocr_vl"
    @property
    def capabilities(self) -> frozenset[str]: ...   # use_layout_detection=False → frozenset()
    def parse(self, source: str | Path) -> OcrDocument: ...
    def close(self) -> None: ...       # 释放 pipeline（显存占用大，长驻进程用完可关）
```

实现要点（与草案的差异）：`PaddleOCRVL(...)` 构造即加载模型（实测 3.6s 起），所以后端懒加载——`__init__` 不建 pipeline，首次 `parse` 才建、之后复用；`capabilities` 做成属性，随配置在空集与四项之间切换；`map_page(res, …)` 抽成独立纯函数，golden 测试直接喂 sidecar JSON，不加载模型。

刻意不做的：不做 ABC 继承体系、不做 async（消费方需要时 `asyncio.to_thread` 一行包住）、不做后端池 / 实例管理。capabilities 只声明消费方会真正分支的项。

### 5.2 包结构

```
src/ocr_backend/
  __init__.py            # 契约与投影的导出、用法示例、"什么该进 / 不该进"
  contract.py            # OcrDocument 与枚举、SCHEMA_VERSION
  render.py              # page_text / document_markdown
  backends/
    __init__.py          # OcrBackend Protocol（runtime_checkable）
    paddleocr_vl.py      # 适配器：原生 res → OcrDocument；配置；懒加载
tests/test_ocr_backend/
  test_contract.py       # JSON roundtrip + JSON Schema 快照
  test_render.py         # 两个投影函数的行为
  test_paddle_mapping.py # 吃 golden 样本（真机 res JSON），不加载模型
  test_paddleocr_vl_live.py  # OCR_LIVE=1 才跑的真机端到端测试（默认跳过）
  fixtures/              # 真机跑出的 golden res JSON + ocr_document.schema.json 快照
```

- pyproject：`packages` 已登记 `src/ocr_backend` 并重装 editable（仓库既有惯例）；`pydantic` 已显式加入核心依赖（契约层自用，此前只是 fastapi 的传递依赖）；OCR 重依赖拆成可独立安装的三个 extras——前端栈 `ocr` + 引擎 `paddle-cpu` / `paddle-gpu`（GPU 引擎由 pyproject 声明的 Paddle 官方源解析，见 §7）。
- 与 pdf_summarizer `extractor/` 的关系：那套是项目内的提取策略。新层落地后 `paddle_ocr.py` 整体删除，extractor 里换成一个调用 `ocr_backend` 的薄后端；这一步属于迁移阶段（§7 第 3 步），不在本提案范围。

### 5.3 真机验证清单与结论（已完成）

环境：paddleocr 3.7.0 / paddlepaddle-gpu 3.3.1（cu126）/ RTX 3070 Ti Laptop 8GB；素材与全部产物在 `data/ocr_backend/verify/`（gitignored），复跑脚本 `shell_scipts/verify_paddle_vl_16.py`。

1. **页面栅格宽高从哪来** → res 自带 `width` / `height`，无需自管栅格化。PDF 由管线内 pypdfium2 按 2× 点尺寸渲染（A4 595.3pt → 1191×1684，≈144 DPI）；`PADDLE_PDX_PDF_RENDER_SCALE` 可覆盖；图片输入等于图片原生像素（150dpi PNG = 1241×1754）。px → pt 换算系数 = `width_px / 页面点数宽`。
2. **`block_order` 形态** → 主阅读流上 1 起连续整数；页面附属 / 独立元素（header、footer、number、table、figure_title、chart、image）为 `null`；`block_id` 是列表内 0 起序号。警告：`layout_det_res.boxes` 另有独立 `order` 字段，与 `block_order` 无关（实测同一 `number` 框：box.order=4，而对应块的 block_order=null）。
3. **表格 / 公式包裹形态** → 表格 content 是 `<table>` HTML（`format_block_content=True` 时带 `border=1 style=…` 内联样式）；`display_formula` 形如 ` $$ … $$ `（前后带空格）；`inline_formula` 以 ` $ … $ ` 内嵌在段落文本里。`format_block_content=True` 还会给标题加 `### ` 前缀（如 `### PaddleOCR-VL 1.6 Output Verification`）。适配器统一剥离包裹符与标题前缀（§4.3）。
4. **`use_layout_detection=False` 输出形态** → 每页一个 `block_label="ocr"` 的整页块：bbox `[0,0,W,H]`、`block_order=1`、content 为整页 markdown；`layout_det_res` 键整体缺失；图片输入 `page_index` / `page_count` 为 `null`。适配器把该块映射为 markdown 段落、页序按 predict 顺序回填、`capabilities` 降级为空集。
5. **空白页 / 纯图页形态** → 页面级 key 齐全：空白页 `boxes=[]` 且 `parsing_res_list=[]`（0 块）；纯图页 1 个 `image` 块、content 为空串。适配器对缺 key 一律兜底，不炸。
6. **耗时 / 显存基线（本机 GPU）** → 构造 pipeline 3.6–14.7s（含首次加载）；5 页 A4 文档冷跑 41.7s、热跑 16.7s（≈3.3s/页）；显存 allocated 3.08 GiB、峰值 5.93 GiB。给消费方的默认策略：复用后端实例、串行逐页；8GB 卡单实例够用。

## 6. 版本与同步升级策略

- `schema_version` 用单字符串（"1.0"）：加字段、加枚举值属于兼容变更，不动版本号；改字段语义或删字段升 major，并在本文更新映射表。
- 消费方分支看 `capabilities` 和 `kind`，不看后端名字，更不看后端版本号——Paddle 换代不需要动消费方。
- 每个产出的文档里都记录后端元数据：`paddleocr` 包版本、模型名、`pipeline_version`、生效开关。1.6 → 1.7 换代时，两份文档可以直接 diff。
- 升级流程：新模型 → 真机跑出新 golden（入 fixtures）→ 映射测试看 diff → 未识别标签补映射（或落 other）→ 需要就 bump schema → 更新本文。
- 契约测试全部不加载模型（JSON roundtrip + Schema 快照 + golden 映射），CI 可跑；真机测试按仓库惯例用环境变量开关跳过（参照 `test_summarize_live`）。golden 样本存 `tests/test_ocr_backend/fixtures/`，升级流程里的"旧样本 diff"直接跑映射测试即可看到。

## 7. 后续步骤与已决事项

落地顺序（每步独立验收）：

1. 跑真机验证清单（§5.3），把假设钉死 —— **已完成**（结论见 §5.3，原始产物在 `data/ocr_backend/verify/`）；
2. 实现 `src/ocr_backend/`（contract + render + paddleocr_vl + 三类测试），登记 pyproject 并重装 —— **已完成**（代码与测试在 `src/ocr_backend/`、`tests/test_ocr_backend/`；pyproject 已登记并重装）；
3. pdf_summarizer 迁移试验：删旧 `paddle_ocr.py`，extractor 接新层，用一张扫描件跑端到端 —— 下一步；
4. （按需）file_manager 接入：用块级契约做"搜索命中定位到页 / 框"。

已决：

- 包名：`ocr_backend`（与 llm_client / storage 的角色命名一致）；
- pydantic v2：已显式加入核心依赖（`uv add pydantic`，非 optional extra）——校验 + JSON Schema 快照两用；
- 页面栅格尺寸：直接用 res 自带的 `width` / `height`（§5.3 第 1 条），不做自栅格化；换算 PDF 坐标用 `width_px / page_pt_width` 折算；
- vLLM 服务形态：暂缓——**接入时机是"本地推理慢到不可用"（典型如 CPU 档），届时再上**。本机 GPU 热跑 ≈3.3s/页、显存峰值 5.93 GiB，够用；配置层已留 `vl_rec_backend` / `vl_rec_server_url`，切换时只改适配器构造参数，`OcrDocument` 契约与消费方不动。
- OCR 依赖拆分（原 `ocr-gpu` 待定项的解）：extras 拆成可独立安装的三块——`ocr`（前端栈）+ `paddle-cpu` / `paddle-gpu`（引擎）。GPU 引擎不再走 PyPI（那里停在 2.6.2、与 paddlex 3.x 不兼容），而是由 pyproject 声明的 Paddle 官方 cu126 源解析（`[[tool.uv.index]]` + `[tool.uv.sources]`）；实测 `uv lock` 直接选出 `paddlepaddle-gpu 3.3.1`（官方源是 flat 页、wheel 直链无 hash，uv 兼容）。组合用法：`uv sync --extra ocr --extra paddle-gpu`。换 CUDA 通道改 index URL；特殊环境仍可退回手装：`uv pip install paddlepaddle-gpu==3.3.1 --index-url https://www.paddlepaddle.org.cn/packages/stable/cu126/`。

## 参考资料

- PaddleOCR-VL 管线文档（官方）：<https://www.paddleocr.ai/latest/en/version3.x/pipeline_usage/PaddleOCR-VL.html>
- PaddleOCR-VL-1.6 模型卡与 README：<https://huggingface.co/PaddlePaddle/PaddleOCR-VL-1.6>（技术报告：<https://arxiv.org/abs/2606.03264>）
- PaddleOCR Releases（3.6.0 / 3.7.0 时间线）：<https://github.com/PaddlePaddle/PaddleOCR/releases>
- 识别无置信度讨论（issue #16899）：<https://github.com/PaddlePaddle/PaddleOCR/issues/16899>
- 版面分析模块文档（标签集）：<https://www.paddleocr.ai/latest/version3.x/module_usage/layout_analysis.html>
- docling DoclingDocument 概念与参考：<https://docling-project.github.io/docling/concepts/docling_document/>
- MinerU 输出文件格式：<https://opendatalab.github.io/MinerU/reference/output_files/>
- hOCR 规范：<https://kba.cloud/hocr-spec/1.2/>；ALTO（LOC）：<https://www.loc.gov/standards/alto/>；PAGE XML：<https://www.primaresearch.org/schema/PAGE/gts/pagecontent/2019-07-15/>
