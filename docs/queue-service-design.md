# Queue Service 共享层设计

> 一句话核心：`src/task_queue` 是通用异步任务调度层——分 lane 的任务队列管"谁先做、能做几个"，LLMClient 内的 token bucket 管"下游不能打爆"。两层各管各的，通过背压自然串联。装饰器是非侵入的接入方式：业务函数不变，`@queued` 只改变调度方式。

日期：2026-09-21。状态：**设计阶段，尚未实现**。

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
  __init__.py       # 导出 TaskQueue, queued, get_queue
  service.py        # TaskQueue, _Lane, _Task, LaneStats
  decorators.py     # @queued, get_queue() 默认实例
```

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
