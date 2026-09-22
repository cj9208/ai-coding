# 通知共享层（notify）设计

> 一句话核心：任务只往**本地账本**记结构化事件，**投递由独立的一次性分发器完成**——运行状态与 alert 是同一条通道上的两种节奏，渠道是可插拔适配器，"谁来监督监控者"的递归止于**不同失败域的叠加**而非无限套娃。

日期：2026-09-22。状态：**M0 shipped 2026-09-22**（config / events / ledger / channels(stdout) / cli，16 测试绿，真机 CLI 冒烟过；`uv run notify` 的 editable 重装与 M1 Telegram 真机为欠账，记 TODO.md）。读法：§1–§2 是需求与已定裁决（背景）；§3–§4 是实现契约（开工后以本节为准）；§5 是里程碑与验收标准；§6–§8 是风险、依赖与待定项。

## 1. 背景与目标

原始诉求（owner，2026-09-22 讨论）：像 `quant record` 这类要长期运行的任务，程序必然会有崩溃或静默的一天，而"不可能时时刻刻盯着"——所以缺的不是监控告警的一个实现，而是一条**长期存在的信息渠道**：日常看得见运行状态，出事时必须主动找到人。

业界通行的三条经验被本设计采纳：

1. **监控者独立于被监控者**——任务代码崩溃时，报警代码不能同归于尽；
2. **推式心跳（dead man's switch）优于拉式检查**——最阴险的故障是"进程活着但什么都不流"（本机 fstream WS 静默即是实例），拉式探测（进程在、端口通）对此全绿；
3. **通知不去重就会被屏蔽，被屏蔽的渠道等于不存在**——节流/聚合是渠道存续的前提。

目标：

- 一个共享层包（与 `llm_client`、`storage` 同级），任何任务两行代码即可上报事件；
- 账本即事实源：`notify status` 随时可回答"某流昨天/上周活没活、缺不缺数"；
- 两级消息：**digest**（日摘要，看状态）与 **alert**（分钟级，出事必须到我手机）；
- 渠道可插拔：第一个真实渠道 Telegram bot，接口同形地容纳 ntfy/企微/邮件的后来者。

非目标（刻意排除）：

- **不做常驻监控 daemon**——dispatch 是被计划任务拉起的短进程，常驻服务把"监控者挂了没人知道"从逻辑问题升级成运维问题；
- **不做升级链/on-call**（PagerDuty 的 ack 超时转人）——个人量级没有第二个人；
- **不租 VPS 做外部监督**——除非任务开始碰真钱（§4.6 触发条件）；
- **不替任务做重启**——重启是进程管理器（systemd/计划任务/`restart:`）的职责，notify 只负责让失败被看见。

## 2. 已定裁决（owner 拍板，勿在实现中重新推导）

| # | 裁决 | 影响 |
|---|---|---|
| D-1 | 做**共享层**而非 quantdesk 局部功能："后期很多任务都要有这个" | `src/notify/`，任务侧只有一个事件 API 门面 |
| D-2 | **alert 优先于 digest**（"alert 甚至反而是更重要的"） | dispatch 里 alert 最高优先、绕过节流；状态摘要可以让路 |
| D-3 | 首个真实渠道 **Telegram bot**；个人企微嫌注册烦、WhatsApp 官方只有 Business API（非官方库有封号风险）已否 | 适配器接口 + telegram/stdout 两实现；机器侧代理条件见 §6.1 |
| D-4 | 接受**四层失败域**作为"谁来监督监控者"的终点，L3 依赖第三方基础设施，其不可达时明示极限而非加层 | §4.6；ping 适配器与 telegram 适配器同形，L3 近乎免费 |
| D-5 | 账本 SQLite、repo-root 锚定（`data/notify/`），复用 `src/storage` | 投递是账本状态的推进，不是旁路副作用 |
| D-6 | quantdesk 的 `gaps.log` 改为账本的**渲染投影**，recorder 记事件 | 同 ocr_backend"契约 + 投影响应"姿势；silence-is-a-gap 落进通用层 |

## 3. 总体架构

```
 任务进程(quant record / 后期任何任务)          计划任务(每 ~5 min)      每天一次
 ┌──────────────────────────────┐            ┌────────────────┐   ┌──────────────┐
 │ notify.emit(project,kind,…)  │            │ notify dispatch│   │ notify digest│
 │  同步·本地·无网络·永不抛异常 │            │  读未决→节流   │   │  聚合渲染    │
 └──────────────┬───────────────┘            │  →逐渠道投递   │   │  +规则评估   │
                │ INSERT                     └───────┬────────┘   └──────┬───────┘
 ┌──────────────▼───────────────────────┐           │ 标记投递结果      │ 缺心跳→
 │ data/notify/events.db  (账本=事实源) │◄──────────┘                   │ 生成 alert
 │  events + deliveries 两张表          │◄──────────────────────────────┘
 └──────────────────────────┬───────────┘
                             │ 只读
              ┌──────────────▼───────────────┐
              │ 渠道适配器（同一形状：往一个  │
              │  URL 发一个 HTTP 请求）      │
              │  stdout ─ telegram ─ ping ─ …│
              └──────┬───────────┬───────────┘
                     ▼           ▼
                  终端      Telegram → 手机 push
                            (ping = L3 外部心跳，见 §4.6)
```

模块划分（tests 按 `tests/test_notify/test_<module>.py` 镜像）：

| 模块 | 职责 |
|---|---|
| `config.py` | DB 路径（`utils.paths.data_dir("notify")`，env `NOTIFY_DATA_DIR` 覆盖）、渠道与规则配置装载、`.env` 约定（§4.7） |
| `events.py` | 事件契约：`Event` dataclass、severity 枚举、`dedup_key` 派生规则；**任务侧唯一门面** `emit()` |
| `ledger.py` | SQLite 两张表的读写（复用 `storage.sqlite` 的 engine/PRAGMA），投递状态推进 |
| `rules.py` | 期望规则评估：给定账本 + `expectations.yaml`，产出应生成而未生成的 alert 事件（§4.5） |
| `policy.py` | 纯函数：一批待发事件 → 节流/合并/优先级排序后的投递计划（§4.4），规则改动过 golden |
| `channels/` | `base.py` 适配器接口 + `stdout.py` / `telegram.py` / `ping.py` |
| `digest.py` | 从账本聚合渲染日摘要文本（投影，不改变账本） |
| `cli.py` | `notify emit / status / dispatch / digest / check` 入口 |

## 4. 核心契约

### 4.1 事件：五字段窄腰

```
Event: ts | project | kind | severity(info|warn|alert) | dedup_key | payload(JSON)
```

- `project` = 来源包名（`quantdesk`、`orchestrator`…），`kind` = 该项目的点分事件名（`record.batch`、`ws.silent`、`dispatch.heartbeat`）；
- `dedup_key` 默认 `project:kind` 由 emit 自动派生，可显式覆盖（如 `quantdesk:ws.silent:liquidations`——静默按流区分）；
- **payload 只放机器可再渲染的结构化字段**（计数、时长、文件名），人话文案在 digest/渠道渲染层生成——同 `OcrDocument`"数据与投影分离"姿势。

### 4.2 任务侧 API：两行接入，零依赖倒置

```python
from notify import emit
emit("quantdesk", "record.batch", rows=240, stream="liquidations")
```

- **同步、纯本地、永不抛异常**：账本写失败（磁盘满、库锁）时 stderr 打一行并继续——通知层的故障绝不能成为任务的故障。这是 D-1"任何任务都可接入"的前提；
- 任务侧不 import 任何渠道概念；`emit` 签名里不出现 telegram/token 等字眼的测试断言守此边界。

### 4.3 账本与投递

```
events(id PK, ts, project, kind, severity, dedup_key, payload)
deliveries(event_id, channel, status(pending|sent|failed|suppressed), detail, at)
```

- "已投递"是对每张 (event, channel) 的 `deliveries` 行的推进，**不是 events 表上的布尔标记**——多渠道部分成功是常态，且审计面要求能回答"8月3日那条到底发没发出去"（仿 move_ledger"操作历史即审计"）；
- dispatch 幂等：同一事件同一渠道已有 `sent` 行则跳过；`failed` 行按 §4.4 的升级规则重投；
- 账本行**永不删除**；清理（如 90 天前 info 事件压缩为计数）是 digest 的投影职责，不是 DELETE。

### 4.4 投递策略：alert 插队，info 攒批，重复收敛

`policy.plan()` 是纯函数，输入未决事件 + 各渠道近况，输出投递计划：

- **优先级**：alert > warn > info。info 默认**不即时投递**（除非标记 `urgent`），等 digest 攒批——否则渠道三周后被自己刷屏杀死；
- **首条必发**：新 `dedup_key` 的第一条 alert/warn 无条件即时投递，节流只作用于其后的重复（默认窗口 1h，窗口内合并为一条"同前因已发生 N 次，最近 ts=…"）；
- **投递失败升级**：alert 在所有已配置渠道上 `failed`，或事件 pending 超过 3 轮 dispatch → 生成 `notify:delivery.failed` alert（project 为 `notify` 自己——自举规则唯一允许的例外），下一轮在**全部渠道**重发，含兜底渠道（邮件，若已配置）；
- 渠道限速（Telegram 对单目标约 1 msg/s，429 带 retry_after）在适配器内处理为退避，policy 层不感知具体渠道。

### 4.5 规则评估：silence 即缺口，检测在分发侧

- `config/notify/expectations.yaml` 声明**期望心跳**：`{project, kind, max_silence, message}`——如"quantdesk 的 `record.batch` 每小时应有，静默 > 3h 出 alert"；
- dispatch 每轮先跑 `rules.evaluate(账本摘要, expectations)`，条件不满足即**由 notify 自己** `emit` 对应 alert 事件，再走正常投递——**任务不需要"报告自己静默"，静默本身就是报告**（quantdesk silence-is-a-gap 的通用层化）；
- 规则评估只读账本、幂等：同 key 的静默 alert 在静默持续期间每个节流窗口最多一条。

### 4.6 四层失败域：递归止于失败域换轨，不止于再加一层

| 层 | 机制 | 覆盖的故障 | 自身挂了谁发现 |
|---|---|---|---|
| L1 任务→账本 | emit（本地写入） | 任务逻辑错误可见化 | 账本缺行在 digest 中即为异常 |
| L2 dispatch | 计划任务拉起的短进程 | 流静默、投递失败、多渠道升级 | L3 + L4 |
| L3 外部 ping | 每轮 dispatch 成功后 GET 专属 URL（Healthchecks 免费档即可）；**超时没收到由对方主动联系我** | 整机断电、断网、计划任务没跑——唯一不与我机器同死域的层 | 服务商存活 + L4 |
| L4 反向预期 | 人：**"今天的 digest 没来 = 出事了"** | L3 也不可达（如国内网络对第三方不通） | 这是诚实的极限，写在这里而不是假装不存在 |

- L3 不是新机制：`ping` 适配器与 telegram 适配器**同形**（都是"往一个 URL 发一个请求"），D-4 因此说它近乎免费；
- **升级触发条件**：任务开始对真钱动作（下单）时，L3 外置从免费档升为自有 VPS/第二地服务器——在那之前租机器是负收益。

### 4.7 配置：`.env` 单一来源，渠道信息不进代码不进 git

仿 `llm_client/settings.py` 的规矩，变量约定记在本节即为唯一出处：

```
NOTIFY_DATA_DIR        账本目录覆盖（默认 data/notify/，经 utils.paths）
TELEGRAM_BOT_TOKEN     BotFather 发的 token
TELEGRAM_CHAT_ID       自己的 chat_id
TELEGRAM_PROXY         如 http://127.0.0.1:7890（适配器内传给 httpx）
NOTIFY_CHANNELS        启用渠道列表，如 stdout,telegram,ping
PING_URL               L3 心跳端点（Healthchecks 的 ping URL）
SMTP_*                 （M2 兜底渠道时再定，不预留空壳）
```

渠道启用与 expect 规则分离：`NOTIFY_CHANNELS` 决定投给谁，`expectations.yaml` 决定什么算出事——两者都不需要改代码。

### 4.8 quantdesk 接线（D-6）

- `record` 的三处现状改为 emit：批次落盘（`record.batch`, info）、gap 记录（`record.gap`, warn，含原因）、流静默**不再由 recorder 判断**——删掉自查逻辑，交给 §4.5 规则评估；
- `gaps.log` 保留为渲染投影：由 `notify status` / digest 从账本重放生成，文件本身降级为可再生缓存；
- recorder 的崩溃重启仍不归 notify 管——接计划任务/systemd 时把"进程退出"本身也 emit（`process.exit`, warn），让重启次数在账本里可见，超限报警写在 expectations。

## 5. 里程碑与验收标准

每个里程碑可独立碎掉，验收均为"真机跑通 + 证据入库"。

| 里程碑 | 内容 | 验收 |
|---|---|---|
| **M0 账本与门面**（零网络） | config / events / ledger / cli `emit`+`status`、stdout 适配器、最简 dispatch（无节流直投 stdout） | 测试目录：emit 一万条后 `status` 按 project/kind 计数正确；重复 dispatch 幂等（不重发 stdout 已 sent 的行）；账本库损坏时 emit 不抛异常仅 stderr；`tests/test_notify/` 全绿、`uv run notify` 可用 |
| **M1 alert 通路**（第一个真渠道 + 静默检测） | telegram 适配器（代理）/ policy（优先级+节流+升级）/ rules + expectations.yaml / 完整 dispatch + cron（Windows 计划任务）接线文档 | 真机：`notify emit --severity alert` 一分钟内手机收到；伪造 3h 无 `record.batch` 事件 → dispatch 当轮生成并送达静默 alert；同 key alert 连发 5 条 → 只收到 1 条即时 + 1 条合并；拔掉代理跑 dispatch → 产生 `delivery.failed` alert，接回后下轮补发；`policy.plan` golden 用例入库 |
| **M2 digest + L3 + 首接线** | digest 日摘要、ping 适配器（L3）、邮件兜底适配器、quantdesk record 接线（§4.8） | 真实 recorder 跑一天：次日 digest 含各流行数/最后数据时间/gap 清单；L3 ping URL 在 Healthchecks 显示连续、手动停 dispatch 一个超时窗后收到对方告警；`notify status --project quantdesk` 回答"昨天缺哪个小时"无需翻文件 |

M3（显式 gated）：多渠道矩阵（ntfy/企微）、事件保留策略、Web 状态页——各等真实使用暴露需求再立项。

## 6. 已知风险（正面记录，不掩盖）

1. **代理与 dispatch 同机同命运**（M1 最大风险）：Telegram 通道依赖本机代理，代理断 = 告警断，且这两者故障域相同。对策即 §4.4 升级路径：投递失败生成自告事件 + 兜底渠道（邮件几乎不需代理）。**渠道冗余不是锦上添花，是这条通道的地基**。
2. **L3 第三方在国内可达性未验证**：Healthchecks.io 的 ping 出向请求与告警回推（默认邮件）需 M2 真机验证；不通则 L3 降级、L4 权重上升，并在 digest 文案中明示"L3 当前未生效"——不假装四层都在。
3. **刷屏 → 被屏蔽 → 渠道死亡**是自建通知最常见的死因：M1 验收特意包含节流用例；info 永远攒批是硬规矩，不是默认值。
4. **账本膨胀**：emit 永不阻塞意味着长跑任务可能堆百万行 info。个人量级 SQLite 无压力（orchestrator 05a 实测口径），但 digest 的聚合读要按 (project,kind,day) 走索引，M0 建库时即建。
5. **计划任务本身没跑**（Windows 重启后任务丢失/睡眠错过）：这恰是 L3 设计覆盖的故障——ping 断供由外部说话；L3 未生效前，此项风险已知且接受，记录于此。

## 7. 依赖与接入清单

- 无新增第三方依赖：HTTP 用已在的 `httpx`（异步在 llm_client 已用），SQLite 走 `src/storage`，路径走 `utils.paths`。SMTP 兜底用 stdlib `smtplib`（M2 时定）。
- 新顶层包 gotcha：建 `src/notify/` 后加入 `pyproject.toml` 的 `[tool.hatch.build.targets.wheel] packages` 并 `uv pip install -e .`（AGENTS.md 记录的坑）。
- `[project.scripts]` 增 `notify = "notify.cli:main"`。
- 数据目录 `data/notify/`（repo-root 锚定）；配置 `config/notify/expectations.yaml`（tracked，改动即规则变更评审）。
- 实现落地时 AGENTS.md 增一行 notify 条目（共享层表格，紧跟 `storage`）。

## 8. 待定项

1. Telegram 侧最终形态：纯 `sendMessage`（够用）还是带 inline 按钮的"确认/静音 1 小时"——M1 实施时定，v1 倾向前者；
2. Windows 计划任务的落地方式（`schtasks` 直注册 vs PowerShell `Register-ScheduledTask`）与错过补偿（`StartWhenAvailable`）——M1 接 cron 时真机验证；
3. digest 投递节奏与窗口（每天几点、统计窗是否滚动 24h）——M2 用起来再调；
4. `notify check` 子命令的最终职责边界（配置校验 / 渠道连通性试发 / 规则 dry-run）——M1 末定。
