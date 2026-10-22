---
description: 'How this repository stores and uses persistent agent memory. Applies whenever the task involves recall, conventions, or recording decisions.'
applyTo: '**'
---

# Repository memory protocol

This repository has persistent agent memory in `.copilot/memory/`, exposed
through the `memory` MCP server.

**Recall first.** Before starting a task that changes behaviour, spans
several files, or involves a decision, call `memory_search` with the task's
key terms. Treat any hit as authoritative context: it records what was
actually decided here, which overrides your general default.

**Write second.** When the user states a preference or a rule, when a
decision resolves an ambiguity, or when you find a non-obvious fact about
the codebase, write it with `memory_write`. Include the reason, not just the
rule — a rule without its rationale gets misapplied later.

**Keep one truth.** Update the existing memory instead of adding a second one
that says something different. Delete memories that are no longer true.

**Never store** secrets, credentials, tokens, personal data about third
parties, or anything the repository already documents in a readme or config
file.

## Storage layout

Memories are grouped by day, plus a curated file for what lasts:

```
.copilot/memory/
    MEMORY.md          curated, long-lived facts
    2026-10-22.md      the log for that day
```

`memory_write` appends to today's log by default. Pass `bucket: "curated"`
when a fact has held up across several days and belongs in `MEMORY.md`
instead — conventions, standing rules, decisions that will still matter
next month. Day-to-day notes stay in the daily log.

Each entry is a level-2 heading, so one file holds many memories:

```
## Database migrations need a rollback script

<!-- tags: db, migration -->
<!-- updated: 2026-10-22T12:00:00+08:00 -->

Migrations are paired with a rollback script.
```

If the MCP server is unavailable, the same store is reachable as plain
Markdown files in `.copilot/memory/` — read and write them directly. Keep
the `## ` heading form so the server can still find each entry.
