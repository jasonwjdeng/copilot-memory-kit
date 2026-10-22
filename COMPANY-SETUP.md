# 公司电脑安装速查卡

> 打印或存手机上了。到公司电脑照着做。
> **本机（家里）不需要装。**

---

## 0. 到公司先确认三件事

| 检查项 | 怎么查 | 缺了怎么办 |
|---|---|---|
| Python 3.8+ | Git Bash 里跑 `python --version` | 装 Python 或用 `py -3 --version` 看有没有 `py` 启动器 |
| VS Code | 打开就看见了 | — |
| Copilot 插件 | VS Code 扩展里搜 `github.copilot` | 装上（公司可能已预装） |

---

## 1. 安装（一条命令）

把这几个文件拷到公司电脑（U 盘 / 内网共享 / 直接 clone 都行）：

```
install_user.py
mcp/memory_server.py
```

然后在 **Git Bash** 里：

```bash
cd <放这些文件的地方>
python install_user.py --dry-run     # 先看它要干什么
python install_user.py               # 确认没问题就装
```

> PowerShell 里同理：`py install_user.py`
> 提示找不到 python 就指定：`python install_user.py --python "C:/Python312/python.exe"`

**成功后会看到：**

```
installing local-memory kit (user scope)
  home:    C:\Users\<你的用户名>
  python:  C:\Python312\python.exe (3.12)
  store:   C:\Users\<你的用户名>\.copilot\memory
  server:  C:\Users\<你的用户名>\.copilot\memory-kit\memory_server.py

  write ...\memory-kit\memory_server.py
  write ...\memory\MEMORY.md
  write ...\instructions\memory.instructions.md
  write ...\copilot-instructions.md
  write ...\Code\User\mcp.json
  write ...\.copilot\mcp-config.json

done.
```

---

## 2. 验证（3 步）

1. **重启 VS Code**，打开任意一个仓库文件夹
2. `Ctrl+Shift+P` → 输入 `MCP: List Servers` → 找到 `memory` → 点 start
   - 如果提示信任这个 server，选 **Trust**
3. 在 Copilot Chat 里问：

```
调用 memory_write 记住：这个项目用 pnpm 不是 npm
```

再问：

```
调用 memory_list 看看记住了什么
```

**应该看到**：`scope: "project"`，路径在 `.copilot\memory\projects\<项目名>-<哈希>\` 下，
文件名是当天日期（`2026-10-22.md`）。

**再切到另一个仓库**，问同样的问题 → **应该看不到**刚才那条（这是分区生效的证据）。

---

## 3. 记忆怎么存的

每个分区内部按**日期**分文件，和 WorkBuddy 的记忆结构一致：

```
projects\<项目名>-<哈希>\
├── MEMORY.md          长期沉淀：跨天仍成立的规则
└── 2026-10-22.md      当天的日志
```

一个文件里放多条记忆，每条以二级标题开头：

```markdown
## 这个项目用 pnpm 不是 npm

<!-- tags: build -->
<!-- updated: 2026-10-22T12:00:00+08:00 -->

仓库有 pnpm-lock.yaml，CI 也按 pnpm 装依赖。
```

| 想记什么 | 怎么记 |
|---|---|
| 今天做了什么、临时决策 | 默认就写进当天的日志文件 |
| 一直成立的规则（提交规范、架构约定、你的偏好） | 让 Copilot 用 `bucket: "curated"` 写进 `MEMORY.md` |

直接跟 Copilot 说就行，例如：

```
把这个项目的提交规范记下来，以后所有任务都按这个来
```

---

## 4. 出问题时

| 现象 | 原因 / 怎么办 |
|---|---|
| `MCP: List Servers` 里没有 memory | 重启 VS Code；确认写的是 `%APPDATA%\Code\User\mcp.json` 不是别处 |
| server 启动失败 | `MCP: List Servers` → 点 memory → 看 Output 面板里的错误。多半是 python 路径不对，用 `--python` 重装 |
| 工具不出现 | Chat 输入框的「Configure Tools」里确认 memory 的工具是勾上的 |
| 企业策略禁用 MCP | 降级方案：直接让 Copilot 用 `read_file` / `edit_file` 读写 `C:\Users\<你>\.copilot\memory\` 下的 md 文件，指令文件里已写明 |
| 记忆串到别的项目 | 检查是不是写成了 `scope: "global"`；项目级记忆应该用默认的 `project` scope |

---

## 5. 日常用法

**在 Copilot Chat 里直接说就行**，不用手动调工具：

```
这个仓库部署走 GitLab CI，记住这个
```

```
我们团队提交信息一律用 Conventional Commits 英文格式（这条对所有项目都适用）
```

第二句如果要**所有项目都生效**，Copilot 会写进 `global/`；只对当前项目生效就留在当前分区。

想直接看存储：

```bash
cat ~/.copilot/memory/MEMORY.md            # 总索引
ls ~/.copilot/memory/projects/              # 各项目的分区
cat ~/.copilot/memory/projects/<项目>/2026-10-22.md   # 某天的日志
```

---

## 6. 卸载

```bash
python install_user.py --uninstall           # 摘掉工具，记忆留着
python install_user.py --uninstall --purge   # 确认不要了才连记忆一起删
```

只移除装的东西：**你自己写的 `copilot-instructions.md` 内容和 `mcp.json` 里其他 server 全部保留**，
`C:\Users\<你>\.copilot\memory\` 里的记忆也保留。想彻底清干净再加 `--purge`。

---

## 7. 装完之后东西在哪

```
C:\Users\<你>\.copilot\
├── copilot-instructions.md          # 你的全局指令（我们追加了一段）
├── instructions\
│   └── memory.instructions.md       # 记忆协议，applyTo: '**'
├── mcp-config.json                  # 便携式 MCP 配置
├── memory-kit\memory_server.py      # server 本体
└── memory\                          # ← 记忆都在这
    ├── MEMORY.md                    # 总索引
    ├── global\                      # 跨项目通用
    │   ├── MEMORY.md                #   长期沉淀
    │   └── 2026-10-22.md            #   当天的日志
    └── projects\
        ├── my-backend-a1b2c3\       # 每个仓库一个分区
        │   ├── MEMORY.md
        │   └── 2026-10-22.md
        └── my-frontend-d4e5f6\

C:\Users\<你>\AppData\Roaming\Code\User\mcp.json   # VS Code 用户级 MCP 配置
```

---

## 8. 一句话总结

**装一次，所有仓库都有记忆；项目之间不串味；跨项目通用的规则放 global；
记忆按日期归档，长期成立的沉淀到 MEMORY.md。**
