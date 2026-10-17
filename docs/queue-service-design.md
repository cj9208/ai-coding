# Queue Service 共享层设计

> 一句话核心：`src/task_queue` 是通用异步任务调度层——分 lane 的任务队列管"谁先做、能做几个"，LLMClient 内的 token bucket 管"下游不能打爆"。两层各管各的，通过背压自然串联。受支持的接入面只有一种：**调用方自己构造 `TaskQueue`、用 `async with` 持有其生命周期**（进入即 start、退出即 drain+stop）。曾设计过的 `@queued` 装饰器与进程级默认单例已按 §12 裁决删除（2026-10-17）——隐式单例没有 owner 去 `stop()`，只会二选一：没 start 就 submit 崩，或 auto-start 后 worker 泄漏到进程退出。

日期：2026-09-21。状态：**已实现并按 §12 裁决收缩**（`src/task_queue/`：`service.py` 分 lane 队列 + `async with` 生命周期；`llm_client/rate_limit.py` token bucket）。采用现状（2026-10-17）：唯一生产消费者是 `rag/enrich.py`，**直接构造 `TaskQueue`**（显式 start/stop）。`decorators.py`（`@queued` / `get_queue` / `set_default_queue`）已删除。pdf_summarizer 保持手搓 `Semaphore`（A-1），research_agent 仍串行——预算重构（阶段 1）已落地并测试，lane 接入（阶段 2–3）gated 在真实需求信号（A-3，细化见 §12.4）。裁决全文见 §12。

## 1. 背景：为什么需要这个层

### 1.1 现状

仓库里所有 LLM 调用都是"直接 await"——没有调度、没有限流、没有优先级：

| 组件 | 并发模型 | 问题 |
|---|---|---|
| `rag enrich` (`src/rag/enrich.py`) | async 但串行 for 循环 | 30k chunks × 3s = 25h，无并发 |
| `pdf_summarizer` (`src/pdf_summarizer/summarizer.py`) | `Semaphore` + `gather` | 唯一有并发的，但限流逻辑不可复用 |
| `llm_client` (`src/llm_client/client.py`) | tenacity 被动重试 429 | 事后补救，无主动限流 |
| `research_agent` | 串行 LLM 调用 | 同 enrich |

`04-scaling.md` §5 已明确指出：enrich 必须是可降级的后台任务，不能阻塞主路径。但"怎么做"没有落地。

### 1.2 核心矛盾

直接建一个全局 queue 行不通——不同任务的需求完全不同：

```text
  任务类型          优先级     量级        延迟容忍    LLM 端点
  ─────────────────────────────────────────────────────────────
  enrich           低         30k/天      小时级      chat
  query answer     高         按需        秒级        chat
  embedding        中         批量        分钟级      embedding
  research agent   中         按需        分钟级      chat
```

一个 FIFO 队列里，enrich 的 30k 任务会饿死 query answer。一个全局并发数也管不住——chat 和 embedding 的 provider 限速不同。

### 1.3 两个独立问题

分析下来，这是两个正交的关切，不该混在一个类里解决：

| 问题 | 本质 | 该谁管 |
|---|---|---|
| 任务调度 | 谁先做、谁后做、同时能做几个 | TaskQueue（lane 模型） |
| 下游限流 | LLM provider 不能打爆 | TokenBucket（在 LLMClient 内） |

混在一起的问题：queue 要感知 model 类型（职责不清）；换 provider 要改 queue（耦合）；新增任务类型要改 queue 的限流逻辑（膨胀）。

## 2. 架构：两层分离

```text
  业务代码
  ┌──────────────────────┐  ┌──────────────────────┐
  │ @queued(lane="enrich")│  │ @queued(lane="query") │
  │ async def enrich_one │  │ async def answer_one  │
  │     (chunk):         │  │     (question):       │
  │   await client.chat  │  │   await client.chat   │
  └──────────┬───────────┘  └──────────┬───────────┘
             │ submit                  │ submit
             ▼                         ▼
  ┌─────────────────────────────────────────────────────┐
  │  Layer 1: TaskQueue（任务调度层）                     │
  │  src/task_queue/service.py                               │
  │                                                     │
  │  ┌─────────────────┐  ┌─────────────────┐          │
  │  │ lane "enrich"   │  │ lane "query"    │  ...     │
  │  │ 4 workers       │  │ 6 workers       │          │
  │  │ asyncio.Queue   │  │ asyncio.Queue   │          │
  │  └────────┬────────┘  └────────┬────────┘          │
  │           │                    │                    │
  │     各 lane 独立隔离，互不饿死                      │
  └───────────┼────────────────────┼───────────────────┘
              │ worker 取任务后     │
              │ 调用业务函数        │
              ▼                    ▼
  ┌─────────────────────────────────────────────────────┐
  │  Layer 2: TokenBucket（限流层）                      │
  │  src/llm_client/rate_limit.py                       │
  │                                                     │
  │  ┌───────────────────────────────────────┐          │
  │  │ "deepseek-chat":      60 RPM          │          │
  │  │ "deepseek-embedding": 120 RPM         │          │
  │  │ ...（按 model 名自动分桶）             │          │
  │  └───────────────────────────────────────┘          │
  │                                                     │
  │  acquire() 拿不到令牌 → await 等待                   │
  │  → worker 阻塞 → 不再从 lane queue 取任务            │
  │  → queue 堆积 → submit() 阻塞（背压）                │
  └─────────────────────────────────────────────────────┘
```

**背压传导链**（全自动，不需要显式"限流"代码）：

```text
  LLM provider 慢
    → TokenBucket 令牌耗尽 → acquire() await
      → worker 卡在 LLM 调用上
        → 不再从 lane queue 取任务
          → queue 满 → submit() await（背压传导到调用方）
            → 调用方自然降速
```

## 3. Layer 1: TaskQueue（任务调度层）

### 3.1 Lane 模型

不是一个全局 FIFO，而是**按 lane 隔离**：每个 lane 有独立的 queue 和 worker pool。

```text
  TaskQueue
  ├── lane "enrich"  → Queue(maxsize=500)  → 4 workers
  ├── lane "query"   → Queue(maxsize=100)  → 6 workers
  └── lane "embed"   → Queue(maxsize=1000) → 4 workers
```

为什么不用单一 queue + priority：

- 单一 queue 需要复杂的优先级调度（priority heap、starvation prevention）
- lane 隔离天然解决"enrich 饿死 query"——它们在不同的 queue 里
- lane 的 worker 数可以独立调——query 需要低延迟所以多给 worker，enrich 是批量所以少给
- 新增任务类型 = 加一个 lane，不动其他 lane

### 3.2 核心 API

```python
# src/task_queue/service.py

class TaskQueue:
    """全局异步任务队列，按 lane 隔离。"""

    def __init__(self):
        self._lanes: dict[str, _Lane] = {}

    def lane(
        self,
        name: str,
        *,
        workers: int = 4,
        maxsize: int = 1000,
    ) -> None:
        """声明一个 lane。在 start() 之前调用。"""

    async def start(self) -> None:
        """启动所有 lane 的 worker pool。"""

    async def stop(self, *, timeout: float = 30) -> None:
        """Graceful shutdown：停止接收新任务，等待已提交任务完成。"""

    async def submit(
        self,
        lane: str,
        fn: Callable,
        *args,
        **kwargs,
    ) -> asyncio.Future:
        """提交任务到指定 lane。queue 满时 await（背压）。"""

    def stats(self) -> dict[str, LaneStats]:
        """各 lane 的 pending / running / completed / failed 计数。"""
```

### 3.3 Lane 内部结构

```python
class _Lane:
    name: str
    queue: asyncio.Queue[_Task]
    workers: list[asyncio.Task]
    stats: LaneStats

class _Task:
    fn: Callable
    args: tuple
    kwargs: dict
    future: asyncio.Future      # submit() 返回的，调用方 await 它拿结果
    retries: int
    max_retries: int

class LaneStats:
    pending: int                # queue.qsize()
    running: int                # 正在执行的 worker 数
    completed: int
    failed: int
```

### 3.4 Worker 循环

```python
async def _worker_loop(self, lane: _Lane) -> None:
    while True:
        task = await lane.queue.get()       # 等待任务
        if task is _SHUTDOWN:               # 优雅退出信号
            break
        try:
            lane.stats.running += 1
            result = await task.fn(*task.args, **task.kwargs)
            task.future.set_result(result)
            lane.stats.completed += 1
        except Exception as exc:
            if task.retries < task.max_retries:
                task.retries += 1
                await lane.queue.put(task)  # 重新入队
            else:
                task.future.set_exception(exc)
                lane.stats.failed += 1
        finally:
            lane.stats.running -= 1
            lane.queue.task_done()
```

### 3.5 Graceful Shutdown

`stop()` 的行为：

1. 设置 `_accepting = False`，后续 `submit()` 抛 `QueueClosed`
2. 向每个 lane 的 queue 放入 `len(workers)` 个 `_SHUTDOWN` 哨兵
3. `await asyncio.gather(*all_workers)` 等待所有 worker 退出
4. 超过 `timeout` 则 cancel 剩余 worker，记录未完成的任务数

## 4. Layer 2: TokenBucket（限流层）

### 4.1 设计

每个 model 名一个独立的 token bucket，挂在 LLMClient 上。调用 LLM 前先 `acquire()` 拿令牌，拿不到就 await。

```python
# src/llm_client/rate_limit.py

class TokenBucket:
    """异步令牌桶。每 (60/rpm) 秒补充一个令牌。"""

    def __init__(self, rpm: int):
        self.rpm = rpm
        self._tokens = asyncio.Semaphore(0)
        self._refiller: asyncio.Task | None = None

    async def start(self) -> None:
        interval = 60.0 / self.rpm
        self._refiller = asyncio.create_task(self._refill_loop(interval))

    async def stop(self) -> None:
        if self._refiller:
            self._refiller.cancel()

    async def _refill_loop(self, interval: float) -> None:
        while True:
            await asyncio.sleep(interval)
            self._tokens.release()            # 补充一个令牌

    async def acquire(self) -> None:
        await self._tokens.acquire()          # 无令牌时阻塞
```

### 4.2 集成到 LLMClient

```python
# src/llm_client/client.py（改造后）

class LLMClient:
    def __init__(self, settings, *, rate_limits: dict[str, int] | None = None):
        ...
        self._buckets: dict[str, TokenBucket] = {}
        self._rate_limits = rate_limits or {}

    async def _chat_messages(self, messages, *, model=None, ...):
        model = model or self.settings.model
        bucket = self._get_bucket(model)
        if bucket:
            await bucket.acquire()        # ← 等令牌，自动反压
        # ... 原有的 tenacity retry 逻辑不变
```

### 4.3 配置

```python
# 方式 1：构造时传入
client = LLMClient(settings, rate_limits={
    "deepseek-chat": 60,
    "deepseek-embedding": 120,
})

# 方式 2：环境变量（可选，未来扩展）
# LLM_RATE_LIMIT_deepseek_chat=60
# LLM_RATE_LIMIT_deepseek_embedding=120
```

未配置限速的 model 不限流（直接放行）。

### 4.4 为什么不放在 TaskQueue 里

| 关切 | 放在 TaskQueue | 放在 LLMClient |
|---|---|---|
| 换 provider 时 | 要改 queue 的限流配置 | 只改 client 配置 |
| 不用 queue 的调用方（如直接 `client.chat()`） | 享受不到限流 | 自动限流 |
| 职责 | queue 管调度，不该知道 model 名 | client 最清楚自己的限速 |
| 非 LLM 任务（如 CPU 密集） | 不需要 LLM 限流 | 不经过 bucket，无影响 |

核心原则：**限流跟着资源走，不跟着调度走**。LLM 是共享资源，限流在 LLMClient 上；worker pool 是调度资源，限流在 TaskQueue 上（通过 lane worker 数）。

## 5. 装饰器：非侵入接入

> **已删除（REMOVED，2026-10-17，见 §12 A-2 / §12.2）。** 本节整节为**历史设计记录**——`decorators.py`（`@queued` / `get_queue` / `set_default_queue`）已从代码库移除，不要照本节接入。删除理由：零生产消费者，且默认路径不可用（`get_queue()` 只建 lane、从不 `start()`，`submit()` 在 `_accepting=False` 时抛 `QueueClosed`）；用懒启动去补只会把显式崩溃换成"没人 stop、worker 泄漏"的隐式失败。受支持的接入面是直接构造 `TaskQueue` + `async with` 生命周期（§3.2 / §12.1）。

### 5.1 API 设计

```python
# src/task_queue/decorators.py

_default_queue: TaskQueue | None = None

def get_queue() -> TaskQueue:
    """进程-wide 默认队列。首次调用时按环境变量创建。"""

def queued(
    fn=None,
    *,
    lane: str = "default",
    retries: int = 3,
):
    """装饰器：把 async 函数提交到队列的指定 lane，而非直接执行。

    用法：
        @queued(lane="enrich", retries=3)
        async def enrich_one(chunk):
            ...

        # 方式 1：await 拿结果（和直接调用一样）
        result = await enrich_one(chunk)

        # 方式 2：fire-and-forget（提交即返回 Future）
        future = enrich_one.submit(chunk)
    """
```

### 5.2 非侵入性

装饰器不改变函数签名和返回值类型——被装饰的函数仍然 `await func(args)` 调用，只是底层从"直接执行"变成了"提交到 queue"。

改造前：
```python
async def enrich(self, chunks):
    for chunk in chunks:
        result = await self._enrich_one(chunk)   # 串行，直接调用
        self.store.write_inferred(chunk.chunk_id, result)
```

改造后：
```python
@queued(lane="enrich", retries=3)
async def _enrich_one(self, chunk):
    return await self.client.chat_json(...)

async def enrich(self, chunks):
    futures = [_enrich_one.submit(self, chunk) for chunk in chunks]
    for future in asyncio.as_completed(futures):
        result = await future
        self.store.write_inferred(result.chunk_id, result)
```

变化量：
- `_enrich_one` 加一行 decorator
- `enrich` 的 for 循环改成 gather/as_completed
- 业务逻辑（LLM 调用、结果处理）零改动

### 5.3 与现有 Semaphore 模式的关系

`pdf_summarizer` 的 `Semaphore + gather` 模式可以保持不变——`@queued` 是另一种选择，不是替代品。两者可以共存：

- `Semaphore + gather`：适合一次性批处理（如 summarize 的 map 阶段），不需要跨调用方隔离
- `@queued`：适合长期运行的后台任务（如 enrich），需要和其他任务类型隔离

## 6. 背压：端到端怎么工作

以 enrich 为例，完整调用链：

```text
  enrich_corpus()
    → 加载 unenriched chunks
    → 对每个 chunk 调用 _enrich_one.submit(chunk)
      │
      │ 如果 lane "enrich" 的 queue 已满（500 个待处理）
      │ → submit() 内部 await queue.put()  ← 背压点 ①
      │ → 调用方自然降速，不会 OOM
      │
      ▼
    lane "enrich" 的 worker 取任务
      → 调用 _enrich_one(chunk)
        → 调用 LLMClient.chat_json()
          → TokenBucket("deepseek-chat").acquire()
            │
            │ 如果令牌耗尽（60 RPM 已用完）
            │ → acquire() await  ← 背压点 ②
            │ → worker 阻塞，不再取新任务
            │ → queue 逐渐满 → 触发背压点 ①
            │
            ▼
          → 拿到令牌 → 发 HTTP 请求
          → tenacity retry（429/timeout/5xx，已有逻辑）
          ← 返回结果
        ← write_inferred(chunk_id, result)  ← per-chunk checkpoint
      ← worker 取下一个任务
```

两个背压点，层层传导，全程自动：
- **背压点 ①**（queue 满）：防止内存爆炸
- **背压点 ②**（token bucket）：防止打爆 LLM provider

## 7. 失败处理

### 7.1 任务级重试

每个 task 携带 `retries` 计数器。worker 捕获异常后：
- `retries < max_retries` → 重新入队（放回 lane queue 尾部）
- `retries >= max_retries` → `future.set_exception()`，计入 `failed`

这和 `llm_client` 的 tenacity retry 是**互补**的：
- tenacity：处理 HTTP 级瞬时错误（429、timeout、5xx），对调用方透明
- queue retry：处理业务级失败（如 DB 写入失败），可配置不同重试次数

### 7.2 Lane 级隔离

一个 lane 的任务全部失败不影响其他 lane——worker pool 独立，queue 独立。enrich 的 LLM 错误不会阻塞 query。

### 7.3 进程级崩溃

queue 本身不持久化——进程崩溃时 queue 中的待处理任务丢失。

这对 enrich 不是问题：per-chunk checkpoint 已经写入 `inferred` 表，重启后 `unenriched_child_chunk_ids()` 只返回未完成的。

如果未来需要持久化 queue（如跨进程的任务分发），那是另一个层级的设计，不在本方案范围内。

## 8. 包结构

```text
src/task_queue/
  __init__.py       # 导出 TaskQueue, LaneStats, QueueClosed
  service.py        # TaskQueue（含 async with 生命周期）, _Lane, _Task, LaneStats
```

（`decorators.py` 曾在此，已于 2026-10-17 按 §12.2 删除。）

```text
src/llm_client/
  client.py         # 改造：集成 TokenBucket.acquire()
  rate_limit.py     # 新增：TokenBucket 实现
  settings.py       # 不变
```

pyproject.toml `packages` 列表新增 `src/task_queue`。

## 9. 迁移路径

### Phase 1：基础层（无业务改动）

1. 实现 `src/task_queue/service.py`（TaskQueue + Lane）
2. 实现 `src/task_queue/decorators.py`（@queued）
3. 实现 `src/llm_client/rate_limit.py`（TokenBucket）
4. 改造 `src/llm_client/client.py`（集成 acquire）
5. 测试：`tests/test_task_queue/` + `tests/test_llm_client/test_rate_limit.py`

### Phase 2：enrich 迁移

1. `src/rag/enrich.py`：`_enrich_one` 加 `@queued(lane="enrich")`
2. `enrich_corpus()` 改为 submit + gather 模式
3. 测试：现有 enrich 测试不变（mock LLM），新增并发场景测试

### Phase 3：其他接入（按需）

- `pdf_summarizer`：可选择迁移到 `@queued`，或保持现有 Semaphore 模式
- `research_agent`：串行调用暂不需要，未来并发化时接入
- 新任务类型：直接 `@queued(lane="new_type")`

## 10. 明确不做的事

| 不做 | 原因 |
|---|---|
| 持久化 queue（Redis/DB） | 单进程 CLI 工具，per-chunk checkpoint 已解决崩溃恢复 |
| 跨进程任务分发 | 同上，不需要 Celery/ARQ |
| 动态 worker 数 | 固定 worker 数足够，KISS |
| 任务优先级（lane 内） | lane 内 FIFO 足够，优先级通过 lane 隔离实现 |
| 任务取消 API | enrich 的 checkpoint 模式不需要取消，重启即可 |
| 定时调度（cron） | 不在本层职责，CLI 入口或外部 cron 负责 |
| 限流 token 持久化 | 进程重启后限速器重置，provider 侧也重置，一致 |

## 11. 待确认

1. **Lane worker 数**：enrich 4 / query 6 的分配是否合理？是否需要根据 LLM provider 的 RPM 动态计算？
2. **默认 queue 实例**：用模块级 singleton（`get_queue()`）还是各项目自建？前者方便但隐式，后者显式但啰嗦。
3. **TokenBucket 初始令牌**：启动时桶是空的（需要等第一个令牌补充）还是预填？预填可以避免冷启动等待，但可能瞬间打满 provider。
4. **是否需要全局并发上限**：除了 lane 级 worker 数限制，是否还需要一个系统级总上限（如 `total_workers <= 20`）？

## 12. 采用裁决（2026-10-17）

贯穿 `task_queue` 与 `storage.migrate` 两个共享层的一条规则：**建在采用之前的层，只在有真实需求处推广；没有需求处，收缩到实际用到的面，或把触发条件写死在文档里——不要为了采用数字去迁移本来正确的代码。**（同一条规则也落在 `storage-usage-guide.md` §4 的采用裁决里。）下面把它落到本层的三处。

先厘清一个框定错误：限流早就共享了——它在 `llm_client` 的 token bucket 里（§4.4），不在本层。所以 `task_queue` 的边际价值只剩 **lane 隔离 + 任务级重试/退避**。"采用薄"不是这个层的缺口，而是它的正确作用域：大多数调用方要的是限流，那个它们已经免费拿到了，没必要再套一层 queue。

**A-1　不迁移 pdf_summarizer。** `@queued` 的适用域是"长期后台任务 + 需要和其他任务类型隔离"；pdf_summarizer 是一次性单文档 map-reduce，两个条件都不满足，手搓的 `Semaphore + gather` 是对的。§5.3 说的"两者共存"到此定案，不再是待选项。

**A-2　`@queued` 降级为未采用的实验面，不再是文档推荐的默认接入方式。** `decorators.py` 零生产消费者，且默认路径有一个生命周期缺陷：`get_queue()` 只构造 lane、从不 `start()`，而 `submit()` 在 `_accepting=False` 时抛 `QueueClosed`——挂默认单例的 `@queued` 一调用即失败（这也回答了 §11 待确认 Q2：单例方便，但当前实现下并不可用）。`service.py` 的直接构造是 rag/enrich 已验证的姿势，是本层唯一受支持的接入面。`@queued` 要留下，就得补上默认单例的懒启动并带来一个真实消费者；在出现真实 lane 隔离需求前，不投入修复，也不推荐。

**A-3　research_agent 的接入 gated 在预算重构之后。** `collector.py` 现在串行、内联 `calls_left` 预算检查，预算是"花掉才扣"的顺序语义；并行提交会让多个任务在扣减发生前一起越过上限。接入 `task_queue` 的前提是先把预算改成提交前预扣（reserve），否则并发直接击穿预算。在那之前串行是正确的，不推广。**预算重构（阶段 1，见 §12.4）已落地并测试；lane 接入（阶段 2–3）仍 gated，等真实需求信号。**

### 12.1 定位（坐实）

`task_queue` 是一个**调度层，坐在 `llm_client` 的限流之上**，不是通用后台框架：

- 受支持的接入面只有一种——**调用方自己构造、自己持有生命周期的 `TaskQueue`**（rag/enrich 的姿势）。层不持有进程级全局实例，不替调用方决定何时 start/stop。
- 分工不变：worker 取任务后调 `client.chat()`，token bucket 在 client 内部限流，背压沿 §6 的链条自然传导。`task_queue` 永远不感知 model 名。
- 它的作用域就是 **lane 隔离 + 任务级重试/退避**；限流那部分调用方已经从 `llm_client` 免费拿到，不必再套一层。

### 12.2 落地改动（已执行 2026-10-17）

裁决落地是一次**以减法为主**的改动，外加一个让"直接构造"足够安全的小 affordance。三步均已完成：

1. ✅ **给 `TaskQueue` 加异步上下文管理器**——`async with TaskQueue(...) as q:` 进入时 `start()`、退出时 drain + `stop()`（`service.py` 的 `__aenter__`/`__aexit__`）。这是唯一受支持的生命周期姿势，让每个消费者用同一套安全模板，而不必各自手搓 start/stop（漏掉 stop 就没有优雅 drain）。注意 lane 必须在进入 `async with` 之前声明（`lane()` 拒绝在 start 后运行）。rag/enrich 仍用等价的显式 start/stop，未强制改写。
2. ✅ **删掉进程级单例 `get_queue()` / `set_default_queue()` 与 `@queued` 装饰器**（`decorators.py` 及其测试整体移除）。单例的隐式生命周期正是 A-2 的 footgun；用懒启动去补只会把"一调就崩"的显式失败换成"没人 stop、worker 泄漏到进程退出"的隐式失败，更糟。当前没有第二个真实消费者，删除即"收缩到实际用到的面"。
3. ✅ **同步收尾**：`__init__.py` 只导出 `TaskQueue` / `LaneStats` / `QueueClosed`；本文 §5 降级为历史设计记录、§8 包结构去掉 `decorators.py`；AGENTS.md 的 `task_queue` 行改为"已删除"表述。

**反向触发条件（诚实标注不确定）**：若将来真出现第二个需要 lane 隔离的消费者，重新引入的应是"接收一个已 `start()` 的显式 `queue=` 的装饰器 + 上下文管理器"，而**绝不**是进程级默认单例——即第 2 步删掉的那个形状不再回来。

### 12.3 与其他模块的合作（本裁决不改变的部分）

- **`llm_client`**：合作契约不变（见 §4.4 / 12.1）。worker → `chat()` → token bucket → 背压，`task_queue` 保持 model-agnostic。
- **`pdf_summarizer`**：不合作，保持本地 `Semaphore`（A-1）。
- **`research_agent`**：唯一的真实集成工作，且**不在 queue 本身**——是预算语义从 spend-then-deduct 改成 reserve-before-submit（A-3）。独立、gated；在此之前串行。细化见 §12.4。

## 12.4 A-3 细化：reserve-before-submit 与并发化的三处击穿点

A-3 的原裁决只点了一处前置（预算改成预扣）。实际读代码后确认：**把 `collector.py` 并发化会同时击穿三处共享状态，预算只是第一处**，另两处不在 queue 层，但同属这次集成的前置工作。任何一处漏掉都是静默错误（超预算 / 撞 id / 结果不可复现），不是崩溃，所以更危险。本节把三处、预算 guard 的设计、以及分阶段方案写死，供将来触发时直接执行，不必重新推导。

### 为什么串行现在是对的

`collector.run_batch` 是一个 `for sq in sub_queries` 的顺序循环，三个计费单元（search / fetch / extract）前各有一道 `if out.calls_used >= calls_left` 闸门。**全程单协程，check 与 `calls_used += 1` 之间没有 `await`，所以 check-then-act 天然原子**——这正是"串行正确、并发不正确"的根因。并发化不是给循环套个 `gather` 那么简单，它打破了这条隐式的原子性保证。

### 三处击穿点

| # | 击穿点 | 机制 | 后果 | 归属阶段 |
|---|---|---|---|---|
| 1 | 预算 TOCTOU | worker 先判 `calls_used < calls_left` 通过，再 `await` 网络/LLM，回来才扣减。N 个 worker 在任何人扣减前一起读到"还有余量"、一起放行 | 越过 `calls_left` 上限（每 query 最坏 7 次调用：1 search + 3×(fetch+extract)） | 阶段 1（已落地） |
| 2 | store 的 finding-id TOCTOU | `allocate_finding_ids` 在**一个独立 session** 里 `SELECT max(F*)` 就返回，真正写入在之后的 `add_findings` 另一个 session。两个 worker 并发 allocate 读到同一个 max | 分配出**相同的 F\* id**；F\* 出现在用户可见引用里且"stable once written"，撞 id = 引用损坏 | 阶段 2 |
| 3 | dedup + 聚类的可复现性 | 今天 fetch→查 dedup→**命中就跳过 extract**（省 LLM 预算）是一条串行优化；`SupportClusterer.key_for` 是跨 query 的共享可变状态 | 并发下同一 URL 双双 miss dedup、都去 extract（白烧预算）；`support_key` 分配顺序不确定，威胁 replay 可复现承诺（machine 05 §5.3） | 阶段 2 |

注意击穿点 1 与 `llm_client/rate_limit.py` 的 token bucket 是**两件事**：token bucket 管 RPM（速率），预算 guard 管总次数（计数）。lane worker 数 + token bucket 不解决总量封顶，两者正交，都要有。

击穿点 2 里 `add_capture` 反而安全——它的读-改-写在同一事务内，SQLite 写锁会把两个并发 add_capture 串行化，第二个能看到第一个已提交的行。不安全的只有 finding 这条"allocate 与 write 分处两个事务"的路径。

### 预算 guard 设计（阶段 1，已落地）

把扣减从 `await` **之后**挪到**之前**，封装成一个内部无 `await` 的原子计数器（asyncio 单线程下即原子）。粒度选**每调用细粒度**，因为它是现有 `calls_used >= calls_left` 闸门的直接并发安全翻译，能原样保留三条"cut short by budget"失败语义，不需要 coarse 方案（预扣 7、事后退还余量）的记账：

```python
class CallBudget:
    """Concurrency-safe total-call cap. reserve() has no await inside, so
    check-then-decrement is atomic under asyncio — the property the serial
    loop got for free and concurrency would otherwise lose (A-3 breach #1)."""

    def __init__(self, total: int) -> None:
        self._remaining, self._used = total, 0

    def reserve(self, n: int = 1) -> bool:
        if self._remaining < n:
            return False
        self._remaining -= n
        self._used += n
        return True

    def release(self, n: int = 1) -> None:   # 细粒度不需要，coarse 方案才用
        self._remaining += n
        self._used -= n

    @property
    def used(self) -> int:
        return self._used
```

`run_batch` 用 `calls_left` 播种一个 `CallBudget`，三个计费点改成 `if not budget.reserve(): 记 SourceFailure; return`；步骤结束 `out.calls_used = budget.used`。**machine 的提交路径与"预算与产物同事务提交"的不变量（machine.py docstring / 05 §4）完全不动**——guard 只在单个 step 生命周期内存在，step 跑完才提交、下一个 step 再重新派生 `collect_left`，并发只发生在 step 内，没有跨 step 竞态。`_schedule` 里那个 `7 * len(scheduled)` 的 room 估算正好印证 7/query 的最坏值，与 guard 粒度自洽。

阶段 1 在**串行下行为等价**：现有 `test_machine_e2e.py` 全绿即回归证明；另加一个用假 adapter 制造并发的单测，断言 N 个并行 sub-query 也不越过 `calls_left`（把 guard 的原子性测死，而不是靠"反正现在还是串行"）。

### 架构定调：并行只做 I/O，串行做状态写入（阶段 2）

击穿点 2、3 的解法不是给 store 和 clusterer 加锁，而是**收窄并发的作用域**：让 worker 只做 search/fetch/extract（纯网络+LLM，受 `CallBudget` 约束、返回纯数据），把 `add_capture` / `allocate_finding_ids` / `add_findings` / `clusterer.key_for` **全部收回驱动协程串行执行**（按提交顺序合并 future，而非 `as_completed`，以保 dedup 与聚类的确定性）。这样三个问题一次性消失，而并行化的恰好是占 99% 墙钟时间的网络+LLM 部分。

需要处理的顺序依赖：dedup-before-extract。今天"先去重再抽取"省的是 LLM 预算，并发下要么保留一个原子 in-flight URL/hash 集合让并发 worker 互相去重，要么接受去重退化到驱动侧（会多花已 reserve 的 extract 调用）。这个取舍留到阶段 2 触发时再定，本节只标注它存在。

### 阶段划分与触发条件

| 阶段 | 内容 | 状态 | 触发条件 |
|---|---|---|---|
| 0 | 本节裁决（docs-first） | ✅ 本次 | — |
| 1 | `CallBudget` + collector 三处闸门改 reserve()；串行等价 + 并发单测 | ✅ 本次 | 纯收益，即使不接 queue 也让预算语义变健壮，无投机 |
| 2 | 切开 I/O 与状态写入（解击穿点 2、3） | ⏸ gated | 出现真实墙钟延迟需求信号 |
| 3 | 接 `collect` lane（照 rag/enrich 直接构造范式，worker 3–4，RPM 由 token bucket 兜底）+ wallclock 串/并对比证据 | ⏸ gated | 阶段 2 完成后 |

**为什么 2–3 gated 而 1 现在做**：阶段 1 是把一个隐式的正确性保证（串行原子）变成显式的（reserve guard），这是纯健壮性收益、且是 A-3 的命名前置，不引入任何投机泛化。阶段 2–3 是真正的并发化，只有在"一次真实 research session 的墙钟延迟变得难受"或"单批 sub_query 数量增长到串行不可接受"时才值得——在那之前串行是正确的，不推广（同 §12 主规则）。
