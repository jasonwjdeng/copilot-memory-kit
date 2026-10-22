# AGENTS.md — cross-tool agent instructions

> Copy this file to the repository root.
> VS Code Copilot, Claude Code, Codex, opencode, pi and others all read
> `AGENTS.md`, so keep this file tool-agnostic. Tool-specific behaviour
> belongs in `.github/copilot-instructions.md`.

## Project memory

This repository keeps durable agent memory as Markdown under
`.copilot/memory/`. Memories are grouped by day, plus a curated file for
what lasts:

```
.copilot/memory/
    MEMORY.md          curated, long-lived entries
    2026-10-22.md      the log for that day
```

Read `.copilot/memory/MEMORY.md` first — it holds the durable facts — then
open the dated logs relevant to the task. Each entry in a dated file is a
level-2 heading:

```markdown
## Database migrations need a rollback script

<!-- tags: db, migration -->
<!-- updated: 2026-10-22T12:00:00+08:00 -->

Migrations are paired with a rollback script.
```

Two ways to reach the same store, use whichever works:

- **MCP tools** (preferred when available): `memory_search`, `memory_read`,
  `memory_list`, `memory_write`.
- **Plain files**: read and write `.copilot/memory/*.md` directly. Keep the
  `## ` heading form so the server can still find each entry.

If a memory contradicts your default approach, the memory wins. It records
what was actually decided in this project.

### Shared store (user-level install)

If the machine was set up with `install_user.py`, the store is shared across
every repository and lives under `~/.copilot/memory/` instead:

```
~/.copilot/memory/
    global/                rules that apply to every project
    projects/<project-id>/ memories scoped to this repository
```

The project id is derived from the working directory, so it is stable per
repository without any configuration. `memory_search` with the default
`project` scope returns this project's memories plus the global ones; pass
`scope: "all"` to search every project. Use `scope: "global"` on
`memory_write` only for rules that genuinely apply everywhere.

## When to write memory

Write a memory when the user states a preference or a rule, when a decision
resolves an ambiguity, or when you discover a non-obvious fact about the
codebase that cost real time to find. Update an existing memory instead of
adding a second, contradicting one; delete memories that have become false.

`memory_write` appends to today's log by default. Pass
`bucket: "curated"` for facts that should outlive the day — standing
conventions, rules the user has stated, decisions that still matter next
month. Everything else stays in the daily log. Promoting an entry that
already exists in a daily log moves it into `MEMORY.md`; it does not leave a
second copy behind.

Never store secrets, credentials, tokens, or personal data about third
parties.

## Build and test

```bash
# syntax check
python3 -m py_compile mcp/memory_server.py install_user.py tests/test_smoke.py
bash -n install.sh

# the whole suite (standard library unittest, no install step)
python3 tests/test_smoke.py

# one-off probe of the MCP server over stdio
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
  '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"memory_write","arguments":{"title":"t","content":"c"}}}' \
  '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"memory_search","arguments":{"query":"t"}}}' \
  | python3 mcp/memory_server.py

# same, dated layout
MEMORY_ORGANIZE=dated python3 mcp/memory_server.py < requests.jsonl

# user-level installer, without writing anything
python3 install_user.py --dry-run
```

`tests/test_smoke.py` is the only test file and it uses nothing but the
standard library, so it runs anywhere the server runs. Every case in it is a
regression test for a bug that shipped once — when you fix something, add the
case before you trust the fix.

## Conventions

- Python standard library only. No pip install, no network calls.
- The server speaks JSON-RPC 2.0 over stdio; keep the protocol version
  pinned at the top of `mcp/memory_server.py`.
- Memory files are plain Markdown and diff-friendly. In the dated layout a
  memory is a level-2 heading; only `##` starts an entry, so bodies may use
  `#` and `###` freely.
- A write only looks for an existing entry inside the directory it is about
  to write to. Never widen that lookup to a whole scope: a `project` write
  would then rewrite a `global` memory that shares the title.
- Writers take the store lock (`store_lock`) and replace files atomically.
  Several servers can share one store, and every write is a full
  read-modify-write; without both, an interleaved pair loses an entry.
- Never delete user data as a side effect of housekeeping. Uninstall keeps
  the memory store unless `--purge` is passed; a parse failure aborts before
  anything is written.
- Both installers are idempotent and never delete user content they did
  not create.

## Before committing

- Tests pass.
- The diff contains only what the task asked for.
- Commit messages follow the format this repository expects.
