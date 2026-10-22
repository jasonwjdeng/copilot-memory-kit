# Curated memory

Long-lived facts for this repository: conventions, rules, and decisions that
survive across days. Day-to-day notes live in the dated log files
(`YYYY-MM-DD.md`) instead.

### Entry format

Each memory starts with a level-2 heading. Keep the body plain Markdown --
level-1 (`#`) and level-3 (`###`) headings are free to use, but `##` starts
a new entry:

```
## Database migrations need a rollback script

<!-- tags: db, migration -->
<!-- updated: 2026-10-22T12:00:00+08:00 -->

Migrations are paired with a rollback script, otherwise a bad deploy cannot
be undone quickly.
```

### Conventions

- One fact per entry. The title states the fact, the body states the fact
  *and* why.
- Update an existing entry instead of adding a second one that disagrees.
- Delete an entry once it stops being true.
- Promote a fact here once it has held up across several days.
