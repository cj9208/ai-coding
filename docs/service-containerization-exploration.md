# 服务 Docker 化探索：从 OCR 开始

> 一句话核心：先把 OCR 做成一个可挂目录、可复现运行的 **containerized runner**，而不是立刻拆成常驻 HTTP 服务；CPU / GPU 用独立镜像和 Compose profile 隔离，等确有多个调用方、排队或并发需求时，再在稳定的 `OcrDocument` 契约外包一层 API。

日期：2026-09-21。状态：OCR 后端已完成（契约 + PaddleOCR-VL 适配器 + 真机验证），`ocr-backend parse` 子命令和 `docker/ocr/`（CPU/GPU Dockerfile + Compose profile）已按本文落地；**尚未**在装有 Docker 的机器上跑过一次真实构建和端到端验证。本文仍是“为什么这样起步”的决策记录，不是实现说明书；OCR 的输出契约和 PaddleOCR-VL 选型见 [ocr-backend-design.md](ocr-backend-design.md)。

读法：只想跑起来 → §1.1；为什么是 runner 而不是常驻服务 → §3；要上 GPU 先读 §5；还差哪些验证 → §8。

## 1. 结论与阅读地图

最省事的路径不是把仓库全部塞进一个 Compose，也不是一开始就引入队列、鉴权和 Kubernetes，而是分三层演进：

1. **现在：OCR runner 容器。** 以一个 PDF / 图片输入、一个 `OcrDocument` 加 sidecar 输出为单位运行。它解决依赖、Poppler、CPU/GPU 运行时和模型缓存的可复现性。
2. **以后：OCR HTTP 服务。** 只有 file manager、pdf summarizer 或外部程序需要共享一张 GPU、需要进度查询或并发隔离时才增加。服务内部仍调用同一个 runner / `ocr_backend`，不改变契约。
3. **更后：作业队列。** 仅当单次处理明显超时、需要重试/限流，或必须异步处理时引入。它不是容器化的前置条件。

`src/ocr_backend/` 的契约、渲染层和 PaddleOCR-VL 适配器均已实现并通过真机验证；`ocr-backend parse` 就是这个"可被命令行调用、可测试的入口"，`docker/ocr/` 的 Dockerfile 和 Compose 也已按它封装完成。下一步不是继续写代码，而是在装有 Docker Desktop 的机器上跑一次真实构建（见 §8）。

### 1.1 使用指南：三步上手

前提：装好 Docker Desktop（Windows 用 WSL2 backend；GPU 另需宿主机 NVIDIA 驱动，见 §5.1）。代码侧只需 `uv sync`——宿主机**不需要**装 Paddle、Poppler 或任何 OCR extras，这些都封在镜像里。

```bash
# 1) 构建 runner 镜像
ocr-backend container build

# 2) 模型快照在容器里下载，落到宿主机 data/ocr_backend/models/（宿主机不碰 HuggingFace）
ocr-backend container download paddleocr-vl-1.6

# 3) 待识别文件放进 data/ocr_backend/in/（目录不存在时首次运行会自动创建），跑一次
ocr-backend container parse data/ocr_backend/in/scan.pdf
```

产物在宿主机 `data/ocr_backend/out/`：`<stem>.ocr.json` 是 `OcrDocument` 契约，`<stem>.ocr.md` 是 markdown 投影；容器 `--rm` 退出后结果仍在。

常用变体：

| 想做的事 | 怎么做 |
|---|---|
| 用 GPU | 三条命令都加 `--gpu`（同时切换 profile、服务名和默认 `--device gpu`） |
| 结果分目录 | `--out data/ocr_backend/out/batch1`——必须是 `out/` 下的子目录，容器只看得见这块挂载 |
| 报错 `not under .../in` | 把文件挪进 `data/ocr_backend/in/`；那是唯一挂进容器的输入目录（见 `src/ocr_backend/container.py`） |
| 调试要看引擎原生输出 | 直接用原始 Compose 命令加 `--raw-dir`（§3.1），wrapper 暂未透传该参数 |
| 本机已有 OCR 环境，不想过容器 | `uv sync --extra ocr --extra paddle-cpu` 后直接 `ocr-backend parse <file> --out <dir>` |

第一次 `build` 会慢（Paddle CPU wheel 约 190 MB），但依赖层与代码层分开缓存，之后改 `src/` 不需要重装依赖。注意：这三条命令在真实 Docker 上还没跑过——§8 的第 3、4 项验收仍待有 Docker Desktop 的机器执行，本文不为它们背书。

## 2. 现状：适合容器化的是什么

仓库是多个独立 Python 子项目，不是一个单体 Web 应用。`file_manager` 已是 FastAPI + Uvicorn 服务；`pdf_summarizer` 的 OCR 提取后端已换成 `ocr_backend` 的 `PaddleOCRVLBackend`（`src/pdf_summarizer/extractor/paddle_vl.py`），旧的 PaddleOCR 2.x 直连实现已删除。`ocr_backend` 同时暴露 `ocr-backend parse` CLI。至此，容器化要封的是这个 CLI，不是一段只能在 Python 里调用的库代码。仍然没有的：OCR API、后台任务、以及在真实 Docker 环境跑过的构建记录。

```
现在

PDF ──> pdf-summarize CLI ──> PyMuPDF（默认）
                         └─> ocr_backend.PaddleOCRVLBackend（可选、进程内）

本次落地

PDF / 图片 ──> ocr-backend parse（同一套 ocr_backend）──> OcrDocument JSON + Markdown
                     │
                     └─> docker/ocr：一次性 runner（CPU / GPU profile）

按需才增加

file_manager / pdf_summarizer / 外部调用方 ──HTTP──> OCR API ──> 同一 runner
```

这带来两个直接判断：

- **OCR 应独立容器化，不能和 file manager 绑成一个进程或镜像。** 前者有沉重且平台敏感的 Paddle 依赖、模型和可选 GPU；后者是轻量 Web 应用与 SQLite。两者发布和扩缩容的节奏不同。
- **输入、输出和模型都不应写进镜像层。** 输入输出是用户数据；模型体积大、更新频率独立。镜像只封装代码、系统库和精确锁定的 Python 依赖。

现有 `ocr` / `ocr-gpu` optional extras 已将重量依赖与核心安装隔离；CPU 路线依赖 `paddlepaddle`，GPU 路线依赖 `paddlepaddle-gpu`，并已有 x86_64 与平台限制。这是镜像分层的自然边界，而不是要重新设计一套依赖管理。

## 3. 推荐的最小形态：一次性 runner，而非常驻服务

### 3.1 运行协议

`ocr-backend parse` 就是这个稳定的文件协议，直接调 Compose 的原始形态是：

```text
docker compose -f docker/ocr/compose.yaml --profile cpu run --rm \
  ocr-cpu parse /work/in/contract.pdf --out /work/out --device cpu
```

这条命令里有四处必须每次敲对——compose 文件路径、profile、服务名、以及宿主机路径到容器挂载点（`/work/in`、`/work/out`）的换算——敲错任何一处都是静默失败（跑错镜像、文件在挂载范围外容器看不见）。`ocr-backend container parse data/ocr_backend/in/contract.pdf`（`src/ocr_backend/container.py`）把这几处全部从 `REPO_ROOT` 推导出来，输入文件不在 `data/ocr_backend/in/` 下会直接报错而不是交给 Docker 悄悄失败；`container build` / `container download` 同理。

容器约定只做三件事：从只读 `/work/in` 取文件；将 `OcrDocument` JSON（`<stem>.ocr.json`）、markdown 投影（`<stem>.ocr.md`）和可选的原生结果 sidecar（`--raw-dir`）写入 `/work/out`；用进程退出码表达成功或失败。宿主机将这两个路径分别映射到：

```text
data/ocr_backend/in/       输入（宿主机拥有）
data/ocr_backend/out/      结果与诊断（宿主机拥有）
```

这与仓库既有的 `data/<project>/` 约定一致，并避免 Docker volume 与应用默认工作目录混在一起。输入挂载应为只读；输出目录必须是显式挂载，不能依赖容器可写层，否则 `--rm` 后结果会消失。

### 3.2 为什么 runner 是当前的最佳边界

runner 有足够的隔离能力：同一条命令在开发者机器、CI 或 GPU 主机上都运行相同镜像，宿主机不需要安装 Poppler、Paddle 或 Python OCR 依赖。它也不虚构目前尚不存在的需求：没有调用方就没有 API 版本、认证、任务状态、超时语义和长期进程的模型生命周期问题。

更重要的是，runner 的业务输入输出就是未来 API 的内核。将来若需要 API，`POST /jobs` 只负责接收文件与创建作业；真正执行仍调用相同的 `parse(source) -> OcrDocument` 路径。这样容器化不会反过来决定领域契约。

## 4. 镜像与 Compose 的建议结构

已落地的目录结构：

```text
docker/
  ocr/
    Dockerfile.cpu           # Python 3.12 + Poppler + uv locked CPU 依赖
    Dockerfile.gpu           # 单独经真机验证的 CUDA/Paddle GPU 组合
    compose.yaml             # ocr-cpu / ocr-gpu 两个 runner service
.dockerignore                # 放在构建上下文根目录，排除 .venv、data、模型、缓存、.git
```

`.dockerignore` 必须位于构建上下文根目录（这里是仓库根），不是 `docker/ocr/` 里面——Compose 的 `build.context` 指向仓库根，Docker 只在该根查找这一个文件。

`compose.yaml` 有两个同名语义不同的服务：`ocr-cpu` 和 `ocr-gpu`。两者共享同样的输入、输出和命令约定，但采用不同 Dockerfile 和 profile：

```yaml
services:
  ocr-cpu:
    profiles: [cpu]
    build:
      context: ../..
      dockerfile: docker/ocr/Dockerfile.cpu
    image: ai-coding-ocr-cpu:latest
    volumes:
      - ../../data/ocr_backend/models:/app/data/ocr_backend/models
      - ../../data/ocr_backend/in:/work/in:ro
      - ../../data/ocr_backend/out:/work/out

  ocr-gpu:
    profiles: [gpu]
    build:
      context: ../..
      dockerfile: docker/ocr/Dockerfile.gpu
    image: ai-coding-ocr-gpu:latest
    volumes:
      - ../../data/ocr_backend/models:/app/data/ocr_backend/models
      - ../../data/ocr_backend/in:/work/in:ro
      - ../../data/ocr_backend/out:/work/out
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
```

模型挂载点是容器内的 `/app/data/ocr_backend/models`——正好等于 `utils.paths.data_dir("ocr_backend")` 在容器里的解析结果（`REPO_ROOT` = `/app`），所以 `ocr-backend download` 和适配器默认的快照查找都能直接命中宿主机那份，不用额外传 `--model-dir`。

关键点不是文件名，而是 CPU 与 GPU **不通过一个 Dockerfile 的条件分支混装**。GPU 镜像要同时匹配 Paddle 轮子、CUDA runtime 与宿主机 NVIDIA runtime；独立文件会使失败边界和锁定策略清楚得多。

Compose profile 可让默认工作流不触碰 GPU；`--profile cpu` 或 `--profile gpu` 显式选中运行形态。GPU 设备保留配置中 `capabilities: [gpu]` 是必填项，`count` 与 `device_ids` 不能同时使用。

## 5. CPU、GPU、模型的具体取舍

| 事项 | CPU runner（第一步） | GPU runner（第二步） |
|---|---|---|
| 目标 | 可复现、排除系统依赖、验证文件协议 | 缩短批处理时间、共享 GPU |
| 适用 | 开发、功能验证、少量文件 | 大 PDF、批处理、多人/多进程使用 |
| 镜像 | Linux `amd64` 的 Python 3.12 + Poppler + `uv sync --locked --extra ocr` | 单独锁定的 Linux `amd64` CUDA/Paddle/PaddleOCR 组合 |
| 模型 | 首次下载或显式挂载的本地模型 | 同样的模型版本，但必须真机记录显存与加载时间 |
| 默认性 | 默认、必须有 | 可选，不能成为本地开发前提 |

### 5.1 宿主机与 GPU 前提

- 在 Windows 上，GPU 容器依赖 Docker Desktop 的 WSL2 backend、NVIDIA GPU、当前驱动与 WSL2 内核；驱动装在 Windows 宿主机，不在 Linux 容器/WSL 发行版中重复安装。
- 在 macOS 上，先将 CPU runner 作为唯一受支持路线；不要承诺 Apple GPU 路径。现有 OCR 设计也将 Docker 视为 macOS 的可行运行方式。
- 无论 CPU 或 GPU，首版统一以 Linux `amd64` 为目标。其他架构不应在没有 Paddle 真机验证时写入支持矩阵。

### 5.2 模型与缓存

模型不能进入 Git，也不建议 bake 进镜像。推荐优先级如下：

1. **开发/复现：显式只读挂载模型目录。** 例如将已下载的 `paddleocr-vl-1.6/` 挂到 `/models/paddleocr-vl-1.6:ro`，并由未来的 `OCR_MODEL_DIR` 配置指向它。优点是版本、来源和磁盘占用对人可见。
2. **便利运行：具名 cache volume。** 允许框架首次下载，再复用缓存；必须在 `docker compose down -v` 的文档中明确它会清缓存。
3. **CI：预热独立 cache 或使用固定小型测试材料。** 不应在普通单元测试中下载大模型或要求 GPU。

目前 PaddleOCR-VL 1.6 本机模型的精确传参方式仍要以真机验证为准。因此不要在实现前把模型路径写死到 Dockerfile 或 Compose 环境变量；先验证 `PaddleOCRVL` 对本地快照的配置入口。

## 6. Dockerfile 的构建原则

CPU Dockerfile 的职责是将当前可选环境重放出来，而不是绕开 `uv.lock` 重新解析依赖：

1. 基于固定的 Python 3.12 Linux `amd64` 基础镜像；安装 `poppler-utils`，以兼容当前 `pdf2image` 路径。
2. 先复制 `pyproject.toml` 与 `uv.lock`，再执行 `uv sync --locked --extra ocr`；最后才复制 `src/`。这样代码修改不会使重量依赖层失去缓存。
3. `.dockerignore` 排除 `.venv/`、`data/`、`paddleocr-vl-1.6/`、Python cache、Git 元数据和本地 `.env`。模型和用户文件不得进入 build context。
4. 生产镜像不挂源码、也不以 editable 安装覆盖镜像代码；开发调试可以另用 compose override 挂源码，但不能把它当发布形态。

GPU Dockerfile 不应简单地把 `--extra ocr` 替换成 `--extra ocr-gpu` 后视为完成。它需要在实际 GPU 主机上确认 CUDA、Paddle wheel、`nvidia-container-toolkit`、模型加载和显存峰值的组合，然后将**验证过的版本组合**写入注释/锁文件和验收记录。

## 7. 何时从 runner 升级为服务

以下信号满足任意两个，再增加 OCR HTTP 服务比较合理：

- `file_manager` 与 `pdf_summarizer` 都需要 OCR，且不希望各自加载一份模型；
- 单次任务足够慢，需要提交后查询进度，而不是等待 CLI；
- 需要限制并发，防止多次 GPU 推理互相抢显存；
- 需要在一台专门 GPU 主机上给多个可信调用方提供 OCR。

服务的第一版保持薄：`POST /jobs`、`GET /jobs/{id}`、`GET /healthz`。`/healthz` 应区分“进程活着”和“模型已可用”；任务的最终产物仍是版本化 `OcrDocument`。不要在第一版加入多租户、外部公开鉴权、通用工作流引擎或消息队列。

到那时，推荐的内部结构是：

```text
调用方 ──> ocr-api（接收、状态、限流） ──> ocr-worker（单一模型进程）
                                           └─> data/ocr_backend/out/<job-id>/
```

SQLite 可以先记录任务元数据；队列只在执行时间、失败重试或并发控制确实无法用单进程解决时再引入。file manager 不要反向持有 OCR 模型或直接访问 GPU。

## 8. 验收清单与当前进度

| # | 事项 | 状态 |
|---|---|---|
| 1 | 跑 `shell_scipts/verify_paddle_vl_16.py`，核验页面尺寸、空页、表格/公式输出、CPU/GPU 耗时与内存 | 已完成（适配器 docstring 记录了 2026-09-21 真机结论） |
| 2 | 在 `OcrDocument` / 渲染层上补齐 Paddle 适配器、映射测试和 `ocr-backend parse` CLI；单元测试不加载模型 | 已完成（`tests/test_ocr_backend/`，`fake_backend` 不触碰 paddleocr） |
| 3 | 在 Linux CPU 容器内跑通一次：输入只读、输出持久、无文字页失败语义、JSON 可解析、`--rm` 后产物仍在 | **未验证**——当前 shell 里没有 Docker CLI，镜像尚未构建过一次 |
| 4 | 在 Windows + WSL2 + NVIDIA GPU 主机上验证 GPU 镜像；记录加载时长、每页耗时、显存峰值，确认未静默退回 CPU | **未验证** |
| 5 | 前四项稳定后，才评估是否有足够调用方需要 HTTP 服务 | 未开始，也不应在此之前开始 |

3、4 两条需要在装有 Docker Desktop 的机器上手动执行，最省事的入口是 `ocr-backend container build` / `ocr-backend container download paddleocr-vl-1.6` / `ocr-backend container parse <文件>`（GPU 加 `--gpu`）；原始 Compose 命令见 `docker/ocr/compose.yaml` 顶部注释。本机不能替它们背书。

## 9. 决策记录

| 决策 | 结论 | 原因 |
|---|---|---|
| 起步形态 | 一次性 OCR runner | 先解决重依赖可复现，不制造尚不存在的 API/任务管理问题 |
| 编排工具 | Docker Compose | 本地单机 CPU/GPU 选择已经足够，profile 可明确区分运行形态 |
| CPU/GPU | 两个独立镜像 | 依赖和失败模式不同，避免“一个镜像到处跑”的隐性不兼容 |
| 持久化 | 显式宿主机挂载 `data/ocr_backend/` | 保持仓库数据目录约定，容器删除后仍可检查结果 |
| 模型 | 先挂载或缓存，不进镜像 | 模型体积大、更新独立，也不应污染构建上下文 |
| 重复操作封装 | `ocr-backend container ...`（Python 子命令，不是 bash 脚本） | compose 路径、profile、服务名、挂载路径映射只需推导一次；跨 Windows/Linux 通用，且能用假 `subprocess.run` 做单元测试，不要求真装 Docker |
| 服务化时机 | 有共享、并发或异步的真实需求后 | 容器化和服务化是两件事，前者不需要以后者为前提 |

## 10. 参考资料

- 仓库事实：[OCR 后端层设计](ocr-backend-design.md)、[项目依赖与 OCR extras](../pyproject.toml)、[pdf_summarizer 的 OCR 桥接](../src/pdf_summarizer/extractor/paddle_vl.py)、[Docker 配置](../docker/ocr/compose.yaml)、[容器命令封装](../src/ocr_backend/container.py)、[PaddleOCR-VL 真机验证脚本](../shell_scipts/verify_paddle_vl_16.py)。
- Docker 官方：[Compose profiles](https://docs.docker.com/compose/how-tos/profiles/)、[Compose GPU support](https://docs.docker.com/compose/how-tos/gpu-support/)、[Windows Docker Desktop GPU 前提](https://docs.docker.com/desktop/features/gpu/)、[构建缓存优化](https://docs.docker.com/build/cache/optimize/)。
- PaddleOCR 官方：[3.x 安装说明](https://www.paddleocr.ai/main/en/version3.x/installation.html)、[PaddleOCR-VL 使用说明](https://paddlepaddle.github.io/PaddleOCR/main/en/version3.x/pipeline_usage/PaddleOCR-VL.html)。
