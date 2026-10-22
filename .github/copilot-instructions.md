# Copilot instructions — memory protocol

This is the always-on instruction layer for this repository: Copilot reads it
in every chat session.

## Memory protocol

You have persistent memory for this repository through the `memory` MCP
server. Treat it as part of your standing context, not as an optional extra.

**Before planning a non-trivial task** (anything that touches more than one
file, changes behaviour, or involves a decision the user might have already
made):

1. Call `memory_search` with the task's key terms.
2. If a hit contradicts your default approach, the memory wins — it records
   what was actually decided here.
3. If nothing relevant comes back, proceed normally.

**During the task**, if you discover something durable, write it with
`memory_write`. Good candidates:

- A convention or rule the user stated ("always use X", "never do Y")
- A decision the user made that resolves an ambiguity
- A non-obvious fact about the codebase that cost time to discover
- A correction to something you previously got wrong

Do not write: transient state, anything the repo already records in a
readme or config, secrets, credentials, or personal data about third
parties.

**At the end of a task**, if the work changed how the project should be
approached, write or update that memory before you summarise.

## Storage

Memories live as plain Markdown under `.copilot/memory/` in this repository.
They are committed alongside the code, so they travel with the repo and are
visible to any other tool or teammate.

The store is grouped by day plus a curated file:

```
.copilot/memory/
    MEMORY.md          curated, long-lived facts
    2026-10-22.md      the log for that day
```

`memory_write` appends to today's log by default. Use `bucket: "curated"`
for facts that should outlive the day — standing conventions, rules the
user has stated, decisions that will still matter next month. Promoting an
entry that already exists in a daily log moves it into `MEMORY.md` rather
than leaving a second copy. Everything else belongs in the daily log.

When a memory is no longer true, update it rather than adding a contradicting
one. When it is obsolete, delete it with `memory_delete`.

## Style

- Match the surrounding code's conventions rather than importing habits from
  another project.
- Prefer the smallest change that solves the problem.
- Do not reformat or restructure code the task did not ask you to touch.
