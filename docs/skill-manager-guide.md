# Skill Manager 使用指南（`src/skill_manager/` + `skills` CLI）

> 一句话核心：外部 AI skill 仓库不再手工搬运——本机只留一份进 git 的**声明**（哪个仓库、锁哪个 commit、装哪些），其余全部由 `uv run skills sync` 重建。任何一台干净机器 clone 本仓库后跑一条命令，得到的 skill 目录逐字节相同。

日期：2026-09-21（§5 目标目录那条 as-of 2026-10-29）。适用代码：`src/skill_manager/`（`sources.py` / `vendor.py` / `install.py` / `cli.py`），测试 `tests/test_skill_manager/`（21 项，全部离线；重跑：`uv run pytest tests/test_skill_manager -q`）。设计缘由见 AGENTS.md "AI skills & specs"；本文讲的是怎么用、出错怎么办。

## 0. 一屏图：真源在内，生成物在外

```
                进 git（唯一真源）                        gitignored（随时可重建，禁止手改）
   ┌──────────────────────────────────────┐
   │ src/skill_manager/sources.py         │   vendor.ensure()    ┌─────────────────────────┐
   │   UPSTREAMS = 4 条，每条锁 40 位 sha  ├─────────────────────►│ repo-skills/<clone>/    │
   │   TARGET_DIRS = (.agents/skills,)    │   clone + detached   │  = 上游镜像 @ pin        │
   └──────────────────────────────────────┘     checkout         └────────────┬────────────┘
   ┌──────────────────────────────────────┐                                   │ 扫 */SKILL.md
   │ skills/<install-name>/SKILL.md       │                                   │ 按 select 过滤、
   │   我们自写的 skill + 覆盖层            │                                   │ 按 prefix 命名
   └──────────────────┬───────────────────┘                                   ▼
                      │  install.planned()：上游层打底，本地层同名覆盖 ──►  一份计划 list[Skill]
                      ▼
              install.install()：整目录复制，计划外的名字直接删掉  ──►  .agents/skills/<name>/  （31 个）
```

三段职责，对应三个模块：`sources.py` 说"要什么"，`vendor.py` 说"怎么把上游弄到本地"（整个包唯一碰 git 的地方），`install.py` 说"怎么把要的东西摆到工具面前"。`cli.py` 只做参数到这三者的映射，以及把异常翻译成退出码。

## 1. 四条命令

| 命令 | 做什么 | 什么时候用 |
| --- | --- | --- |
| `uv run skills sync` | 补齐缺失的 clone → 把它们钉到声明的 commit → 按计划重建每个目标目录（并剪掉不再声明的名字） | 拉到新仓库、改了 `UPSTREAMS`、或发现 `.agents/skills/` 和声明不一致 |
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

.agents\skills: 31
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

**A. 新机器**：`uv sync` 之后 `uv run skills sync`。四个 clone 会按 pin 重新克隆，`.agents/skills/` 重新生成。已验证过：删掉 clone 再 sync，能原位复原。

**B. 加一个新上游**：`uv run skills add https://github.com/x/y --skills-dir skills --prefix y`，把打印出来的条目粘进 `sources.UPSTREAMS`，然后 `uv run skills sync`，把改动（含 pin）一起提交。

**C. 想改一个已经装上的 vendored skill**：**别去改 `repo-skills/` 里的 clone**，也别说改 `sync` 会迁就你——`sync` 一旦发现 clone 有未提交改动且需要移动它，会直接报错拒绝，以免你的改动被静默丢弃。正确做法：把那个 skill 整个文件夹复制到仓库根的 `skills/` 下，**用它安装时的名字**（含上游前缀），例如 `skills/superpowers-brainstorming/`，然后在那儿改，最后在 `sources.OVERRIDE_BASES` 里记一行它是从哪个 commit 剪出来的（§7）。这层覆盖进 git，`install.planned()` 会让它盖掉同名上游 skill，`skills list` 会列出覆盖关系。仓库里已有三个现成的样子可抄：`skills/openspec-proposal/`、`-apply/`、`-archive/`（见 §6）。
代价要说清楚：覆盖是**整目录替换**，覆盖期间上游对那个 skill 的后续更新不再生效；pin 升级后如果还想要上游的新变化，得重新复制一次再叠上你的改动。"上游已经走过去了"这件事由 `sync` 警告，不用自己盯（§7）。（和 `ocr_review` 的 sidecar 同理：机器产物保持纯净，差异单独可审。）

**D. 我们自己写一个 skill**：放进 `skills/<name>/SKILL.md`，名字不用加前缀（那是我们的地盘），跑 `uv run skills sync`。

**E. 升级某个上游**：`uv run skills outdated` 看落后多少、哪些 skill 动了（其中哪些是覆盖会被点名）→ `git -C repo-skills/<clone> log --oneline <pin>..<tip>` 自己读一遍 → 把 `sources.py` 里的 `commit` 换成新 sha → `uv run skills sync` → 提交。`sync` 这时会替你检查覆盖有没有被落下（§7）。一个上游更新 = 一次改 `commit` + 一次 sync，**绝不是 `git pull`**：pull 会让这台机器持有仓库不知道的状态。

**F. 换/加一个 agent 工具**：先查它在哪个目录读 skill——大概率就是 `.agents/skills/`，那就**什么都不用做**（为什么见 §5 那条"目标目录只有一个"）。只有当某个工具坚持自己的私有路径时，才往 `sources.TARGET_DIRS` 里加一条相对路径；`sync` 会把同一份计划复制到每个目标，每个目标同样按计划剪枝。

加之前必须过一道检查：**没有任何一个 harness 同时扫到两个装着内容的根。** 多份拷贝本身不是问题——它们由同一条命令、同一份计划生成，彼此不可能不一致；问题是重名。OpenCode 项目级同时扫 `.opencode/skills`、`.claude/skills`、`.agents/skills` 三个根，并要求 skill 名跨根唯一，所以再加一条 `.claude/skills` 等于把 31 个名字各送它两份。`.qoder/skills` 不在这三个根里，是目前唯一可以安全新增的。

## 4. 报错与告警对照

| 输出 | 含义 | 处理 |
| --- | --- | --- |
| `no skills found for: xxx -- check clone, skills_dir, select` | 某个上游一个 skill 都没扫出来 | 检查 `skills_dir` 路径和 `select` 前缀；**这条必须当真**：它防的就是"上游空了还照装，结果把那个上游的 skill 全剪没了" |
| `<a> comes from both <b> and <c> — give one of them a prefix` | 两个上游撞名 | 给其中一个 `Upstream.prefix` |
| `repo-skills/x has N local edit(s) … would displace them -- move the difference into skills/ as an override, or fork the upstream` | clone 被手改过，且 pin 要求移动它 | 按 §3 场景 C 把差异搬进 `skills/`；差异值得回馈上游就 fork，并把该条目的 `repo` 指向 fork |
| `warning: … uncommitted edit(s) … recorded nowhere` | 改动会被装出去，但仓库不知道 | 同上，别留给下一台机器 |
| `no recorded base for override: xxx -- add it to sources.OVERRIDE_BASES` | `skills/` 下有个覆盖，但没记它是从哪个 commit 剪出来的 | 补一行出身（§7）。**这条必须当真**：没有 base 就无法判断漂移，这份覆盖从此脱离检测 |
| `warning: override xxx was cut from <base> but <upstream>'s copy has changed by <pin>` | pin 已经走过这份覆盖的出身，且上游那个 skill 文件夹的内容确实变了 | 重新复制上游版本、再叠上你的改动，然后把 base 改成新 pin（§7）。刻意要留住旧行为就忽略这次警告，但别忽略第二次 |
| `git clone …: fatal: repository not found` | URL 错 / 没有网络 / 私有仓库没权限 | 检查 `repo`；`sync` 里某个上游失败会中止整条命令，不会半装 |
| `git checkout <sha>: reference is not a tree` 之后又成功 | clone 是浅的或旧的 | `ensure()` 已经自己 `fetch`（必要时 `--unshallow`）重试一次，不用管 |

## 5. 几个刻意的设计取舍

- **为什么锁 commit 而不是分支 / tag**：可复现，且上游推东西不会改变已提交的状态——和 `uv.lock` 是同一件事。看更新是 `outdated` 的职责，不是安装的职责。
- **为什么复制而不是软链**：目标目录必须对一个完全不知道 git 存在的 agent 工具有效；链接还把 `repo-skills/` 的结构变成契约的一部分。
- **为什么目标目录只有 `.agents/skills/` 一个**（as-of 2026-10-29）：它是 Agent Skills 开放标准（agentskills.io）的中立路径，一个目标就喂饱四个 harness——OpenCode / Codex CLI / DeepSeek Harness 按各自官方文档原生扫它，Qoder 是本机实测的：临时建一个 `zz-harness-scan-probe/` 探针技能，下一轮它就出现在 Qoder 的可用技能清单里，`sync` 把它剪掉之后下一轮又消失；反向对照来自同一次会话的开头——那 31 个 skill 当时还躺在 `.opencode/skills/`，Qoder 一个都没列出来。所以这次替换不是"多加一个目标"，是把只服务 OpenCode 的私有路径换成服务四个工具的中立路径，`TARGET_DIRS` 的长度没变。
  顺带一条可复用的方法：**判断某个 harness 到底扫哪个目录，别只读文档下结论**——文档这次就是过期的（`cj9208.github.io` 仓库 2026-10-26 的 harness 调研里记着"Qoder 不读 `.agents/skills`"，那份文档自己写明失效条件是"任一 harness 的路径约定发生变化"，现在就是）。建一个一次性探针技能，看它出不出现，**两个方向都验**（建了出现、删了消失），比读文档硬。
- **为什么"剪枝"算安装的一部分**：把目标目录建成计划的**完整视图**，才能顺手解决上游改名留下的僵尸目录——旧 copy 和新 copy 并排存在，正是手工同步的布局不可信的原因。
- **为什么一个 clone 有本地改动时要报错而不是覆盖**：这台机器上的未提交改动对 git 不可见，`checkout` 会静默吃掉它们。宁可停下来让人决定搬去哪。
- **为什么扫描靠 `SKILL.md` 而不是清单**：判据来自被 vendored 仓库自己的约定，上游加/删 skill 不需要改本仓库任何代码。
- **为什么两个生成视图必须待在 index 之外**：`.gitignore` 只对**未跟踪**文件生效——某条路径一旦在加规则之前进过 git，之后每次 `sync` 重写它都会产生一次真实 diff，看起来像有人在手改生成物，而设计的前提是这些目录随时可删可重建。自查一行：`git ls-files repo-skills .agents .opencode` 应当什么都不输出；输出就说明有漏网的，用 `git rm --cached <路径>` 请出去（文件留在磁盘上，`skills sync` 本来就重建得出来）。本仓库当年的 `.opencode/skills/openspec-{apply,archive,proposal}/SKILL.md` 就是这么三个历史遗留（早于 `.gitignore` 规则），已按此取消跟踪；那三个文件如今随目标目录一起搬到了 `.agents/skills/` 下，仍然不在 index 里。
  忽略规则写的是 `.agents/skills/` 而不是整个 `.agents/`：被生成的只有 `skills/` 这一层，忽略整个目录会在日后有人往 `.agents/` 下放一个**想进 git** 的文件时把它静默吃掉。

## 6. 与 `specs/` 的关系

真正会写 spec 的只有 5 个：`openspec-proposal` / `-apply` / `-archive`（SDD 工作流）和 `superpowers-brainstorming` / `-writing-plans`。

上游那三个 openspec skill 把输出写死在 `openspec/changes/{change-id}/`，而本仓库的证据目录是 `specs/`——这正是 §3 场景 C 那层覆盖的第一批住户：`skills/openspec-{proposal,apply,archive}/` 是上游原件的副本，里面 26 处路径引用统一加了 `specs/` 前缀，于是 `openspec/changes/…` → `specs/openspec/changes/…`、`openspec/specs/` → `specs/openspec/specs/`、`openspec/archive/` → `specs/openspec/archive/`。效果是 skill 一跑就直接落在进 git 的证据目录里，仓库根再也不会冒出一个 `openspec/`，也不需要谁记得事后搬运。
副作用要说在前面：上游那份仍然写 `openspec/…`，`specs/openspec/` 只是被我们的副本改出来的形状；pin 升级后若想要上游对这三个 skill 的新改动，得重新复制一次再叠上这段路径重定向。这三份副本何时算落后、由谁提醒，见 §7——不靠人记。`superpowers-*` 的产物没有硬编码路径，仍由人归档到 `specs/superpowers/`。

一句话分清归属：`skills/` 和 `sources.py` 是**源头**，进 git；`repo-skills/`（clone）和 `.agents/skills/`（安装）是**产物**，gitignored，任何时候删掉都能用 `skills sync` 重建；`specs/` 是这些 skill 做出来的成果，永久留存。

重定向之前手工搬进来的那一份（`specs/pdf-summarizer-agent/`）保持原样，不要往 `specs/openspec/` 里迁——按变更命名的历史产物，形状不同不是问题。

## 7. 覆盖层的漂移检测

§3 场景 C 那句"覆盖期间上游对那个 skill 的后续更新不再生效"，在很长一段时间里只是**写下来**的一句话，没有任何机制支撑——而它恰好是这个仓库里最容易被静默违反的规则：违反它不报错、不失败，只是某天你发现上游的修复没生效，而你不知道是从哪一次 pin 升级开始的。

原来那句"`skills outdated` 会告诉你它们有没有动"只说对了一半，漏掉的正好是会咬人的那一半。`outdated` 报的是**上游动了什么**，而且只在 pin 落后于 tip 时报。覆盖的处境不同：它是某一时刻从 clone 里**复制**出来的副本。`sources.py` 的 pin 一推进、`sync` 一跑，`outdated` 就开始说 up to date，而 `skills/` 下那份副本还是从旧 pin 剪出来的，此刻正盖住上游的新版本——并且再没有任何东西会提起这件事。信号只在升级**之前**亮，升级**之后**永久熄灭。这是最坏的一种时间形状：它在你最需要它的那一刻消失。

补法是让覆盖记住自己的出身。`sources.OVERRIDE_BASES` 把"安装名 → 剪这份副本时上游所在的 commit"和 pin 并排写进 git：

```python
OVERRIDE_BASES: dict[str, str] = {
    "openspec-proposal": "092825896ed788b40c7ea987c17f1c5f75ed843a",
    ...
}
```

有了出身，"这份覆盖过期了吗"就变成一个能离线回答的问题。

**判定用的是内容，不是版本号相等。** base 和当前 pin 不同并不等于覆盖过期——pin 可能因为同一上游里别的 skill 动过而推进，被覆盖的那个文件夹一个字节都没变。所以真正的判据是：上游那**一个 skill 文件夹**的内容在 base 和 pin 两个版本之间有没有差异（`vendor.folder_differs`）。两个 revision 都已经在 clone 的历史里（`clone()` 是全量克隆），所以这一步不联网、不 fetch。用版本号相等去判定会狼来了，而一个会狼来了的警告比没有警告更糟——它教会读者忽略这一行。

**三个触发点，各自回答一个不同的问题：**

| 命令 | 回答的问题 | 动作 |
| --- | --- | --- |
| `skills sync` | 我现在装出去的这份覆盖，是不是正盖住一个已经变了的上游版本 | 内容漂移 → stderr 警告；`OVERRIDE_BASES` 查无此项 → **中止** |
| `skills outdated` | 上游动过的那些文件夹里，哪些是覆盖 | 在 changed 清单里点名 |
| `skills list` | 每份覆盖的出身是哪一版，pin 有没有走过它 | 打印 base，base ≠ pin 时标出 |

`list` 只做声明层的比较（base vs pin，不碰 git），把要读历史的那步留给 `sync`——`sync` 本来就已经把 clone provision 到位了，`list` 则可能在 clone 还没克隆出来的机器上被运行。

**为什么 `sync` 对漂移只警告、对缺失出身却中止**：漂移不会丢东西，覆盖本来就可以是刻意落后的（比如上游改坏了，你就是要留住旧行为），所以决定权在人；而缺失出身是声明本身不完整——没有 base 就无从判断漂移，等于这份覆盖脱离检测，`sync` 装下去只会让"看起来受检、实际不受检"的状态更难发现。这和 `_reject_empty_upstreams` 拦的是同一类错误：声明与事实不符，装下去只会更糟。

**为什么出身记在 `sources.py` 而不是覆盖文件夹里的一个标记文件**：`install()` 是整目录 `copytree`，文件夹里多出来的任何东西都会被复制进每一个安装目标，于是一个只服务于本仓库自检的元数据会出现在 agent 工具实际读取的技能目录里。放进 `sources.py` 还有一层好处：它和 pin 在同一个文件、同一次 diff 里被 review——推进了 pin 却忘了同步 base，在 PR 里是看得见的一行。

**代价要说清楚**：加一个覆盖从一步变成两步（复制文件夹 + 记一行 base），忘了第二步会被 `sync` 当场拦下。换来的是覆盖不再是一个只能靠人记住的静默屏蔽。目前这三份 openspec 覆盖的 base 都等于 openspec 的 pin，上游自 2026-01 起没有新提交，所以检测器上线时报告为零漂移——它是为下一次 pin 升级准备的，不是为今天。
