# Skill Manager 使用指南（`src/skill_manager/` + `skills` CLI）

> 一句话核心：外部 AI skill 仓库不再手工搬运——本机只留一份进 git 的**声明**（哪个仓库、锁哪个 commit、装哪些），其余全部由 `uv run skills sync` 重建。任何一台干净机器 clone 本仓库后跑一条命令，得到的 skill 目录逐字节相同。

日期：2026-09-21。适用代码：`src/skill_manager/`（`sources.py` / `vendor.py` / `install.py` / `cli.py`），测试 `tests/test_skill_manager/`（20 项，全部离线）。设计缘由见 AGENTS.md "AI skills & specs"；本文讲的是怎么用、出错怎么办。

## 0. 一屏图：真源在内，生成物在外

```
                进 git（唯一真源）                        gitignored（随时可重建，禁止手改）
   ┌──────────────────────────────────────┐
   │ src/skill_manager/sources.py         │   vendor.ensure()    ┌─────────────────────────┐
   │   UPSTREAMS = 4 条，每条锁 40 位 sha  ├─────────────────────►│ repo-skills/<clone>/    │
   │   TARGET_DIRS = (.opencode/skills,)  │   clone + detached   │  = 上游镜像 @ pin        │
   └──────────────────────────────────────┘     checkout         └────────────┬────────────┘
   ┌──────────────────────────────────────┐                                   │ 扫 */SKILL.md
   │ skills/<install-name>/SKILL.md       │                                   │ 按 select 过滤、
   │   我们自写的 skill + 覆盖层            │                                   │ 按 prefix 命名
   └──────────────────┬───────────────────┘                                   ▼
                      │  install.planned()：上游层打底，本地层同名覆盖 ──►  一份计划 list[Skill]
                      ▼
              install.install()：整目录复制，计划外的名字直接删掉  ──►  .opencode/skills/<name>/  （31 个）
```

三段职责，对应三个模块：`sources.py` 说"要什么"，`vendor.py` 说"怎么把上游弄到本地"（整个包唯一碰 git 的地方），`install.py` 说"怎么把要的东西摆到工具面前"。`cli.py` 只做参数到这三者的映射，以及把异常翻译成退出码。

## 1. 四条命令

| 命令 | 做什么 | 什么时候用 |
| --- | --- | --- |
| `uv run skills sync` | 补齐缺失的 clone → 把它们钉到声明的 commit → 按计划重建每个目标目录（并剪掉不再声明的名字） | 拉到新仓库、改了 `UPSTREAMS`、或发现 `.opencode/skills/` 和声明不一致 |
| `uv run skills list` | 每个上游的 pin / 实际 checkout / 未提交改动数 / 技能数，加覆盖关系和已装名字清单 | 想知道"现在装的到底是什么" |
| `uv run skills outdated` | `git fetch` 后报告每个 pin 落后上游默认分支多少 commit、动了哪些 skill 目录 | 想升级之前先看一眼 |
| `uv run skills add <repo>` | 克隆一个新上游，打印该粘进 `UPSTREAMS` 的那段代码 | 想引入别人维护的一批 skill |

`sync --no-provision` 跳过所有 git 操作，只按 clone 目录当前的样子重建目标——离线、或临时试验某个未提交的 skill 时用。

`skills list` 的真实输出（本机当前状态）：

```
upstream     pin       clone     edited  skills
openspec     0928258   0928258   -       3
superpowers  6efe32c   6efe32c   -       14
karpathy     2c60614   2c60614   -       1
google       6f0b877   6f0b877   -       13
local        -         -         -       0

overrides from the tracked skills/ directory:
  openspec-apply replaces the openspec version
  openspec-archive replaces the openspec version
  openspec-proposal replaces the openspec version

.opencode\skills: 31
  openspec-proposal
  superpowers-brainstorming
  ...
```

两列计数法则要说清楚：`skills` 列算的是**这个上游贡献了几个 skill**，被本地覆盖掉的也算它的——否则上游被全部覆盖时这行会显示 0，看着像它什么都没提供。同一条"这个上游有没有交付东西"的判断也用在 §4 那条中止保护上（一个真正扫不出 skill 的上游会让整条 `sync` 失败，免得把它的名字全剪掉）；`local` 行只算**带来新名字**的第一方 skill，覆盖型的在下面的 overrides 清单里点名。

`edited` 列不为 `-` 说明某个 clone 里有未提交改动：它们**会**被原样装出去，但仓库里没有任何地方记录它们，换台机器就没了。看到这个数字就该处理（见 §3 场景 C）。

## 2. Upstream 的六个字段

```python
"superpowers": Upstream(
    repo="https://github.com/obra/superpowers",   # 从哪来
    commit="6efe32c9e2dd002d0c394e861e0529675d1ab32e",  # 锁死在这一个快照
    clone="repo-skills/superpowers",              # 克隆到仓库根的哪里
    skills_dir="skills",                          # 仓库里"一个 skill 一个文件夹"的那层子目录
    select="",                                    # 只要名字以此为前缀的 skill（openspec 用这个）
    prefix="superpowers",                         # 装出去时加的名字前缀（避免撞名）
),
```

- 装出去的最终名字 = `prefix` + `-` + 文件夹名（无 prefix 就是文件夹名）。`select` 在加前缀**之前**过滤。
- `commit` 必须是完整 sha，不能是分支名：上游推东西就不能悄悄改变本机装到的内容——这条和 `ocr_backend.models` 的模型快照是同一个姿势。
- 两个上游会装出同一个名字时，`planned()` 直接报错让你给其中一个加 `prefix`，不会先到先得。
- 只有目录里存在 `SKILL.md` 才算一个 skill；clone 里的 README、模板、脚本都会被忽略。

## 3. 场景手册

**A. 新机器**：`uv sync` 之后 `uv run skills sync`。四个 clone 会按 pin 重新克隆，`.opencode/skills/` 重新生成。已验证过：删掉 clone 再 sync，能原位复原。

**B. 加一个新上游**：`uv run skills add https://github.com/x/y --skills-dir skills --prefix y`，把打印出来的条目粘进 `sources.UPSTREAMS`，然后 `uv run skills sync`，把改动（含 pin）一起提交。

**C. 想改一个已经装上的 vendored skill**：**别去改 `repo-skills/` 里的 clone**，也别说改 `sync` 会迁就你——`sync` 一旦发现 clone 有未提交改动且需要移动它，会直接报错拒绝，以免你的改动被静默丢弃。正确做法：把那个 skill 整个文件夹复制到仓库根的 `skills/` 下，**用它安装时的名字**（含上游前缀），例如 `skills/superpowers-brainstorming/`，然后在那儿改。这层覆盖进 git，`install.planned()` 会让它盖掉同名上游 skill，`skills list` 会列出覆盖关系。仓库里已有三个现成的样子可抄：`skills/openspec-proposal/`、`-apply/`、`-archive/`（见 §6）。
代价要说清楚：覆盖是**整目录替换**，覆盖期间上游对那个 skill 的后续更新不再生效；pin 升级后如果还想要上游的新变化，得重新复制一次再叠上你的改动。（和 `ocr_review` 的 sidecar 同理：机器产物保持纯净，差异单独可审。）

**D. 我们自己写一个 skill**：放进 `skills/<name>/SKILL.md`，名字不用加前缀（那是我们的地盘），跑 `uv run skills sync`。

**E. 升级某个上游**：`uv run skills outdated` 看落后多少、哪些 skill 动了 → `git -C repo-skills/<clone> log --oneline <pin>..<tip>` 自己读一遍 → 把 `sources.py` 里的 `commit` 换成新 sha → `uv run skills sync` → 提交。一个上游更新 = 一次改 `commit` + 一次 sync，**绝不是 `git pull`**：pull 会让这台机器持有仓库不知道的状态。

**F. 换/加一个 agent 工具**：它在哪个目录读 skill，就往 `sources.TARGET_DIRS` 里加一条相对路径。`sync` 会把同一份计划复制到每个目标；被覆盖的目录同样会被剪枝。

## 4. 报错与告警对照

| 输出 | 含义 | 处理 |
| --- | --- | --- |
| `no skills found for: xxx -- check clone, skills_dir, select` | 某个上游一个 skill 都没扫出来 | 检查 `skills_dir` 路径和 `select` 前缀；**这条必须当真**：它防的就是"上游空了还照装，结果把那个上游的 skill 全剪没了" |
| `<a> comes from both <b> and <c> — give one of them a prefix` | 两个上游撞名 | 给其中一个 `Upstream.prefix` |
| `repo-skills/x has N local edit(s) … would displace them -- move the difference into skills/ as an override, or fork the upstream` | clone 被手改过，且 pin 要求移动它 | 按 §3 场景 C 把差异搬进 `skills/`；差异值得回馈上游就 fork，并把该条目的 `repo` 指向 fork |
| `warning: … uncommitted edit(s) … recorded nowhere` | 改动会被装出去，但仓库不知道 | 同上，别留给下一台机器 |
| `git clone …: fatal: repository not found` | URL 错 / 没有网络 / 私有仓库没权限 | 检查 `repo`；`sync` 里某个上游失败会中止整条命令，不会半装 |
| `git checkout <sha>: reference is not a tree` 之后又成功 | clone 是浅的或旧的 | `ensure()` 已经自己 `fetch`（必要时 `--unshallow`）重试一次，不用管 |

## 5. 几个刻意的设计取舍

- **为什么锁 commit 而不是分支 / tag**：可复现，且上游推东西不会改变已提交的状态——和 `uv.lock` 是同一件事。看更新是 `outdated` 的职责，不是安装的职责。
- **为什么复制而不是软链**：目标目录必须对一个完全不知道 git 存在的 agent 工具有效；链接还把 `repo-skills/` 的结构变成契约的一部分。
- **为什么"剪枝"算安装的一部分**：把目标目录建成计划的**完整视图**，才能顺手解决上游改名留下的僵尸目录——旧 copy 和新 copy 并排存在，正是手工同步的布局不可信的原因。
- **为什么一个 clone 有本地改动时要报错而不是覆盖**：这台机器上的未提交改动对 git 不可见，`checkout` 会静默吃掉它们。宁可停下来让人决定搬去哪。
- **为什么扫描靠 `SKILL.md` 而不是清单**：判据来自被 vendored 仓库自己的约定，上游加/删 skill 不需要改本仓库任何代码。

## 6. 与 `specs/` 的关系

真正会写 spec 的只有 5 个：`openspec-proposal` / `-apply` / `-archive`（SDD 工作流）和 `superpowers-brainstorming` / `-writing-plans`。

上游那三个 openspec skill 把输出写死在 `openspec/changes/{change-id}/`，而本仓库的证据目录是 `specs/`——这正是 §3 场景 C 那层覆盖的第一批住户：`skills/openspec-{proposal,apply,archive}/` 是上游原件的副本，里面 26 处路径引用统一加了 `specs/` 前缀，于是 `openspec/changes/…` → `specs/openspec/changes/…`、`openspec/specs/` → `specs/openspec/specs/`、`openspec/archive/` → `specs/openspec/archive/`。效果是 skill 一跑就直接落在进 git 的证据目录里，仓库根再也不会冒出一个 `openspec/`，也不需要谁记得事后搬运。
副作用要说在前面：上游那份仍然写 `openspec/…`，`specs/openspec/` 只是被我们的副本改出来的形状；pin 升级后若想要上游对这三个 skill 的新改动，得重新复制一次再叠上这段路径重定向（`skills outdated` 会告诉你它们有没有动）。`superpowers-*` 的产物没有硬编码路径，仍由人归档到 `specs/superpowers/`。

一句话分清归属：`skills/` 和 `sources.py` 是**源头**，进 git；`repo-skills/`（clone）和 `.opencode/skills/`（安装）是**产物**，gitignored，任何时候删掉都能用 `skills sync` 重建；`specs/` 是这些 skill 做出来的成果，永久留存。

重定向之前手工搬进来的那一份（`specs/pdf-summarizer-agent/`）保持原样，不要往 `specs/openspec/` 里迁——按变更命名的历史产物，形状不同不是问题。
