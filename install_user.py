#!/usr/bin/env python3
"""Install the local-memory kit for the current user (not per repository).

One installation, shared by every repository you open. Memories live in a
single store under ``~/.copilot/memory`` and are partitioned automatically:

    ~/.copilot/memory/
        MEMORY.md              index of the whole store
        global/                rules that apply to every project
        projects/<id>/         memories scoped to one repository

The project partition is derived from the working directory VS Code gives
the server when it starts, so no per-repository configuration is needed.

    python install_user.py                 # install (or update)
    python install_user.py --dry-run       # show what would happen
    python install_user.py --uninstall     # remove the kit, keep the memories
    python install_user.py --uninstall --purge   # also delete the memory store

Uninstall never deletes memories unless ``--purge`` is given: the store is
the user's data, not part of the kit.

Runs on Windows, macOS and Linux. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

KIT_DIR = Path(__file__).resolve().parent
SERVER_SRC = KIT_DIR / "mcp" / "memory_server.py"

COPILOT_HOME = Path(os.environ.get("COPILOT_HOME") or (Path.home() / ".copilot")).expanduser()
SERVER_DST = COPILOT_HOME / "memory-kit" / "memory_server.py"
STORE = COPILOT_HOME / "memory"
INSTRUCTIONS_DIR = COPILOT_HOME / "instructions"
USER_INSTRUCTIONS = INSTRUCTIONS_DIR / "memory.instructions.md"
ALWAYS_ON = COPILOT_HOME / "copilot-instructions.md"
PORTABLE_MCP = COPILOT_HOME / "mcp-config.json"

BEGIN_MARK = "<!-- BEGIN copilot-memory-kit -->"
END_MARK = "<!-- END copilot-memory-kit -->"

SERVER_ID = "memory"


# --------------------------------------------------------------------------
# JSON with comments (VS Code allows comments in mcp.json)
# --------------------------------------------------------------------------

def strip_json_comments(text: str) -> str:
    """Remove // and /* */ comments outside of strings."""
    out = []
    i = 0
    n = len(text)
    in_string = False
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n:
            nxt = text[i + 1]
            if nxt == "/":
                while i < n and text[i] != "\n":
                    i += 1
                continue
            if nxt == "*":
                i += 2
                while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                    i += 1
                i += 2
                continue
        out.append(ch)
        i += 1
    return "".join(out)


class ConfigError(Exception):
    """An existing config file could not be parsed.

    Raising beats returning an empty mapping: the caller would otherwise
    write a fresh object back over the user's file and silently drop every
    other server (and every comment) it held.
    """


def load_jsonc(path: Path) -> dict:
    """Read a JSON(C) file. Missing file -> empty dict.

    Raises ``ConfigError`` when the file exists but is not parseable, so the
    caller can abort instead of clobbering it.
    """
    if not path.exists():
        return {}
    raw = path.read_text(encoding="utf-8-sig")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        cleaned = strip_json_comments(raw)
        # Trailing commas are legal in JSONC but not in JSON.
        cleaned = re_sub_trailing_commas(cleaned)
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level is not a JSON object")
    return data


def re_sub_trailing_commas(text: str) -> str:
    out = []
    in_string = False
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if ch == ",":
            j = i + 1
            while j < n and text[j].isspace():
                j += 1
            if j < n and text[j] in "}]":
                i += 1
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def dump_json(data: dict) -> str:
    """Serialise a config object the way VS Code writes it."""
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


# --------------------------------------------------------------------------
# Locating things
# --------------------------------------------------------------------------

def vscode_user_mcp_paths() -> list[Path]:
    """Candidate locations of the VS Code user-profile mcp.json."""
    home = Path.home()
    if os.name == "nt":
        appdata = Path(os.environ.get("APPDATA") or (home / "AppData" / "Roaming"))
        return [
            appdata / "Code" / "User" / "mcp.json",
            appdata / "Code - Insiders" / "User" / "mcp.json",
        ]
    if sys.platform == "darwin":
        return [
            home / "Library" / "Application Support" / "Code" / "User" / "mcp.json",
            home / "Library" / "Application Support" / "Code - Insiders" / "User" / "mcp.json",
        ]
    return [
        home / ".config" / "Code" / "User" / "mcp.json",
        home / ".config" / "Code - Insiders" / "User" / "mcp.json",
    ]


def windows_python_dirs() -> list[Path]:
    """Directories that hold per-user CPython installs on Windows.

    ``LOCALAPPDATA`` can be unset (services, stripped CI shells); falling back
    to the conventional location beats scanning a path relative to the cwd.
    """
    local = os.environ.get("LOCALAPPDATA", "").strip()
    base = Path(local) if local else (Path.home() / "AppData" / "Local")
    return [base / "Programs" / "Python"]


def find_python() -> str:
    """An interpreter path that works when launched with a minimal PATH.

    VS Code starts stdio servers with a trimmed environment, so a bare
    "python3" may not resolve. Prefer a concrete absolute path.
    """
    candidates: list[str] = []
    exe = sys.executable
    if exe and Path(exe).exists():
        candidates.append(exe)
    if os.name == "nt":
        for name in ("python.exe", "python3.exe"):
            found = shutil.which(name)
            if found:
                candidates.append(found)
        for base in windows_python_dirs():
            if base.is_dir():
                for entry in sorted(base.iterdir()):
                    candidate = entry / "python.exe"
                    if candidate.exists():
                        candidates.append(str(candidate))
    else:
        for name in ("python3", "python"):
            found = shutil.which(name)
            if found:
                candidates.append(found)
        candidates.extend([
            "/opt/homebrew/bin/python3",
            "/usr/local/bin/python3",
            "/usr/bin/python3",
        ])
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return str(Path(candidate))
    return "python3"


def check_python_version(python: str) -> tuple[bool, str]:
    """Verify the interpreter is Python 3.8+."""
    try:
        out = subprocess.run(
            [python, "-c", "import sys;print('%d.%d'%sys.version_info[:2])"],
            capture_output=True, text=True, timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return False, "could not run the interpreter"
    version = out.stdout.strip()
    if out.returncode != 0:
        return False, out.stderr.strip() or "interpreter failed to start"
    try:
        major, minor = (int(part) for part in version.split(".")[:2])
    except ValueError:
        return False, f"unexpected version string: {version!r}"
    if (major, minor) < (3, 8):
        return False, f"Python {version} is too old, 3.8 or newer is required"
    return True, version


# --------------------------------------------------------------------------
# Instruction text
# --------------------------------------------------------------------------

ALWAYS_ON_SECTION = f"""
{BEGIN_MARK}
## Persistent memory

This machine has a local memory store shared by every repository. It is
exposed through the `{SERVER_ID}` MCP server and stored as plain Markdown
under `~/.copilot/memory` (use `MEMORY_ROOT` if that differs).

**Recall first.** Before starting a non-trivial task -- one that spans
several files, changes behaviour, or involves a decision the user might
already have made -- call `memory_search` with the task's key terms. Treat
any hit as authoritative context: it records what was actually decided here,
which overrides your general default.

**Write second.** When the user states a preference or a rule, when a
decision resolves an ambiguity, or when you find a non-obvious fact about
the codebase, write it with `memory_write`. Include the reason, not just the
rule -- a rule without its rationale gets misapplied later.

**Scopes.** Memories written with the default `project` scope stay in the
current repository's partition. Use `scope: "global"` only for rules that
genuinely apply everywhere -- personal preferences, universal tooling
conventions. Keep project-specific decisions out of the global scope, or
they will leak into unrelated repositories.

**Buckets.** Each partition groups memories by day, plus a curated
`MEMORY.md` for what lasts:

    projects/<id>/2026-10-22.md   the log for that day
    projects/<id>/MEMORY.md       facts promoted out of the daily logs

`memory_write` appends to today's log by default. Pass `bucket: "curated"`
for a fact that has held up across several days and belongs in `MEMORY.md`
instead -- standing conventions, rules the user has stated, decisions that
will still matter next month. Everything else stays in the daily log.

**Keep one truth.** Update an existing memory instead of adding a second one
that says something different. Delete memories that are no longer true.

**Never store** secrets, credentials, tokens, personal data about third
parties, or anything the repository already documents in a readme or config
file.
{END_MARK}
""".strip()

TARGETED_INSTRUCTION = """---
description: 'Persistent agent memory for this machine. Applies to recall, project conventions, and recording decisions.'
applyTo: '**'
---

# Persistent memory

This machine has a local memory store shared by every repository, exposed
through the `memory` MCP server and stored as plain Markdown under
`~/.copilot/memory`.

**Recall first.** Before starting a task that changes behaviour, spans
several files, or involves a decision, call `memory_search` with the task's
key terms. A hit is authoritative context: it records what was actually
decided, which overrides your general default.

**Write second.** When the user states a preference or a rule, when a
decision resolves an ambiguity, or when you find a non-obvious fact about
the codebase, write it with `memory_write`. Include the reason, not just the
rule.

**Scopes.** The default `project` scope keeps a memory in the current
repository's partition. Use `scope: "global"` only for rules that genuinely
apply to every project. Project-specific decisions must not go into the
global scope, or they leak into unrelated repositories.

**Buckets.** Each partition groups memories by day, plus a curated
`MEMORY.md` for what lasts. `memory_write` appends to today's log by default.
Pass `bucket: "curated"` for a fact that has held up across several days
and belongs in `MEMORY.md` instead -- standing conventions, rules the user
has stated, decisions that will still matter next month.

**Keep one truth.** Update an existing memory rather than adding a second one
that says something different. Delete memories that are no longer true.

**Never store** secrets, credentials, tokens, personal data about third
parties, or anything the repository already documents in a readme or config
file.
""".rstrip()

GLOBAL_CURATED = """# Global memory

Curated facts that apply to every repository on this machine: personal
preferences, universal tooling conventions, rules the user has stated for
all work.

Keep project-specific decisions out of this file -- they belong in the
project's own partition, or they will leak into unrelated repositories.

Each entry starts with a level-2 heading:

```
## Commit messages are English

<!-- tags: git -->
<!-- updated: 2026-01-01T00:00:00+08:00 -->

Conventional Commits, English only.
""".rstrip()


STORE_INDEX = """# Memory store

Shared store for every repository on this machine.

```
~/.copilot/memory/
    MEMORY.md          curated, long-lived facts
    global/            memories that apply to every project
    projects/<id>/     memories scoped to one repository
```

Memories are grouped by day inside each scope:

```
<scope>/2026-10-22.md      the log for that day
<scope>/MEMORY.md          facts promoted out of the daily logs
```

Each entry inside a dated file is a level-2 heading:

```
## Database migrations need a rollback script

<!-- tags: db, migration -->
<!-- updated: 2026-10-22T12:00:00+08:00 -->

Migrations are paired with a rollback script.
```

### Conventions

- One fact per entry. Title states the fact, body states the fact *and* why.
- Update in place; never keep two memories that disagree.
- Delete when a memory becomes false.
- Use the global scope only for rules that apply everywhere.
- Promote a fact to `MEMORY.md` once it has held up across several days.
""".rstrip()

# --------------------------------------------------------------------------
# Install steps
# --------------------------------------------------------------------------

class Reporter:
    def __init__(self, dry_run: bool) -> None:
        self.dry_run = dry_run
        self.changed = False

    def say(self, text: str) -> None:
        print(text)

    def write(self, path: Path, content: str) -> None:
        label = "would write" if self.dry_run else "write"
        if path.exists() and path.read_text(encoding="utf-8") == content:
            self.say(f"  keep   {path}")
            return
        self.say(f"  {label} {path}")
        self.changed = True
        if not self.dry_run:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Replace atomically: a half-written mcp.json breaks every MCP
            # server in the editor, not just this one.
            tmp = path.with_name(f".{path.name}.tmp")
            tmp.write_text(content, encoding="utf-8")
            os.replace(tmp, path)

    def copy(self, src: Path, dst: Path) -> None:
        if not src.exists():
            self.say(f"  skip   {dst} (source missing: {src})")
            return
        if dst.exists() and dst.read_bytes() == src.read_bytes():
            self.say(f"  keep   {dst}")
            return
        label = "would write" if self.dry_run else "write"
        self.say(f"  {label} {dst}")
        self.changed = True
        if not self.dry_run:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)

    def remove(self, path: Path) -> None:
        if not path.exists():
            return
        label = "would remove" if self.dry_run else "remove"
        self.say(f"  {label} {path}")
        self.changed = True
        if not self.dry_run:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()


def merge_marked_section(path: Path, section: str, rep: Reporter) -> None:
    """Insert or refresh a marked block inside an existing markdown file."""
    if not path.exists():
        rep.write(path, section + "\n")
        return
    existing = path.read_text(encoding="utf-8")
    if BEGIN_MARK in existing and END_MARK in existing:
        head = existing.split(BEGIN_MARK)[0]
        tail = existing.split(END_MARK, 1)[1]
        updated = head + section + tail
    else:
        updated = existing.rstrip() + "\n\n" + section + "\n"
    rep.write(path, updated)


def server_entry(python: str, use_workspace_cwd: bool) -> dict:
    entry = {
        "type": "stdio",
        "command": python,
        "args": [str(SERVER_DST)],
        "env": {
            "MEMORY_ROOT": str(STORE),
            "MEMORY_LAYOUT": "partitioned",
            "MEMORY_ORGANIZE": "dated",
        },
    }
    if use_workspace_cwd:
        # VS Code's own mcp.json understands this variable; it resolves to
        # the workspace that is currently open.
        entry["cwd"] = "${workspaceFolder}"
    return entry


def plan_config_writes(python: str) -> list[tuple[Path, dict, str]]:
    """Parse every MCP config we are about to touch, before writing anything.

    A malformed file raises ``ConfigError`` here rather than halfway through
    the install, so a broken ``mcp.json`` can never be overwritten with a
    one-server object that loses the user's other servers.
    """
    targets = vscode_user_mcp_paths()
    existing = [p for p in targets if p.exists()]
    chosen = existing[:1] if existing else targets[:1]

    plans: list[tuple[Path, dict, str]] = []
    for path in chosen:
        data = load_jsonc(path)
        servers = data.get("servers")
        if servers is not None and not isinstance(servers, dict):
            raise ConfigError(f"{path}: 'servers' is not an object")
        data.setdefault("servers", {})
        plans.append((path, data, "servers"))

    portable = load_jsonc(PORTABLE_MCP)
    servers = portable.get("mcpServers")
    if servers is not None and not isinstance(servers, dict):
        raise ConfigError(f"{PORTABLE_MCP}: 'mcpServers' is not an object")
    portable.setdefault("mcpServers", {})
    plans.append((PORTABLE_MCP, portable, "mcpServers"))
    return plans


def install(args: argparse.Namespace) -> int:
    rep = Reporter(args.dry_run)
    python = args.python or find_python()
    ok, detail = check_python_version(python)
    if not ok:
        print(f"error: {detail}", file=sys.stderr)
        print("hint: pass --python /path/to/python3", file=sys.stderr)
        return 1

    # Validate first, write second. Nothing on disk changes if this fails.
    try:
        config_plans = plan_config_writes(python)
    except ConfigError as exc:
        print(f"error: cannot read existing MCP config: {exc}", file=sys.stderr)
        print("hint: fix or move that file, then re-run. Nothing was written.", file=sys.stderr)
        return 1

    print("installing local-memory kit (user scope)")
    print(f"  home:    {Path.home()}")
    print(f"  python:  {python} ({detail})")
    print(f"  store:   {STORE}")
    print(f"  server:  {SERVER_DST}")
    print()

    rep.copy(SERVER_SRC, SERVER_DST)
    if not args.dry_run:
        try:
            SERVER_DST.chmod(0o755)
        except OSError:
            pass

    rep.write(STORE / "MEMORY.md", STORE_INDEX + "\n")
    # Curated memory is user data: initialise it once, never refresh it on
    # reinstall or upgrade. Project partitions get their own MEMORY.md the
    # first time something is promoted into them.
    global_curated = STORE / "global" / "MEMORY.md"
    if not global_curated.exists():
        rep.write(global_curated, GLOBAL_CURATED + "\n")
    if not args.dry_run:
        (STORE / "global").mkdir(parents=True, exist_ok=True)
        (STORE / "projects").mkdir(parents=True, exist_ok=True)

    # Targeted user instruction (applies to every file).
    rep.write(USER_INSTRUCTIONS, TARGETED_INSTRUCTION + "\n")
    # Always-on user instruction, merged without clobbering other content.
    merge_marked_section(ALWAYS_ON, ALWAYS_ON_SECTION, rep)

    # VS Code user-profile configuration uses "servers"; the portable
    # Copilot config uses "mcpServers" and takes no workspace-relative cwd.
    for path, data, key in config_plans:
        data[key][SERVER_ID] = server_entry(python, use_workspace_cwd=(key == "servers"))
        rep.write(path, dump_json(data))

    print()
    print("done." if not args.dry_run else "dry run complete.")
    print("next:")
    print("  1. restart VS Code")
    print("  2. open any repository folder")
    print("  3. MCP: List Servers -> start 'memory'")
    print("  4. in Copilot Chat ask: 'what do you remember about this project?'")
    return 0


def uninstall(args: argparse.Namespace) -> int:
    rep = Reporter(args.dry_run)
    print("removing local-memory kit (user scope)")
    print()

    rep.remove(SERVER_DST)
    # The kit directory is ours and holds nothing else.
    kit_dir = SERVER_DST.parent
    if kit_dir.is_dir() and not any(kit_dir.iterdir()):
        rep.remove(kit_dir)
    rep.remove(USER_INSTRUCTIONS)

    if ALWAYS_ON.exists():
        existing = ALWAYS_ON.read_text(encoding="utf-8")
        if BEGIN_MARK in existing and END_MARK in existing:
            head = existing.split(BEGIN_MARK)[0]
            tail = existing.split(END_MARK, 1)[1]
            cleaned = (head + tail).strip()
            if cleaned:
                rep.write(ALWAYS_ON, cleaned + "\n")
            else:
                rep.remove(ALWAYS_ON)
        else:
            rep.say(f"  keep   {ALWAYS_ON} (no managed section found)")

    for path in vscode_user_mcp_paths():
        if not path.exists():
            continue
        try:
            data = load_jsonc(path)
        except ConfigError as exc:
            rep.say(f"  keep   {path} (unreadable: {exc})")
            continue
        servers = data.get("servers")
        if isinstance(servers, dict) and SERVER_ID in servers:
            del servers[SERVER_ID]
            rep.write(path, dump_json(data))

    if PORTABLE_MCP.exists():
        try:
            data = load_jsonc(PORTABLE_MCP)
        except ConfigError as exc:
            rep.say(f"  keep   {PORTABLE_MCP} (unreadable: {exc})")
            data = {}
        servers = data.get("mcpServers")
        if isinstance(servers, dict) and SERVER_ID in servers:
            del servers[SERVER_ID]
            rep.write(PORTABLE_MCP, dump_json(data))

    # The store holds the user's memories, which are not ours to delete as a
    # side effect of removing the tool. Opt in explicitly with --purge.
    if args.purge:
        rep.remove(STORE)
    elif STORE.exists():
        rep.say(f"  keep   {STORE} (memories kept; run with --purge to delete them)")

    print()
    print("done." if not args.dry_run else "dry run complete.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Install the local-memory kit for the current user.",
    )
    parser.add_argument("--dry-run", action="store_true", help="show what would change")
    parser.add_argument("--uninstall", action="store_true", help="remove what was installed")
    parser.add_argument("--purge", action="store_true",
                        help="with --uninstall, also delete the memory store")
    parser.add_argument("--python", help="path to the python3 interpreter to use")
    args = parser.parse_args()

    if args.purge and not args.uninstall:
        parser.error("--purge only makes sense together with --uninstall")

    if not SERVER_SRC.exists():
        print(f"error: cannot find {SERVER_SRC}", file=sys.stderr)
        return 1

    if args.uninstall:
        return uninstall(args)
    return install(args)


if __name__ == "__main__":
    raise SystemExit(main())
