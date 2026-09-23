# 通知共享层（notify）设计

> 一句话核心：任务只往**本地账本**记结构化事件，**投递由独立的一次性分发器完成**——运行状态与 alert 是同一条通道上的两种节奏，渠道是可插拔适配器，"谁来监督监控者"的递归止于**不同失败域的叠加**而非无限套娃。

日期：2026-09-22。状态：**M1 shipped 2026-09-23**（telegram 适配器 / policy / rules + expectations.yaml / 完整 dispatch / `notify check` / `notify schedule`；`tests/test_notify/` 105 测试绿，§5 的四项真机验收全过，证据见 §5）。M0（2026-09-22：config / events / ledger / channels(stdout) / cli）已并入。计划任务**已在本机注册**（`notify schedule install`，§4.9），无人值守的一轮已由调度器自己跑通并送达手机；唯一欠账：**合盖睡眠后的补跑尚未物理验证**（`-StartWhenAvailable` 的语义，记 TODO.md）。读法：§1–§2 是需求与已定裁决（背景）；§3–§4 是实现契约（开工后以本节为准）；§5 是里程碑与验收标准；§6–§8 是风险、依赖与待定项。

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
| `schedule.py` | 把"谁来拉起 dispatch"包成一个命令：构造/注册/查询/删除计划任务，路径全部由 `REPO_ROOT` 推导（§4.9）；只 shell 到系统调度器，不做常驻 |
| `digest.py` | 从账本聚合渲染日摘要文本（投影，不改变账本） |
| `cli.py` | `notify emit / status / dispatch / digest / check / schedule` 入口 |

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
deliveries(event_id, channel, status(pending|sent|failed|suppressed), detail, at, attempts)
```

- "已投递"是对每张 (event, channel) 的 `deliveries` 行的推进，**不是 events 表上的布尔标记**——多渠道部分成功是常态，且审计面要求能回答"8月3日那条到底发没发出去"（仿 move_ledger"操作历史即审计"）；
- `attempts` 是 §4.4"连败 3 轮"的计数位：`failed` 时 +1、`sent` 时归零，由 upsert 维护，不在业务代码里加。M0 账本靠 `storage.sqlite` 的两级演进升级（`ensure_columns` 加列 + `user_version` 盖章），下次打开即完成，无需手工补丁——2026-09-23 真机验证：既有 2 行的 M0 库开盖后列已在、行未动；
- dispatch 幂等：同一事件同一渠道已有 `sent` 行则跳过；`failed` 行按 §4.4 的升级规则重投；
- 账本行**永不删除**；清理（如 90 天前 info 事件压缩为计数）是 digest 的投影职责，不是 DELETE。

### 4.4 投递策略：alert 插队，info 攒批，重复收敛

`policy.plan()` 是纯函数，输入未决事件 + 各渠道近况，输出投递计划：

- **优先级**：alert > warn > info。info 默认**不即时投递**（除非标记 `urgent`），等 digest 攒批——否则渠道三周后被自己刷屏杀死；
- **首条必发**：新 `dedup_key` 的第一条 alert/warn 无条件即时投递，节流只作用于其后的重复（默认窗口 1h，窗口内合并为一条"同前因已发生 N 次，最近 ts=…"）；
- **投递失败升级**：alert 在所有已配置渠道上 `failed`，或事件 pending 超过 3 轮 dispatch → 生成 `notify:delivery.failed` alert（project 为 `notify` 自己——自举规则唯一允许的例外），下一轮在**全部渠道**重发，含兜底渠道（邮件，若已配置）；
- 渠道限速（Telegram 对单目标约 1 msg/s，429 带 retry_after）在适配器内处理为退避，policy 层不感知具体渠道。

M1 落地时补齐的三处实现口径（本节为准，勿再从上面的条文重新推导）：

- **"info 不即时投递"落在读侧，不落在 policy**：`ledger.pending_for()` 默认只取 `alert|warn`，`urgent` 的例外由 SQL 的 `json_extract(payload,'$.urgent')=true` 放行。policy 因此永远不会规划它本不该看见的东西，两边的规则不会漂移；
- **升级范围含 warn**（`policy.ESCALATE_SEVERITIES = {alert, warn}`）。条文只写了 alert，实现放宽到 warn 的理由是：warn 是 `record.gap` 这一类"缺数据"事件，它投不出去与 alert 投不出去是同一种失明——反正 info 已经不进即时路径，放宽不会带来刷屏；
- **自告事件不作茧：三重防护**——`is_self_alert` 的事件既不进 `stuck`、也不进"全渠道失败"的被告集合，其产生又按 `notify:delivery.failed` 这条 dedup_key 的上一次出现时间节流。于是"渠道死了"这件事在窗口内是一小时一条，而不是每轮 dispatch 一条（真机验收第 4 项：第 2 轮打印 `escalation throttled` 并保持账本 1 条）。自告事件在本轮 dispatch **末尾**入库，因此它下一轮在全部渠道上重发——包括可能已经活过来的那条。

### 4.5 规则评估：silence 即缺口，检测在分发侧

- `config/notify/expectations.yaml` 声明**期望心跳**：`{project, kind, max_silence, message}`——如"quantdesk 的 `record.batch` 每小时应有，静默 > 3h 出 alert"；
- dispatch 每轮先跑 `rules.evaluate(账本摘要, expectations)`，条件不满足即**由 notify 自己** `emit` 对应 alert 事件，再走正常投递——**任务不需要"报告自己静默"，静默本身就是报告**（quantdesk silence-is-a-gap 的通用层化）；
- 规则评估只读账本、幂等：同 key 的静默 alert 在静默持续期间每个节流窗口最多一条。

M1 落地的两条口径：

- **"从未见过"就是静默**：`last_seen` 里没有这条 (project, kind) 时按 `never_seen: true` 出 alert，因为一个从未上报过的生产者与一个死掉的生产者对账本而言不可区分。推论是接线顺序必须是**先接 emit、再 enabled**——否则规则会在生产者存在之前把它报警出来；
- 因此随代码入库的 `config/notify/expectations.yaml` 只带一条规则（quantdesk `record.batch` > 3h），且 `enabled: false`：recorder 的计划任务还没接（§4.8 在 M2），此刻打开它等于天天告警。文件里注释了每个字段与打开的前置条件，`test_shipped_expectations_file_loads_and_is_quiet` 把"这条规则保持关闭"钉成测试——规则要变，连测试一起改，这就是"改动即评审"的可执行形式。

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
NOTIFY_CHANNELS        启用渠道列表，如 stdout,telegram,ping（默认 stdout）
NOTIFY_EXPECTATIONS    规则文件覆盖（默认 config/notify/expectations.yaml）
TELEGRAM_BOT_TOKEN     BotFather 发的 token
TELEGRAM_CHAT_ID       自己的 chat_id（用 getUpdates 自问自答一条即可读到）
TELEGRAM_PROXY         可选，如 http://127.0.0.1:7890；**空值 = 不走代理**
PING_URL               L3 心跳端点（Healthchecks 的 ping URL，M2）
SMTP_*                 （M2 兜底渠道时再定，不预留空壳）
```

- 谁读哪个变量也是约定：`NOTIFY_*` 三个在 `config.py`（包的唯一路径/开关出口），`TELEGRAM_*` 在 `channels/telegram.py`——config 知道哪些渠道开着，永远不想知道某个渠道需要什么；
- **"必须挂代理"是设计期的假设，2026-09-23 真机否掉了**：本机直连 `api.telegram.org` 返回 200，而 `.env` 里那行 `TELEGRAM_PROXY=127.0.0.1:7890` 指向一个根本没在监听的端口（`netstat` 无、`curl -x` 连接被拒）。写死代理 = 造一条死渠道，所以适配器把空/缺失一律当作"不走代理"。§6.1 的风险叙述随之改写：同域共命运的不再是"渠道+代理"，而是"渠道+dispatch"。

渠道启用与 expect 规则分离：`NOTIFY_CHANNELS` 决定投给谁，`expectations.yaml` 决定什么算出事——两者都不需要改代码。

### 4.8 quantdesk 接线（D-6）

- `record` 的三处现状改为 emit：批次落盘（`record.batch`, info）、gap 记录（`record.gap`, warn，含原因）、流静默**不再由 recorder 判断**——删掉自查逻辑，交给 §4.5 规则评估；
- `gaps.log` 保留为渲染投影：由 `notify status` / digest 从账本重放生成，文件本身降级为可再生缓存；
- recorder 的崩溃重启仍不归 notify 管——接计划任务/systemd 时把"进程退出"本身也 emit（`process.exit`, warn），让重启次数在账本里可见，超限报警写在 expectations。

### 4.9 dispatch 的调度接线（Windows 计划任务）

§1 那条非目标（不做常驻 daemon）加上 §4.6 的 L2，合起来就是"dispatch 是被计划任务拉起的短进程"，于是**这条通道唯一的常驻组件是操作系统的调度器**——notify 自己不 fork、不守护。M1 交付的是 `notify schedule`：注册本身仍是一个由 owner 执行的命令（它改的是系统状态，不是仓库状态），但命令背后那份该记的东西——路径、间隔、补跑、日志——由代码负责每次都对。

**周期怎么定**：alert 的到达上界就是调度周期（emit 只写账本，投递要等下一轮 dispatch）。节流窗口是 1h，所以 1 分钟一轮不会多发消息，只会更早发。默认 5 分钟（`DEFAULT_INTERVAL_MINUTES`）；要 §5 那条"一分钟内收到"字面成立，`notify schedule install --interval-minutes 1`。空转的一轮成本是一次 SQLite 读 + 一次 YAML 读，毫秒级。

**注册是一个命令**（`src/notify/schedule.py`）：

```
uv run notify schedule install [--interval-minutes 5]   # 注册/更新
uv run notify schedule status                          # 调度器视角 + dispatch.log 尾部
uv run notify schedule run-now                         # 立刻补一轮（走调度器，不是人肉 dispatch）
uv run notify schedule remove
```

它生成的是 `Register-ScheduledTask`，把该记的事每次都记对，而不是靠人记得：工作目录 = `REPO_ROOT`、重跑间隔、`-StartWhenAvailable`、stdout/stderr 重定向到 `data/notify/dispatch.log`。**为什么不是 `schtasks`**：本机实测它的旗标表里**没有**工作目录项（`/D /DELAY /DU /EC /ED /ET /F /I /IT /K /M /MO /NP /P /RI /RL /RP /RU /S /SD /ST /TN /TR /U /V1 /XML /Z`），只能把 `cd /d` 塞进 `/TR`；而 `StartWhenAvailable`（错过补跑）与 `ExecutionTimeLimit` 只有 `Register-ScheduledTask` 能显式设。动作直接调用装好的 `.venv/Scripts/notify.exe` 而不是 `uv run`：无人值守的进程不该依赖 `uv` 在 `PATH` 上、也不该在那一刻赌解析器的判断。

真机注册时踩到三个 PowerShell 的坑，都写进了实现（每一条都会"看起来成功"）：

- **`schtasks /Query /V` 的字段名跟着控制台 UI 语言走**，这台机器答中文，按英文 label 过滤会静默返回空——`status` 因此读 `Get-ScheduledTask` / `Get-ScheduledTaskInfo` 的属性名（与 locale 无关）；
- **cmdlet 失败默认是 non-terminating error**：`-Command` 里一条坏语句可以什么都不打印、仍然 exit 0。所以每条命令都以 `$ErrorActionPreference = 'Stop'` 开头——否则 `schedule status` 会显示成"调度器是空的"，而它其实是"我没读懂"；
- **双引号字符串只展开变量，不展开成员访问**：`"$t.State"` 打出来的是对象的 `ToString()` 加字面量 `.State`，属性必须写成 `$($t.State)`。第一条命令就打了这么一行废话给我看。

四条约定与它们的理由：

- **`-StartWhenAvailable`（错过补偿）是必需的**，不是可选优化：机器睡眠/重启后调度器会丢掉整段窗口，没有它 L2 的故障面等于"这台机器上次开机以来"。它补的是"漏跑"，补不了"这台机器根本没醒"——后者仍是 L3/L4 的领地（§4.6）；
- **`ExecutionTimeLimit` = 周期**（设计期写的是"短于周期"，实现时改了）：一轮 dispatch 正常是秒级，超时只可能是网络卡在某个渠道上；让它死掉、由下一轮重试。不许轮次叠轮次靠的不是把时限设小——Task Scheduler 默认就不允许同一任务并发实例，时限只是兜底；
- **电池策略 `-AllowStartIfOnBatteries -DontStopIfGoingOnBatteries`**：这是一台笔记本。默认设置下"拔了电源"就够让一轮告警投递不被启动，而 notify 存在的理由恰恰是人不盯着的时候；
- **日志落在 `data/notify/dispatch.log`**：`data/` 与 `cache/` 的分界判据是"有没有命令能重建它"（AGENTS.md）——账本能回答"消息发没发出去"，但答不了"调度器有没有把我拉起来、起来后死在哪一行"，那条证据只在 stdout/stderr 里，不可再生，所以归 `data/`。**它没有轮转**：一轮一行/渠道，默认周期下约 1.5k 行/天，且绝大多数是 `messages 0, sent 0`；不预先造 rotation（`status` 只读尾部若干行，文件大小的痛要晚得多才出现），等它真的开始占地方再决定截断策略。怀疑调度器时：先 `notify schedule status`（它把这一页日志的尾巴塞进输出），再 `notify check` 看配置与规则。

**已注册并真机跑过（2026-09-23）**：`schedule install` 之后，第一轮无人值守的 dispatch 由调度器在 `12:50:33` 自己拉起（`LastTaskResult 0`），投递了 09-22 那条还压在账本里的 `quantdesk.ws.silent` alert——`deliveries` 里 event 2 首次出现 `telegram/sent` 行（这一行是适配器拿到 Telegram 的 `ok` 才写的），而这一轮没有任何人敲过 `notify dispatch`。到达手机这一段不是本轮新证的，是上面四项验收已经走过的那条通路。随后 `schedule run-now` 再补一轮：`telegram: messages 0, sent 0`——同一条已 sent 的事件没有被重发，节流与 pending 过滤在跨调度轮次上也成立。**唯一仍欠的实测**：合盖睡眠 20 分钟再唤醒，看 `dispatch.log` 里是否出现补跑的那几轮（`-StartWhenAvailable` 的语义验证，要物理动作，记 TODO.md）。

**为什么不用仓里已有的 `src/task_queue`**：那是一个**进程内**的异步队列，把 dispatch 投成它的一条 lane 就等于让告警通道的存活依赖于"某个常驻进程正好在跑"——D-5 排除常驻 daemon 的理由一个字都没变，何况 task_queue 自己挂掉时没有任何层会报警（监控者与被监控者同进程）。操作系统的调度器是这里唯一合理的宿主：它自己死了，是 §4.6 的 L3/L4 该说话的事。

## 5. 里程碑与验收标准

每个里程碑可独立碎掉，验收均为"真机跑通 + 证据入库"。

| 里程碑 | 内容 | 验收 |
|---|---|---|
| **M0 账本与门面**（零网络） | config / events / ledger / cli `emit`+`status`、stdout 适配器、最简 dispatch（无节流直投 stdout） | 测试目录：emit 一万条后 `status` 按 project/kind 计数正确；重复 dispatch 幂等（不重发 stdout 已 sent 的行）；账本库损坏时 emit 不抛异常仅 stderr；`tests/test_notify/` 全绿、`uv run notify` 可用 |
| **M1 alert 通路**（第一个真渠道 + 静默检测） | telegram 适配器（代理）/ policy（优先级+节流+升级）/ rules + expectations.yaml / 完整 dispatch + `notify schedule`（Windows 计划任务接线，§4.9） | 真机：`notify emit --severity alert` 一分钟内手机收到；伪造 3h 无 `record.batch` 事件 → dispatch 当轮生成并送达静默 alert；同 key alert 连发 5 条 → 只收到 1 条即时 + 1 条合并；拔掉代理跑 dispatch → 产生 `delivery.failed` alert，接回后下轮补发；`policy.plan` golden 用例入库 |
| **M2 digest + L3 + 首接线** | digest 日摘要、ping 适配器（L3）、邮件兜底适配器、quantdesk record 接线（§4.8） | 真实 recorder 跑一天：次日 digest 含各流行数/最后数据时间/gap 清单；L3 ping URL 在 Healthchecks 显示连续、手动停 dispatch 一个超时窗后收到对方告警；`notify status --project quantdesk` 回答"昨天缺哪个小时"无需翻文件 |

M3（显式 gated）：多渠道矩阵（ntfy/企微）、事件保留策略、Web 状态页——各等真实使用暴露需求再立项。

### M1 真机验收记录（2026-09-23）

四项全部在真机跑过，收件方是 owner 的手机（Telegram）。验收跑在临时账本 `NOTIFY_DATA_DIR=data/notify_m1_acceptance/` 上——**正式账本 append-only，不能往里灌合成事件**，这是 D-5 的纪律而不是洁癖；跑完即删。

| 验收项 | 实际输出 | 结论 |
|---|---|---|
| emit alert → 手机收到 | `telegram: messages 1, sent 1, failed 0`（直连，无代理） | 过 |
| 伪造 3h 静默 → 当轮生成并送达 | 插一条 4h 前的 `record.batch`(info)，`dispatch` 打 `rules: emitted 1 silence alert(s): quantdesk:record.batch:silent` 并 `sent 1`；同轮 `check` 先行 dry-run，只报 `firing now: 1` 不动渠道 | 过 |
| 同 key 连发 5 条 → 1 即时 + 1 合并 | `stdout/telegram: messages 2, sent 5`；合并条 payload 为 `{"repeats": 4, "event_ids": [5,6,7,8], "detail": {...}}`；再跑一轮 `messages 0, sent 0` | 过 |
| 渠道死掉 → 自告 + 接回补发 | 第 1 轮 `sent 0, failed 1` + `escalated: notify:delivery.failed for event(s) [9]`；第 2 轮仍死 → `escalation throttled`，账本仍 1 条；恢复后 `messages 2, sent 2`（原事件 + 自告条一起补上），`deliveries` 里 event 9 变为两渠道 `sent` | 过 |

两处如实记录的偏差：

1. **"拔掉代理"改为"把代理指到一个死端口"**（`TELEGRAM_PROXY=http://127.0.0.1:1`）。语义等价（都是 `httpx.ConnectError` → `ChannelError` → `failed` 行），且可复现；真拔代理在这台机器上反而造不出故障——§4.7 记的正是"本机不需要代理"；
2. **"一分钟内收到"测的是 emit→dispatch→手机这条通路本身**（手动 dispatch，秒级到达）。注册之后（§4.9），端到端上界变成"下一个调度点"= 调度周期，默认 5 分钟；要字面成立就 `notify schedule install --interval-minutes 1`——节流窗口 1h 保证加密周期只会更早发、不会多发。无人值守那一轮的证据在 §4.9 末（调度器自己拉起、`LastTaskResult 0`、手机上收到 09-22 那条 alert）。

离线侧的证据面：`tests/test_notify/` 105 绿（含 `tests/golden/notify_policy.jsonl` 8 条 golden 把 §4.4 的每条规则钉成数据：首条必发、一轮内 5→2、窗口内重复、窗口过期、按 dedup_key 分流域、urgent info、连败 3 轮 stuck 且自告不参与、2 轮不算 stuck）。全仓 782 绿。所有测试都不碰网络、不碰真 `.env`、也不碰调度器：telegram 用 `httpx.MockTransport`，凭证由 fixture 覆盖，`test_schedule.py` 把 `subprocess.run`/`shutil.which`/`platform.system` 全部换成假的，只断言它**将要**执行的那段 PowerShell 长什么样。

## 6. 已知风险（正面记录，不掩盖）

1. **渠道与 dispatch 同机同命运**（M1 最大风险，2026-09-23 修订）：设计期以为风险在"Telegram 依赖本机代理"，真机发现本机直连可达、那行代理指向死端口——代理这一环根本不存在。真正的同域故障于是落在 **dispatch 进程与它所依赖的网络栈同一台机器上**：机器睡了、断网了、计划任务没跑，渠道与上报者一起哑。对策不变且更明确：§4.4 的升级路径（已真机验证）+ 邮件兜底（M2，走的不是同一条出向链路）+ L3 外部 ping（M2，唯一不同域的层）。**渠道冗余不是锦上添花，是这条通道的地基**。
2. **L3 第三方在国内可达性未验证**：Healthchecks.io 的 ping 出向请求与告警回推（默认邮件）需 M2 真机验证；不通则 L3 降级、L4 权重上升，并在 digest 文案中明示"L3 当前未生效"——不假装四层都在。
3. **刷屏 → 被屏蔽 → 渠道死亡**是自建通知最常见的死因：M1 验收特意包含节流用例；info 永远攒批是硬规矩，不是默认值。
4. **账本膨胀**：emit 永不阻塞意味着长跑任务可能堆百万行 info。个人量级 SQLite 无压力（orchestrator 05a 实测口径），但 digest 的聚合读要按 (project,kind,day) 走索引，M0 建库时即建。
5. **计划任务本身没跑**（Windows 重启后任务丢失/睡眠错过）：机器侧的缓解已落地并注册（§4.9：`-StartWhenAvailable` 补跑 + 电池策略 + `dispatch.log` 作为"调度器有没有拉起我"的唯一证据），但"合盖睡眠后到底补没补"仍是纸面推断——真要证它得物理合盖一次，欠在 TODO.md。这一层之上仍是 L3 的领地：ping 断供由外部说话；L3 未生效前，此项风险已知且接受，记录于此。

## 7. 依赖与接入清单

- **实际 import 面**：`click`（CLI）、`httpx`（telegram 适配器）、`sqlalchemy`（经 `src/storage`）、`python-dotenv`（读 `.env`）、`yaml`（`rules.load`）。**五个全部在 `pyproject.toml` 声明**（2026-09-23 补齐 `click`+`pyyaml`——此前全仓靠 `streamlit` 的传递依赖在场，`orchestrator/registry.py` 用 `yaml` 已是同一个缺口：删掉 streamlit 会打死十一个 CLI）。补声明时踩到一个真机 gotcha：`uv add` 会附带一次不带 extras 的 `uv sync`，于是本机 venv 里的 `ocr` + `paddle-gpu` 两包被静默卸掉——**改依赖后要用 `uv sync --locked --extra ocr --extra paddle-gpu` 复原**，别只盯着 `uv.lock` 的 diff。SMTP 兜底用 stdlib `smtplib`（M2 时定）。
- `schedule.py` 的 import 面是纯 stdlib（`platform`/`shutil`/`subprocess`/`pathlib`）+ `utils.paths`：它要在没有人盯着的时候运行，多一个第三方入口就多一种"那次恰好没装"的失败方式。因此也不引入 `pywin32` 之类的计划任务库——PowerShell 是 Windows 自带且一定在场的。
- 新顶层包 gotcha：建 `src/notify/` 后加入 `pyproject.toml` 的 `[tool.hatch.build.targets.wheel] packages` 并 `uv pip install -e .`（AGENTS.md 记录的坑）。
- `[project.scripts]` 增 `notify = "notify.cli:cli"`（click group，2026-09-23 全仓 CLI 迁移后的统一姿势）。
- 数据目录 `data/notify/`（repo-root 锚定）；配置 `config/notify/expectations.yaml`（tracked，改动即规则变更评审）。
- 实现落地时 AGENTS.md 增一行 notify 条目（共享层表格，紧跟 `storage`）。

## 8. 待定项

M1 收口的三项：

1. **Telegram 形态 = 纯 `sendMessage`（已定，2026-09-23）**。v1 不带 inline 按钮：按钮要 webhook 或轮询 `getUpdates`，前者要求一个可被外网寻址的端点（与"不租 VPS"冲突），后者把 dispatch 变成常驻消费者（与 §1 的短进程前提冲突）。静音/确认因此写在文案里由人处理，不做成机器能力。真要它，起点是给 `TelegramChannel` 加一个 `getUpdates` 的读侧 + 一个新的 `snooze` 规则，而不是改 policy；
2. **`notify check` 的边界 = 三件事，且只这三件（已定）**：配置校验（账本路径、渠道名可解析——未知名非零退出）、规则 dry-run（打印"现在会 firing 什么"，**不写账本**）、可选 `--send` 逐渠道试发（失败只打印不退出，因为它诊断的是连通性不是配置）。错误文案只点名变量名（`TELEGRAM_BOT_TOKEN`）绝不点值，这条有测试守着。**"计划任务在不在场"不属于这三件**（2026-09-23 再确认）：那是 `notify schedule status` 的职责，`check` 因此一次也不 shell 到 PowerShell，保持与平台无关；
3. **计划任务的落地方式已定并已注册（2026-09-23）**：不再是文档里的一段 PowerShell，而是 `notify schedule install / status / run-now / remove`（§4.9）。选 `Register-ScheduledTask` 而非 `schtasks` 不是风格问题——本机实测 `schtasks /create` 没有工作目录旗标（只能把 `cd /d` 塞进 `/TR`），也只有 `Register-ScheduledTask` 能显式设 `StartWhenAvailable`（错过补跑）与 `ExecutionTimeLimit`。**注册已完成、无人值守一轮已由调度器自己跑通并送达手机**；仍欠的只有"合盖睡眠 20 分钟再唤醒后 `dispatch.log` 里出现补跑轮次"这一条物理验证（记 TODO.md，M2 的 L3 ping 之前补上，否则 L2 的补跑语义与 L3 一起是空的）。

仍待定：

4. digest 投递节奏与窗口（每天几点、统计窗是否滚动 24h）——M2 用起来再调；
5. info 的清理策略（90 天前压缩为计数，§4.3）——账本真长到需要之前不动。
