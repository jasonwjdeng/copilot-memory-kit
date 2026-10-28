# GitHub Copilot 记忆功能落地方案（公司电脑版）

> 约束：**公司电脑 / 只有 VS Code 的 GitHub Copilot 插件 / 没有 Copilot CLI / 不能用云端 Copilot Memory**

## 0. 先说结论：两种装法

| 装法 | 记忆范围 | 适合 |
|---|---|---|
| **用户级安装**（`install_user.py`） | 全机共享一个存储，按仓库自动分区 + 一层全局 | **公司电脑推荐**：装一次，所有仓库都能用 |
| 仓库级安装（`install.sh`） | 每个仓库一个独立存储 | 想让记忆跟着仓库进 git、共享给同事 |

两种装法装的是**同一个 server**，只是存储布局不同（`MEMORY_LAYOUT=partitioned` vs `flat`）。
下面第 1 节讲用户级装法，第 2 节讲仓库级装法。

## 0.1 能力边界

| 能力 | 公司电脑上能否用 | 说明 |
|---|---|---|
| **VS Code 内置 Memory Tool** | ✅ **能** | 数据只写本机 `workspaceStorage`，不经过 GitHub 云端，无需登录联网 |
| **MCP server（自建本地记忆）** | ✅ **能** | VS Code 是完整 MCP 客户端，本地 stdio 进程即可 |
| **指令文件（AGENTS.md 等）** | ✅ **能** | 纯本地文件，永远可用 |
| **Copilot Memory（云端）** | ❌ **不能** | 需要 GitHub 账号侧开启，且企业策略常禁用 |
| **Copilot CLI** | ❌ **不能** | 未安装 / 不允许 |

所以「类 WorkBuddy 的记忆」在你这里的等价物是三层：

```
第 1 层  指令层    AGENTS.md / .github/copilot-instructions.md   → 稳定规则，常驻注入
第 2 层  记忆层    .copilot/memory/*.md（经 MCP 读写）            → 运行时积累，可 git
第 3 层  会话层    Copilot 内置 Memory Tool 的 session scope      → 单次对话临时
```

**关键认知**：WorkBuddy 的「记忆」本质是**仓库里可版本化的 Markdown 文件**。Copilot 侧要做到同构，
关键不是找一个「云端记忆」开关，而是让 Copilot 的记忆**落到真实文件里**——这样它能进 git、
能跨机器同步、能被其他 agent 读到。

---

## 1. 用户级安装（推荐：装一次，所有仓库共享）

装在**用户主目录**而不是每个仓库里。装完之后，无论打开哪个仓库，Copilot 都有一套自己的记忆。

### 1.1 一条命令

```bash
python install_user.py
```

Windows 上用 Git Bash 或 PowerShell 都行，`py install_user.py` 也可以。**不需要 pip install**，
只用 Python 标准库。

先看它要干什么而不实际写盘：

```bash
python install_user.py --dry-run
```

### 1.2 装了什么、装在哪

| 路径 | 作用 |
|---|---|
| `~/.copilot/memory-kit/memory_server.py` | server 本体 |
| `~/.copilot/memory/` | 共享记忆存储（见 1.3） |
| `~/.copilot/memory/MEMORY.md` | 存储索引 |
| `~/.copilot/instructions/memory.instructions.md` | 用户级定向指令（`applyTo: '**'`） |
| `~/.copilot/copilot-instructions.md` | 用户级常驻指令（**追加**，不覆盖你原有内容） |
| `%APPDATA%\Code\User\mcp.json` | VS Code 用户级 MCP 配置（Windows；macOS 是 `~/Library/Application Support/Code/User/mcp.json`） |
| `~/.copilot/mcp-config.json` | 便携式 MCP 配置（Copilot harness 直接读） |
| `~/.copilot/.locks/` | 写入互斥锁，不在 temp 目录（见下）；可随时删除，下次写入自动重建 |

两个 MCP 配置文件都会写，目的是覆盖不同会话类型：VS Code 原生会话读前者，
Copilot/Agent Host 会话读后者。已有的 server 条目和你自己写的配置**都会保留**。

如果这些 JSON 本身有语法错误（比如少一个括号），安装会**直接中止、一个字都不写**，
不会像早期版本那样把它读成空对象再覆盖掉——那种写法会连带删掉你其它 MCP server。

### 1.3 记忆怎么分区

```
~/.copilot/memory/
├── MEMORY.md              总索引
├── global/                跨项目通用的规则（提交规范、个人偏好…）
│   ├── MEMORY.md          ← 长期沉淀
│   └── 2026-10-22.md      ← 当天的日志
└── projects/
    ├── my-backend-a1b2c3/   ← 每个仓库一个分区，自动识别
    │   ├── MEMORY.md
    │   └── 2026-10-22.md
    └── my-frontend-d4e5f6/
        ├── MEMORY.md
        └── 2026-10-22.md
```

**项目身份怎么来的**：server 启动时的**工作目录**。VS Code 启动 stdio server 时，
工作目录就是当前打开的仓库。server 会向上找最近的 `.git` 目录来确定项目根，
所以在一个 monorepo 的子目录里打开也算同一个项目。目录名后缀是路径的短哈希，
防止两个同名仓库撞车。

想手动指定项目名，设 `MEMORY_PROJECT` 环境变量即可。

### 1.4 记忆怎么组织：按日期 + 长期沉淀

每个分区内部按**日期**分文件，和 WorkBuddy 的记忆结构一致：

```
projects/my-backend-a1b2c3/
├── MEMORY.md          长期沉淀：跨天仍然成立的规则与决策
├── 2026-10-21.md      某天的日志
└── 2026-10-22.md
```

一个文件里可以放多条记忆，每条以二级标题开头：

```markdown
## 数据库迁移必须先写回滚脚本

<!-- tags: db, migration -->
<!-- updated: 2026-10-22T12:00:00+08:00 -->

迁移脚本要配套回滚脚本，否则线上出问题无法快速恢复。
```

| 操作 | 落在哪 |
|---|---|
| `memory_write`（默认） | 追加到**当天**的日志文件 |
| `memory_write` + `bucket: "curated"` | 写进 `MEMORY.md`；若同名条目已在日志里，会**从日志迁移过来**而不是留两份 |
| `memory_write`（同标题已存在） | **原位更新**那条，不新增 |

作用域是硬的：默认 `scope: project` 的写入**只会在本仓库分区里找同名条目**，
不会碰到 `global/` 里的同名记忆（早期版本会，属于数据事故）。

**什么时候用哪个**：日常琐事、临时决策 → 日志文件；反复出现、跨天仍成立的
规则（提交规范、架构约定、用户明确表达的偏好）→ `MEMORY.md`。
日志文件本身就是「那天干了什么」的完整记录，`MEMORY.md` 是被提炼过的结论。

正文里可以自由用 `#` 和 `###` 标题。行首的 `## ` 在**写入时**会被降级成 `### `
（`##` 本来的含义是「新条目」，留着它一条记忆会劈成两条）；未闭合的代码围栏
会自动补上收尾，否则它会把当天后面所有条目都吞成代码。代码块（```）里的
`##` 保持原样，不会被误解析。

同一个标题写两次时，写入发生在**它原来所在的那个文件**里；`bucket: "curated"`
会把日志里的同名条目搬到 `MEMORY.md`，不会留下两份。

旧格式（一记忆一文件）的 store 仍然**能读**（`MEMORY_ORGANIZE` 也是 `file` 时直接就是这种格式），
但不提供批量转换脚本：格式切换只影响新写入的落点，不需要挪旧文件。

### 1.5 三种 scope

| scope | 写到哪 | 检索时看到 |
|---|---|---|
| `project`（默认） | 当前仓库的分区 | 本项目 + global |
| `global` | `global/` | 只看 global |
| `all` | — | 所有项目的分区 + global |

`memory_search` / `memory_list` 接受 `scope` 参数，`memory_write` 接受 `scope`
（`project` 或 `global`）。**跨项目通用的规则才写 `global`**——把项目专属决策
放进 global 会污染其他仓库，这是这套设计唯一需要你注意的地方。

### 1.6 验证

```bash
# 1. 重启 VS Code，打开任意一个仓库
# 2. Ctrl+Shift+P → MCP: List Servers → 启动 'memory'
# 3. 在 Copilot Chat 里问：
```

```
调用 memory_write 记住：这个项目用 pnpm，不是 npm
```

然后：

```
调用 memory_list 看看记住了什么
```

`~/.copilot/memory/projects/<项目>/` 下应该出现一个 `.md` 文件。
切到**另一个**仓库再问 `memory_list`，应该**看不到**刚才那条，但能看到 `global/` 里的。

### 1.7 卸载

```bash
python install_user.py --uninstall           # 摘掉 server 和指令文件，保留记忆
python install_user.py --uninstall --purge   # 连记忆库一起删
```

`--uninstall` 会移除装的东西，但**保留 `~/.copilot/memory/`**——那是你的数据，不是工具的一部分。
它也会**保留**你在 `copilot-instructions.md` 和 `mcp.json` 里写的其他内容
（安装时是追加/合并，卸载时精确摘掉自己那一段）。

确认真的不要这些记忆了，再显式加 `--purge`。

### 1.8 全部参数

```
python install_user.py                 # 安装（可重复执行，幂等）
python install_user.py --dry-run       # 只看不写
python install_user.py --uninstall     # 卸载，保留记忆
python install_user.py --uninstall --purge   # 卸载并删除记忆库
python install_user.py --python /path/to/python3   # 指定解释器
```

---

## 2. 最快路径：只用内置 Memory Tool（零配置）

先确认这个能用，再决定要不要上 MCP。

### 2.1 检查设置

`Ctrl+Shift+P` → `Preferences: Open User Settings (JSON)`，确认：

```jsonc
{
  "github.copilot.chat.tools.memory.enabled": true
}
```

这个开关**默认就是开的**。它是纯本地功能，不依赖 Copilot Memory。

### 2.2 使用

在 Copilot Chat 里直接用自然语言：

```
记住：这个项目的测试放在 tests/ 目录，文件名和被测模块同名
```

它会自己判断写到哪一档：

| scope | 路径 | 跨会话 | 跨项目 | 装什么 |
|---|---|---|---|---|
| User | `/memories/` | ✅ | ✅（前 200 行自动注入） | 个人偏好、常用命令 |
| Repository | `/memories/repo/` | ✅ | ❌ | 本仓库约定、构建命令 |
| Session | `/memories/session/` | ❌ | ❌ | 当前任务的临时笔记 |

管理命令：`Chat: Show Memory Files` / `Chat: Clear All Memory Files`。

### 2.3 致命限制

这些文件落在 VS Code 的 `workspaceStorage` 里，**不在你的仓库里**：

- 公司电脑 ↔ 家里电脑：不同步
- 不进 git，同事看不到
- VS Code 换 profile / 重装扩展：可能丢
- Claude Code / Codex / WorkBuddy：**完全读不到**

---

## 3. 仓库级安装（可 git、可跨 agent）

> 这一节是**仓库级**装法：记忆跟着仓库走，能进 git、共享给同事。
> 如果你只是想在**自己所有的仓库**里都有记忆（不需要跟同事共享），
> 用第 1 节的用户级安装更省事——装一次就行，不用每个仓库跑一遍。

本 kit 提供一个**零依赖**的本地记忆 MCP server（只用 Python 标准库，不联网、不需要 API key），
把记忆存成仓库里的 Markdown。

### 3.1 安装

```bash
cd /path/to/copilot-memory-kit
./install.sh /path/to/your-repo
```

装完仓库里会有：

```
your-repo/
├── AGENTS.md                                  # 跨工具规则，Copilot/Claude/Codex 都读
├── .github/
│   ├── copilot-instructions.md                # Copilot 专属、常驻注入的记忆协议
│   └── instructions/
│       └── memory.instructions.md            # 按 applyTo glob 定向的记忆协议
├── .vscode/
│   └── mcp.json                              # MCP 客户端配置（注意是 servers 键）
├── mcp/
│   └── memory_server.py                       # 零依赖记忆 server
└── .copilot/
    ├── .locks/                                 # 写入互斥锁（运行时生成）
    └── memory/
        ├── MEMORY.md                           # 长期沉淀的记忆（安装时就有）
        └── 2026-10-22.md                       # 首次写入后才会出现的当天日志
```

### 3.2 启动

VS Code 里 `Ctrl+Shift+P` → `MCP: List Servers` → 找到 `memory` → start。
或者开一个 Copilot Chat 的 Agent 会话，工具会自动出现。

### 3.3 验证

在 Copilot Chat 里问：

```
调用 memory_write 记住：数据库迁移必须先写回滚脚本
```

然后：

```
调用 memory_list 看看记住了什么
```

仓库里应该出现 `.copilot/memory/2026-10-22.md`（今天的日期文件）。

### 3.4 MCP 暴露的工具

| 工具 | 作用 |
|---|---|
| `memory_search(query, limit, scope, since, until)` | BM25 排序（含文档长度归一化与词频饱和）+ 短语命中加成；标题词按 3 倍计入、标签 2 倍、正文 1 倍；英文按词做词形折叠（`score`/`scored`/`scoring` 视为同一词），中文只在连续汉字串内切二元组、不跨空格或标点；内容重复的记忆会合并，命中的其余标题放在 `also`；低于最高分 40% 的结果直接丢弃。可按日期区间过滤（`since`/`until` 只收 `YYYY-MM-DD`，格式不对直接报错，无日期的记忆不参与区间过滤） |
| `memory_write(title, content, tags, scope, bucket)` | 写入/更新一条记忆，同标题自动原位更新；`bucket` 选 `daily`（默认）或 `curated`（会把同名日志条目迁移进 `MEMORY.md`） |
| `memory_read(name, scope)` | 读全文；`name` 传日期（`2026-10-22`）可读出那天的全部记忆；`scope` 选 `project`（默认）/ `global` / `all` |
| `memory_list(scope, date, bucket)` | 列出全部，带预览；可按日期或 bucket 过滤（日期不是 `YYYY-MM-DD`、bucket 不是 `daily`/`curated` 直接报错，不会静默忽略过滤条件） |
| `memory_delete(name)` | 删除（仅在明确要求时用） |

`scope` 参数在**仓库级**装法下不起实际作用（只有一层存储）；在**用户级**装法下
用来区分「当前项目 / 全局 / 全部」，见第 1.5 节。

`bucket` 只在 dated 组织方式下有意义。如果你把 `MEMORY_ORGANIZE` 设成 `file`
（一记忆一文件，早期格式），每条记忆本身就是独立文件，`bucket` 不参与写入路径。

### 3.5 让 Copilot 真的去用它

装了 MCP 不等于 Copilot 会主动调用。**必须靠指令层驱动**，这也是
`.github/copilot-instructions.md` 的作用——它写明了：

- 开工前先 `memory_search`
- 命中结果**优先于**你的默认做法（因为那是这个项目真正做过的决定）
- 用户表态 / 决策消歧 / 发现非显然事实 → `memory_write`
- 记忆过时了就改，不要新增一条矛盾的

### 3.6 纳入 git

```bash
cd /path/to/your-repo
git add .copilot AGENTS.md .github .vscode mcp
git commit -m "chore: add local memory layer for Copilot"
```

不想把记忆提交上去的话，`.gitignore` 里加：

```gitignore
.copilot/memory/*.md
!.copilot/memory/MEMORY.md
.copilot/.locks/
```

---

## 4. 指令层怎么分工

| 文件 | 读它的东西 | 放什么 | 会同步吗 |
|---|---|---|---|
| `AGENTS.md` | 所有主流 agent（Copilot / Claude Code / Codex / opencode / pi） | 跨工具通用：构建命令、目录约定、提交规范 | 走 git |
| `.github/copilot-instructions.md` | 仅 Copilot | Copilot 专属行为、记忆协议 | 走 git |
| `.github/instructions/*.instructions.md` | 仅 Copilot，且按 `applyTo` glob 触发 | 特定语言/目录的规则 | 走 git |
| `~/.copilot/copilot-instructions.md` | 仅 Copilot，跨项目 | 个人全局偏好 | 不走 git（`~/.copilot/instructions` 不同步 Settings Sync） |

**注意**：GitHub 现在多个自定义指令文件是**合并**的，不再按优先级回退。所以别在两个文件里写
互相矛盾的内容——冲突不会被规则解决，只会让模型困惑。

---

## 5. 公司电脑特有的注意事项

1. **不能装东西** → 本 kit 只依赖系统自带 python3，无需 pip。
   - Windows：`install_user.py` 会自动找 `python.exe` / `py` 启动器下的解释器，
     找不到就用 `--python` 手动指定。
   - macOS/Linux：`/usr/bin/python3` 即可。
2. **不能联网** → 记忆 server 完全离线；`memory_search` 是本地 BM25 检索，不调用 embedding API。
3. **MCP 审批** → 首次调用工具时 VS Code 会弹权限确认，选「Always allow」避免每次都点。
4. **企业策略可能禁用 MCP** → 若 `MCP: List Servers` 看不到你的 server，或被策略拦截，
   退回到纯文件方案：让 Copilot 直接用内置 `read_file` / `edit_file` 读写
   `~/.copilot/memory/` 下的 Markdown。用户级指令文件里已经写了这条降级路径。
5. **仓库不能提交** → 用 `MEMORY_ROOT` 环境变量把存储指到仓库外
   （例如 `~/copilot-memories/<repo-name>`），`mcp.json` 里改 `env` 即可。
6. **多仓库共用一份记忆** → 用**用户级安装**（第 1 节）。它会自动按仓库分区，
   既共享又不会串味，比手工共用一个 `MEMORY_ROOT` 更省心。
7. **不想留痕于用户目录** → 用户级安装只写 `~/.copilot/` 和 VS Code 的用户配置目录，
   不碰任何仓库目录，卸载用 `python install_user.py --uninstall`。

---

## 6. 文件清单与来源

| 文件 | 说明 |
|---|---|
| `install_user.py` | **用户级**安装脚本（Python，跨平台）：装一次，全机共享；支持 `--dry-run` / `--uninstall` |
| `install.sh` | **仓库级**安装脚本（bash），幂等，不删除任何已有文件；`--force` 才覆盖 |
| `mcp/memory_server.py` | 记忆 MCP server，Python 标准库，JSON-RPC 2.0 over stdio；支持 flat / partitioned 两种分区布局 × file / dated 两种组织方式 |
| `tests/test_smoke.py` | 回归测试（仅标准库 unittest）：`python3 tests/test_smoke.py` |
| `.github/copilot-instructions.md` | Copilot 记忆协议（仓库级，常驻注入） |
| `.github/instructions/memory.instructions.md` | 定向记忆协议（`applyTo: '**'`） |
| `AGENTS.md` | 跨工具 agent 指令 + 记忆索引指引 |
| `.vscode/mcp.json` | MCP 客户端配置（顶层 `servers` 键） |
| `memory/MEMORY.md` | 长期沉淀记忆的模板 |

已本地验证：协议握手、`tools/list`、写入、检索（含中文）、读取、缺失记忆报错、
`MEMORY_ROOT` 路径解析、安装脚本幂等性、**用户级安装的仓库分区隔离**
（A 仓库看不到 B 仓库的记忆）、**已有用户配置的合并保留**、**卸载精确还原**、
**JSONC（带注释）配置解析**、**按日期分文件的写入 / 原位更新 / 删除**、
**旧格式 store 的读取**、**代码块内的 `##` 不被误解析**、**多进程并发写入不丢条目**、
**超长标题不再撞同一文件**、**未闭合围栏不再吞掉后续条目**、**标题含 `---` 不再截断 frontmatter**、
**正文里的 `<!-- tags: -->` 不再被当元数据**、**锁文件不随 `TMPDIR` 漂移**。

这些检查现在固定成 `tests/test_smoke.py`（37 个用例，仅标准库），跑一条命令即可：

```bash
python3 tests/test_smoke.py
```

---

## 7. 参考来源

- VS Code Memory 文档：https://code.visualstudio.com/docs/agents/run/memory
- Copilot Memory（云端，本方案不依赖）：https://code.visualstudio.com/docs/copilot/agents/memory
- Copilot 自定义功能速查：https://docs.github.com/en/copilot/reference/customization-cheat-sheet
- VS Code 自定义指令：https://code.visualstudio.com/docs/agent-customization/custom-instructions
- VS Code MCP 服务器：https://code.visualstudio.com/docs/copilot/chat/mcp-servers
- VS Code MCP 配置参考：https://code.visualstudio.com/docs/copilot/reference/mcp-configuration
