#!/usr/bin/env python3
"""Local-first memory MCP server for GitHub Copilot (VS Code extension).

Zero external dependencies -- standard library only. Speaks JSON-RPC 2.0 over
stdio, which is what VS Code's MCP client expects.

Memories are stored as plain Markdown files under a memory directory so they
stay diff-able, git-versionable, and readable by any other agent on the
machine. No cloud service, no API key, no network access.

Storage is controlled by two independent environment variables.

Scope separation -- ``MEMORY_LAYOUT``:

    flat        (default) one store per repository, every memory in one
                directory. Produced by the repository-level installer.
    partitioned one shared store for the whole machine, split into
                ``global/`` and one directory per project. Produced by the
                user-level installer.

Organisation inside a scope -- ``MEMORY_ORGANIZE``:

    file        (default) one file per memory, frontmatter header::

                    ---
                    title: <human title>
                    tags: [a, b]
                    updated: 2026-10-22T12:00:00Z
                    ---

                    <body text>

    dated       one file per day, plus a curated file. Layout::

                    <scope-dir>/
                        MEMORY.md          curated, long-lived entries
                        2026-10-21.md     daily log
                        2026-10-22.md

                Every entry inside a dated file is a level-2 heading::

                    ## <title>

                    <!-- tags: a, b -->
                    <!-- updated: 2026-10-22T12:00:00+08:00 -->

                    <body>

                ``memory_write`` appends to today's file by default and
                updates an existing entry in place when the title is already
                known. Use ``bucket: "curated"`` to write to ``MEMORY.md``
                instead -- that is where facts promoted out of the daily
                logs belong. Promoting an existing daily entry moves it to
                ``MEMORY.md`` rather than leaving a second copy behind.
                Level-1 (``#``) and level-3 (``###``) headings are free to
                use inside a body; only ``##`` starts an entry.

A write only ever looks for an existing entry inside the directory it is
about to write to, so a ``project`` write can never rewrite a memory that
lives in the ``global`` partition.

Legacy frontmatter files are still readable in ``dated`` mode, so an
existing store keeps working after switching.

Configure the storage root with the MEMORY_ROOT environment variable.
"""

from __future__ import annotations

import contextlib
import functools
import hashlib
import json
import math
import os
import re
import sys
import tempfile
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "local-memory"
SERVER_VERSION = "1.4.0"

SCOPE_PROJECT = "project"
SCOPE_GLOBAL = "global"
SCOPE_ALL = "all"

BUCKET_DAILY = "daily"
BUCKET_CURATED = "curated"

ORGANIZE_FILE = "file"
ORGANIZE_DATED = "dated"

CURATED_FILENAME = "MEMORY.md"
CURATED_HEADER = "# Curated memory\n\nDurable notes promoted out of the daily logs.\n"
DATE_FILE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ENTRY_RE = re.compile(r"^##[ \t]+(.+?)[ \t]*$", re.MULTILINE)
META_COMMENT_RE = re.compile(r"<!--\s*(tags|updated)\s*:\s*(.*?)\s*-->", re.IGNORECASE)


# --------------------------------------------------------------------------
# Argument hygiene
# --------------------------------------------------------------------------

def normalize_title(title: Any) -> str:
    """Coerce a title to a single line.

    A newline inside a title would open a second ``##`` heading in a dated
    file -- the caller could forge entries it never meant to write -- so
    control characters collapse to spaces before anything is rendered.
    """
    text = unicodedata.normalize("NFKC", str(title or ""))
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    return text or "memory"


def as_tags(value: Any) -> list[str]:
    """Coerce the ``tags`` argument into a list of tag strings.

    Clients do send a comma-separated string even though the schema says
    array; ``list("db,migration")`` would produce a dozen single-character
    tags instead.
    """
    if value is None:
        return []
    if isinstance(value, str):
        parts = re.split(r"[,;\s]+", value)
        return [part.strip() for part in parts if part.strip()]
    if isinstance(value, (list, tuple, set)):
        tags: list[str] = []
        for item in value:
            text = str(item).strip()
            if text:
                tags.append(text)
        return tags
    text = str(value).strip()
    return [text] if text else []


def as_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    """Clamp a numeric argument that may have arrived as a string or null."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


def as_date(value: Any) -> str:
    """Accept only YYYY-MM-DD; anything else is treated as absent."""
    text = str(value or "").strip()
    return text if DATE_FILE_RE.match(text) else ""


def write_text_atomic(path: Path, text: str) -> None:
    """Write via a unique temp file so a crash cannot leave a truncated store."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent,
        prefix=f".{path.name}.", suffix=".tmp", delete=False,
    ) as handle:
        tmp = Path(handle.name)
        handle.write(text)
    try:
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


LOCK_TIMEOUT = 5.0


def lock_path() -> Path:
    """Advisory lock file for this store, kept out of the store itself.

    Two VS Code windows -- or VS Code plus another agent -- can share one
    store, and each write is a read-modify-write of a whole file.

    The lock lives beside the store, not in the temp directory. ``tempfile``
    honours ``TMPDIR``, so two processes started with different values (GUI
    launch, a test harness, a cron job) locked two different files and both
    believed they held the store -- mutual exclusion failed silently. The OS
    also sweeps the temp directory, which can delete a lock a live process
    still holds. Deriving the path from the store makes the mapping
    store <-> lock absolute; the digest keeps one lock per store, and
    ``.locks`` is a dot-directory so it can never be read as a memory
    (the store only reads ``*.md``).
    """
    root = memory_root()
    try:
        key = str(root.resolve())
    except OSError:
        key = str(root)
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
    return root.parent / ".locks" / f"memory-{digest}.lock"


def _lock(fd: int) -> None:
    """Take an exclusive lock on ``fd`` without blocking."""
    if os.name == "nt":
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(fd: int) -> None:
    if os.name == "nt":
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)


@contextlib.contextmanager
def store_lock(timeout: float = LOCK_TIMEOUT):
    """Serialise writers across processes.

    Writers must never fall back to an unlocked read-modify-write: another
    process could then overwrite their entries. A lock failure rejects the
    write and is returned to the caller as a tool error.
    """
    path = lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o644)

    acquired = False
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                _lock(fd)
                acquired = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"could not acquire store lock within {timeout:g}s")
                time.sleep(0.02)
        yield
    finally:
        if acquired:
            try:
                _unlock(fd)
            except OSError:
                pass
        os.close(fd)


def locked(func):
    """Run a write tool with the store lock held."""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            with store_lock():
                return func(*args, **kwargs)
        except OSError as exc:
            return _failed(f"Could not write memory: {exc}")
    return wrapper


# --------------------------------------------------------------------------
# Time
# --------------------------------------------------------------------------

def local_now_iso() -> str:
    """Local time with offset -- a diary entry belongs to a local day."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def local_date() -> str:
    return datetime.now().strftime("%Y-%m-%d")


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------

def memory_root() -> Path:
    """Resolve the memory directory.

    Order: MEMORY_ROOT env var > <cwd>/.copilot/memory. Created on demand.
    """
    env = os.environ.get("MEMORY_ROOT")
    if env:
        root = Path(env).expanduser()
    else:
        root = Path.cwd() / ".copilot" / "memory"
    root.mkdir(parents=True, exist_ok=True)
    return root


def is_partitioned() -> bool:
    """True when the store is shared across projects."""
    return os.environ.get("MEMORY_LAYOUT", "flat").strip().lower() == "partitioned"


def organize_mode() -> str:
    """How memories are stored inside a scope directory.

    ``file`` keeps one file per memory. ``dated`` groups them into one file
    per day plus a curated MEMORY.md, mirroring how a diary-style agent
    memory accumulates over time.
    """
    value = os.environ.get("MEMORY_ORGANIZE", ORGANIZE_FILE).strip().lower()
    if value in (ORGANIZE_DATED, "daily", "journal", "date"):
        return ORGANIZE_DATED
    return ORGANIZE_FILE


SLUG_MAX = 80


def slugify(title: str) -> str:
    """Filesystem-safe slug. Keeps CJK characters intact (they are valid in
    filenames on macOS/Linux and keep the file human-readable).

    A title longer than ``SLUG_MAX`` keeps a readable prefix plus a digest of
    the full title. Truncating alone mapped every title that shared its first
    80 characters onto one file, so the second memory silently overwrote the
    first.
    """
    title = unicodedata.normalize("NFKC", title).strip()
    title = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "", title)
    title = re.sub(r"\s+", "-", title)
    title = re.sub(r"-{2,}", "-", title).strip("-.")
    if not title:
        return "memory"
    if len(title) <= SLUG_MAX:
        return title
    digest = hashlib.sha1(title.encode("utf-8")).hexdigest()[:6]
    head = title[:SLUG_MAX - len(digest) - 1].rstrip("-.")
    return f"{head}-{digest}" if head else f"memory-{digest}"


def split_doc(text: str) -> tuple[dict[str, Any], str]:
    """Parse the optional YAML-ish frontmatter into a dict + body.

    The delimiters are matched as whole lines. Splitting on the substring
    ``---`` treated the ``---`` inside a title (``title: A---B``) as the end
    of the header, so the memory came back with a truncated title and a body
    made of leftover header text.
    """
    meta: dict[str, Any] = {}
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return meta, text
    close = next((index for index in range(1, len(lines))
                  if lines[index].strip() == "---"), None)
    if close is None:
        return meta, text
    header = lines[1:close]
    body = "\n".join(lines[close + 1:]).lstrip("\n")
    for line in header:
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        value = value.strip()
        if key == "tags":
            cleaned = value.strip("[]").strip()
            meta["tags"] = [t.strip() for t in cleaned.split(",") if t.strip()]
        else:
            meta[key] = value
    return meta, body


def render_doc(title: str, body: str, tags: list[str]) -> str:
    # Local time with offset, like the dated entries: ``_doc_date`` trims the
    # date off this stamp, and a UTC stamp filed a memory written at 01:00
    # +08:00 under the previous day.
    stamp = local_now_iso()
    tag_line = ", ".join(tags) if tags else ""
    return (f"---\ntitle: {normalize_title(title)}\ntags: [{tag_line}]\n"
            f"updated: {stamp}\n---\n\n{body.rstrip()}\n")


def read_doc(path: Path) -> dict[str, Any]:
    meta, body = split_doc(path.read_text(encoding="utf-8"))
    meta["name"] = path.stem
    meta["body"] = body.strip()
    meta["path"] = str(path)
    return meta


# --------------------------------------------------------------------------
# Dated entries: parse / render / write
# --------------------------------------------------------------------------

BODY_ENTRY_RE = re.compile(r"^##[ \t]+")


def _prepare_body(body: str) -> str:
    """Make a body safe to drop into a dated file.

    Two things a body can carry would corrupt the file it is written into:

    * a line starting with ``##`` opens a new entry when the file is parsed,
      so Markdown pasted into a memory split one memory into two;
    * an odd number of fence markers leaves a fence open, and every line
      after it -- including every later entry in that day's log -- was
      parsed as code, so the entries vanished from ``list`` and ``read``
      while the write still reported success.

    Headings are demoted one level and an open fence is closed. Lines inside
    a fence are kept exactly as written.
    """
    text = (body or "").strip()
    if not text:
        return ""
    lines = text.splitlines()
    fence: str | None = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if fence is None:
            if stripped.startswith("```") or stripped.startswith("~~~"):
                fence = stripped[:3]
            elif BODY_ENTRY_RE.match(line):
                lines[index] = "### " + line[2:].lstrip()
        elif stripped.startswith(fence):
            fence = None
    if fence is not None:
        lines.append(fence)
    return "\n".join(lines)


def render_entry(title: str, body: str, tags: list[str], updated: str) -> str:
    """Render one entry as a level-2 heading block."""
    lines = [f"## {normalize_title(title)}", ""]
    if tags:
        lines.append(f"<!-- tags: {', '.join(tags)} -->")
    if updated:
        lines.append(f"<!-- updated: {updated} -->")
    lines.append("")
    lines.append(_prepare_body(body))
    lines.append("")
    # Trailing blank line so the next entry starts after a real gap.
    return "\n".join(lines) + "\n"


def _extract_meta(section: str) -> tuple[list[str], str, str]:
    """Pull tags / updated out of the meta comments, return clean body.

    Only the comment block directly under the heading is metadata. Stripping
    every ``<!-- tags: ... -->`` in the section also ate such a comment out
    of the body text and invented a tag the writer never passed.
    """
    tags: list[str] = []
    updated = ""
    lines = section.splitlines()
    index = 0
    while index < len(lines) and not lines[index].strip():
        index += 1
    consumed = 0
    while index < len(lines):
        match = META_COMMENT_RE.fullmatch(lines[index].strip())
        if match is None:
            break
        value = match.group(2).strip()
        if match.group(1).lower() == "tags":
            tags = [t.strip() for t in value.split(",") if t.strip()]
        else:
            updated = value
        index += 1
        consumed += 1
    if not consumed:
        return tags, updated, section.strip()
    return tags, updated, "\n".join(lines[index:]).strip()


def _fenced_lines(text: str) -> set[int]:
    """Line numbers (0-based) that sit inside a fenced code block.

    A ``##`` heading inside a code fence is documentation, not an entry.
    A fence that is never closed is a typo rather than a container: treating
    the rest of the file as code hid every later entry (writes reported
    success while the memory was unreachable), so an unterminated fence is
    ignored and its headings stay visible.
    """
    inside: set[int] = set()
    fence: str | None = None
    opened_at = -1
    for number, line in enumerate(text.splitlines()):
        stripped = line.strip()
        if fence is None:
            if stripped.startswith("```") or stripped.startswith("~~~"):
                fence = stripped[:3]
                opened_at = number
                inside.add(number)
            continue
        inside.add(number)
        if stripped.startswith(fence):
            fence = None
    if fence is not None:
        inside = {number for number in inside if number < opened_at}
    return inside


def parse_dated_file(path: Path, label: str, bucket: str) -> list[dict[str, Any]]:
    """Split a dated file into its individual entries."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    fenced = _fenced_lines(text)
    matches = [
        match for match in ENTRY_RE.finditer(text)
        if text.count("\n", 0, match.start()) not in fenced
    ]
    docs: list[dict[str, Any]] = []
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        section = text[start:end]
        title = match.group(1).strip()
        tags, updated, body = _extract_meta(section)
        docs.append({
            "name": slugify(title),
            "title": title,
            "tags": tags,
            "updated": updated,
            "body": body,
            "path": str(path),
            "scope": label,
            "bucket": bucket,
            "date": path.stem if DATE_FILE_RE.match(path.stem) else "",
            "format": "dated",
        })
    return docs


def _legacy_docs(path: Path, label: str) -> list[dict[str, Any]]:
    """A frontmatter file seen as a single memory."""
    try:
        doc = read_doc(path)
    except OSError:
        return []
    updated = doc.get("updated", "")
    return [{
        "name": doc["name"],
        "title": doc.get("title", doc["name"]),
        "tags": doc.get("tags", []),
        "updated": updated,
        "body": doc.get("body", ""),
        "path": doc["path"],
        "scope": label,
        "bucket": "legacy",
        "date": updated[:10] if updated else "",
        "format": "legacy",
    }]


def _is_legacy_file(path: Path) -> bool:
    """True when a file uses the frontmatter (one memory per file) format.

    The opening delimiter must be a line of its own -- a file that merely
    starts with ``---`` is a dated log whose first entry happened to begin
    with a horizontal rule, not a frontmatter document.
    """
    try:
        with path.open("r", encoding="utf-8") as handle:
            return handle.readline().strip() == "---"
    except OSError:
        return False


def scoped_docs(path: Path, label: str) -> list[dict[str, Any]]:
    """Interpret one file according to the current organisation mode."""
    if organize_mode() == ORGANIZE_FILE:
        if path.name == CURATED_FILENAME:
            return []  # index document, not a memory
        return _legacy_docs(path, label)
    if path.name == CURATED_FILENAME:
        return parse_dated_file(path, label, BUCKET_CURATED)
    if DATE_FILE_RE.match(path.stem):
        return parse_dated_file(path, label, BUCKET_DAILY)
    if _is_legacy_file(path):
        return _legacy_docs(path, label)
    return parse_dated_file(path, label, BUCKET_DAILY)


def append_entry(path: Path, title: str, body: str, tags: list[str],
                 updated: str, header: str = "") -> None:
    """Append an entry to a dated file, creating it when needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = ""
    if path.exists():
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            text = ""
    if not text.strip():
        text = header
    if text and not text.endswith("\n"):
        text += "\n"
    # One blank line between entries; render_entry already ends with one.
    if text.strip() and not text.endswith("\n\n"):
        text += "\n"
    text += render_entry(title, body, tags, updated)
    write_text_atomic(path, text)


def _entry_matches(text: str) -> list[re.Match[str]]:
    """Entry headings in a dated file, ignoring headings inside code fences."""
    fenced = _fenced_lines(text)
    return [
        match for match in ENTRY_RE.finditer(text)
        if text.count("\n", 0, match.start()) not in fenced
    ]


def upsert_entry(path: Path, title: str, body: str, tags: list[str],
                 updated: str) -> bool:
    """Replace the entry with this title in place. False when not found."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    matches = _entry_matches(text)
    target = normalize_title(title)
    for index, match in enumerate(matches):
        if normalize_title(match.group(1)) != target:
            continue
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        replacement = render_entry(target, body, tags, updated)
        write_text_atomic(path, text[:start] + replacement + text[end:])
        return True
    return False


def delete_entry(path: Path, title: str) -> bool:
    """Remove the entry with this title from a dated file."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    matches = _entry_matches(text)
    target = normalize_title(title)
    for index, match in enumerate(matches):
        if normalize_title(match.group(1)) != target:
            continue
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        # Only the seam is tidied: collapsing runs of blank lines across the
        # whole file would eat intentional spacing inside other entries.
        head = text[:start].rstrip("\n")
        tail = text[end:].lstrip("\n")
        if head and tail:
            remainder = head + "\n\n" + tail
        else:
            remainder = head or tail
        write_text_atomic(path, remainder.rstrip("\n") + "\n")
        return True
    return False


def drop_if_empty(path: Path) -> None:
    """Delete an untouched generated daily log that no longer holds entries."""
    if path.name == CURATED_FILENAME or not path.exists():
        return
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    if _entry_matches(text) or text.strip() != f"# {path.stem}":
        return
    try:
        path.unlink()
    except OSError:
        pass


# --------------------------------------------------------------------------
# Project partitioning (shared store mode)
# --------------------------------------------------------------------------

def _project_base() -> Path | None:
    """The directory that identifies the current project.

    Nearest ancestor holding a ``.git`` entry wins, so sub-folders of a
    repository share one partition. Falls back to the working directory.
    """
    try:
        cwd = Path.cwd()
    except OSError:
        return None
    for candidate in [cwd, *cwd.parents]:
        try:
            if (candidate / ".git").exists():
                return candidate
        except OSError:
            continue
    return cwd


def project_id() -> str:
    """Stable identifier for the current project.

    ``MEMORY_PROJECT`` wins when set. Otherwise the project name is the
    directory name plus a short digest of its absolute path, so two
    checkouts of the same repository name do not collide.
    """
    override = os.environ.get("MEMORY_PROJECT", "").strip()
    if override:
        return slugify(override)
    base = _project_base()
    if base is None:
        return "default"
    try:
        absolute = str(base.resolve())
    except OSError:
        absolute = str(base)
    digest = hashlib.sha1(absolute.encode("utf-8")).hexdigest()[:6]
    return f"{slugify(base.name)}-{digest}"


def project_dir() -> Path:
    """Partition directory for the current project."""
    path = memory_root() / "projects" / project_id()
    path.mkdir(parents=True, exist_ok=True)
    return path


def global_dir() -> Path:
    """Partition directory for cross-project memories."""
    path = memory_root() / "global"
    path.mkdir(parents=True, exist_ok=True)
    return path


def all_project_dirs() -> list[Path]:
    base = memory_root() / "projects"
    if not base.is_dir():
        return []
    return sorted(p for p in base.iterdir() if p.is_dir())


def _write_project_meta(directory: Path, name: str, origin: str) -> None:
    """Record where a partition came from, for traceability.

    Written next to the memories rather than into the directory itself --
    ``write_text`` on a directory path always fails, which is how the earlier
    version managed to do nothing at all.
    """
    path = directory / "project.json"
    payload = json.dumps({"name": name, "path": origin}, ensure_ascii=False, indent=2) + "\n"
    try:
        if path.exists() and path.read_text(encoding="utf-8") == payload:
            return
        write_text_atomic(path, payload)
    except OSError:
        pass


def _scope_dirs(scope: str) -> list[tuple[Path, str]]:
    """Directories to search for a scope, in priority order.

    Returns (directory, scope_label) pairs. In flat mode the single store
    directory is used for every scope.
    """
    if not is_partitioned():
        return [(memory_root(), "flat")]
    if scope == SCOPE_GLOBAL:
        return [(global_dir(), SCOPE_GLOBAL)]
    if scope == SCOPE_ALL:
        pairs = [(project_dir(), SCOPE_PROJECT), (global_dir(), SCOPE_GLOBAL)]
        current = project_dir()
        for path in all_project_dirs():
            if path != current:
                pairs.append((path, SCOPE_PROJECT))
        return pairs
    return [(project_dir(), SCOPE_PROJECT), (global_dir(), SCOPE_GLOBAL)]


def docs_in_directory(directory: Path, label: str) -> list[dict[str, Any]]:
    """Every memory stored directly in one directory, in filename order."""
    docs: list[dict[str, Any]] = []
    if not directory.is_dir():
        return docs
    for path in sorted(directory.glob("*.md")):
        if path.is_file():
            docs.extend(scoped_docs(path, label))
    return docs


def collect_docs(scope: str = SCOPE_PROJECT) -> list[dict[str, Any]]:
    """Every memory visible in a scope, as individual documents."""
    docs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for directory, label in _scope_dirs(scope):
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.md")):
            if not path.is_file():
                continue
            key = str(path.resolve()) if path.exists() else str(path)
            if key in seen:
                continue
            seen.add(key)
            docs.extend(scoped_docs(path, label))
    return docs


def _partition_for(scope: str) -> tuple[Path, str]:
    """Directory a new memory should be written to."""
    if not is_partitioned():
        return memory_root(), "flat"
    if scope == SCOPE_GLOBAL:
        return global_dir(), SCOPE_GLOBAL
    path = project_dir()
    base = _project_base()
    if base is not None:
        _write_project_meta(path, base.name, str(base))
    return path, SCOPE_PROJECT


# --------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------
#
# Ranking is BM25 over a term index. The previous counter scored raw keyword
# occurrences (3x title + 1x tag + 0.5x body) with neither a document-length
# normalisation nor any notion of how common a term is, and it took every
# single CJK character as a term. Three failure modes came out of that, all
# reproducible:
#
#   * a long note that repeats one word outranked the memory that answers the
#     question -- a body with "database" twelve times beat a note that
#     actually said "database migration";
#   * one shared character was enough to pull in an unrelated memory, so
#     "池化" matched "数据可视化" through 化, and "数据库" could not be told
#     apart from "数据可视化";
#   * "score" never matched "scored" or "scoring", "migration" never matched
#     "migrations": ordinary English queries silently returned nothing.
#
# Terms are whole Latin words (suffix-folded) and CJK bigrams taken *inside*
# one run of CJK characters. Bigrams never straddle a space, punctuation or a
# Latin word: gluing every CJK character in the text together first invented
# "本迁" out of "回滚脚本 迁移", which then matched "版本迁移".

STOPWORDS = frozenset("""
a an and are as at be been but by can for from had has have how if in into is
it its of on or our that the their then there these they this to was were
what when where which who why will with would you your
""".split())

# BM25 knobs. k1 controls term-frequency saturation, b how hard a long
# document is penalised. The defaults are the usual ones; they are named
# because a store that is mostly one-line rules and one that is mostly long
# postmortems want different values.
BM25_K1 = 1.2
BM25_B = 0.75

# A term in the title is worth this many occurrences of the same term in the
# body, likewise a tag. Field weighting by repetition keeps a single term
# index instead of three.
TITLE_WEIGHT = 3
TAG_WEIGHT = 2

# A lone CJK character ("锁", "化") carries far less signal than a bigram, so
# it counts for less when it is all a query gives us.
WEAK_TERM_WEIGHT = 0.4

# Hits this far below the best hit are dropped. Without a floor the tail of
# every result list was noise: any single shared character scored above zero.
# 0.40 also drops a memory that matched one word of a two-word query, which
# is the case that made "database migration" list an unrelated ops note.
RELATIVE_FLOOR = 0.40

CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uf900-\ufaff]+")
WORD_RE = re.compile(r"[a-z0-9]+")
WORD_SEPARATOR_RE = re.compile(r"[_./\\-]+")


def _is_weak(term: str) -> bool:
    """True for a single CJK character: a weak, ambiguous query term."""
    return len(term) == 1 and CJK_RE.match(term) is not None


def _stem_variants(word: str) -> tuple[str, ...]:
    """Fold the common English inflections onto one form.

    Both the index and the query run through this, so agreement between the
    two is what matters, not linguistic correctness. A stripped stem also
    gets its ``e`` back, which is what lets "writing" and "write" meet in the
    middle. Irregular forms ("wrote", "written") and derivations that change
    the stem ("write" vs "writer") are still separate terms.
    """
    if len(word) <= 3 or not word.isalpha():
        return (word,)
    base = word
    restored = False
    if base.endswith("ies") and len(base) > 4:
        base = base[:-3] + "y"
    elif base.endswith("sses"):
        base = base[:-2]
    elif base.endswith(("xes", "zes", "ches", "shes", "ses")):
        base = base[:-2]
    elif base.endswith("s") and not base.endswith(("ss", "us", "is")):
        base = base[:-1]
    if base.endswith("ing") and len(base) > 5:
        base, restored = base[:-3], True
    elif base.endswith("ed") and len(base) > 4:
        base, restored = base[:-2], True
    elif base.endswith("er") and len(base) > 5:
        # "writers" -> "writer" -> "write". Without this a query for "write"
        # found nothing while "write" itself was a term: the agent verbs
        # ("reader", "manager", "handler") all live one derivation away.
        base, restored = base[:-2], True
    if len(base) > 3 and base[-1] == base[-2] and base[-1] not in "lssz":
        base = base[:-1]  # running -> runn -> run
    if not base:
        return (word,)
    return (base, base + "e") if restored else (base,)


def term_units(text: str, unigrams: bool = False) -> list[list[str]]:
    """Split text into units, each carrying the terms it contributes.

    A unit is one Latin word or one run of CJK characters. Keeping the units
    lets the caller ask "how much of the query did this memory actually
    cover", which is what stops a document that matches a single word from
    outranking one that matches the whole phrase.

    ``unigrams`` adds every CJK character of a run as well as its bigrams.
    The index asks for them so that a one-character query ("锁") still finds
    the note that says "锁文件路径"; a query does not, because expanding
    "池化" into 池 + 化 is exactly what made it match "数据可视化".
    """
    text = unicodedata.normalize("NFKC", str(text or "")).lower()
    text = WORD_SEPARATOR_RE.sub(" ", text)
    units: list[list[str]] = []
    for match in WORD_RE.finditer(text):
        word = match.group(0)
        if word in STOPWORDS:
            continue
        if len(word) < 2 and not word.isdigit():
            continue
        units.append(list(_stem_variants(word)))
    for match in CJK_RE.finditer(text):
        run = match.group(0)
        if len(run) == 1:
            units.append([run])
            continue
        units.append([run[i:i + 2] for i in range(len(run) - 1)])
        if unigrams:
            units.append(list(run))
    return units


def tokenize(text: str) -> list[str]:
    """Every term this text contributes, in order. Debug aid for a store."""
    return [term for unit in term_units(text) for term in unit]


def _squash(text: str) -> str:
    """Whitespace-free, case-folded text, for phrase containment checks."""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(text or "")).lower())


def _phrase_bonus(query: str, title: str, body: str) -> float:
    """Reward a memory that contains the query as a phrase, not a bag of words.

    BM25 alone cannot express "these words next to each other": a note that
    merely mentions the same two words anywhere ranks alongside one titled
    with them. Whitespace is squashed so "数据库 迁移" still matches
    "数据库迁移".
    """
    needle = _squash(query)
    if len(needle) < 2:
        return 0.0
    if needle in _squash(title):
        return 3.0
    if len(body) <= 20000 and needle in _squash(body):
        return 1.5
    return 0.0


def doc_terms(doc: dict[str, Any]) -> dict[str, int]:
    """Field-weighted term frequencies for one memory."""
    counts: dict[str, int] = {}
    for field, weight in ((doc.get("title", ""), TITLE_WEIGHT),
                          (" ".join(doc.get("tags", [])), TAG_WEIGHT),
                          (doc.get("body", ""), 1)):
        for unit in term_units(field, unigrams=True):
            for term in unit:
                counts[term] = counts.get(term, 0) + weight
    return counts


def _doc_date(doc: dict[str, Any]) -> str:
    if doc.get("date"):
        return doc["date"]
    updated = doc.get("updated", "")
    return updated[:10] if updated else ""


def _dedupe_key(doc: dict[str, Any]) -> str:
    """Group memories that record the same fact twice.

    Writing the same rule on three consecutive days left three entries with
    near-identical titles, and search returned all three at the top -- five
    slots spent on one fact. An identical body is the same fact regardless of
    the title, so that is the key; a short body falls back to the title slug
    because two one-word bodies matching by accident is more likely than two
    memories truly duplicating a ten-character note.
    """
    body = re.sub(r"\s+", " ", doc.get("body", "")).strip().lower()
    if len(body) >= 24:
        return hashlib.sha1(body.encode("utf-8")).hexdigest()
    return slugify(doc.get("title", ""))


def search_memories(query: str, limit: int = 5, scope: str = SCOPE_PROJECT,
                    since: str = "", until: str = "") -> list[dict[str, Any]]:
    """Rank memories against ``query`` with BM25 plus a phrase bonus."""
    query = str(query or "")
    units = term_units(query)
    q_terms = {term for unit in units for term in unit}
    if not q_terms:
        # A query made only of stopwords ("the", "how") carries no signal.
        # Returning the whole store for it looked like a working search.
        return []

    dated: list[tuple[dict[str, Any], str]] = []
    for doc in collect_docs(scope):
        date = _doc_date(doc)
        if since or until:
            # A memory with no usable date cannot satisfy a date range.
            # Skipping the check instead let undated notes through a filter
            # that explicitly excluded them.
            if not date:
                continue
            if since and date < since:
                continue
            if until and date > until:
                continue
        dated.append((doc, date))
    if not dated:
        return []

    indexed: list[tuple[dict[str, Any], str, dict[str, int], int]] = []
    freq: dict[str, int] = {}
    total_terms = 0
    for doc, date in dated:
        counts = doc_terms(doc)
        length = sum(counts.values())
        total_terms += length
        for term in counts:
            freq[term] = freq.get(term, 0) + 1
        indexed.append((doc, date, counts, length))
    total = len(indexed)
    avgdl = (total_terms / total) or 1.0

    scored: list[tuple[float, dict[str, Any], str]] = []
    for doc, date, counts, length in indexed:
        value = 0.0
        matched = 0
        for term in q_terms:
            tf = counts.get(term, 0)
            if not tf:
                continue
            seen = freq.get(term, 0)
            idf = math.log(1.0 + (total - seen + 0.5) / (seen + 0.5))
            weight = WEAK_TERM_WEIGHT if _is_weak(term) else 1.0
            value += (weight * idf * tf * (BM25_K1 + 1)
                      / (tf + BM25_K1 * (1 - BM25_B + BM25_B * length / avgdl)))
            matched += 1
        if not matched:
            continue
        covered = sum(1 for unit in units if any(counts.get(term) for term in unit))
        coverage = covered / len(units)
        value = value * (0.5 + 0.5 * coverage)
        value += _phrase_bonus(query, doc.get("title", ""), doc.get("body", ""))
        scored.append((value, doc, date))

    if not scored:
        return []
    # Newest wins a tie, then the stable sort keeps file order.
    scored.sort(key=lambda item: item[2], reverse=True)
    scored.sort(key=lambda item: item[0], reverse=True)

    floor = scored[0][0] * RELATIVE_FLOOR
    results: list[dict[str, Any]] = []
    by_key: dict[str, dict[str, Any]] = {}
    for value, doc, date in scored:
        if value < floor:
            break
        key = _dedupe_key(doc)
        if key in by_key:
            by_key[key]["also"].append(doc.get("title", doc["name"]))
            continue
        item = {
            "name": doc["name"],
            "title": doc.get("title", doc["name"]),
            "tags": doc.get("tags", []),
            "updated": doc.get("updated", ""),
            "date": date,
            "bucket": doc.get("bucket", ""),
            "scope": doc.get("scope", ""),
            "score": round(value, 3),
            "excerpt": excerpt_around(doc.get("body", ""), query),
            "path": doc.get("path", ""),
            "also": [],
        }
        by_key[key] = item
        results.append(item)
        if len(results) >= limit:
            break
    return results


def excerpt_around(body: str, query: str, width: int = 220) -> str:
    """Return the window of body text that holds the densest query matches.

    Anchoring on the first single character that happened to appear put the
    snippet on an unrelated sentence: the old token list began with lone CJK
    characters, so "缓存" centred on the 本 of "本项目" instead of the
    paragraph that talks about caching.
    """
    if not body:
        return ""
    terms = [term for unit in term_units(query) for term in unit if not _is_weak(term)]
    if not terms:
        terms = tokenize(query)
    if not terms:
        return body[:width].replace("\n", " ").strip()

    lowered = body.lower()
    best_pos, best_score = -1, 0
    for term in terms[:12]:
        start = 0
        for _ in range(40):  # a very common term must not scan the whole body
            pos = lowered.find(term, start)
            if pos < 0:
                break
            window = lowered[max(0, pos - width // 3):pos + width]
            hits = sum(1 for candidate in terms if candidate in window)
            if hits > best_score:
                best_pos, best_score = pos, hits
            start = pos + 1
        if best_score == len(terms):
            break
    if best_pos < 0:
        return body[:width].replace("\n", " ").strip()
    start = max(0, best_pos - width // 3)
    end = min(len(body), start + width)
    snippet = body[start:end].replace("\n", " ").strip()
    if start > 0:
        snippet = "..." + snippet
    if end < len(body):
        snippet = snippet + "..."
    return snippet


# --------------------------------------------------------------------------
# Tool implementations
# --------------------------------------------------------------------------

def _normalize_scope(scope: Any) -> str:
    value = str(scope or "").strip().lower()
    if value in (SCOPE_PROJECT, SCOPE_GLOBAL, SCOPE_ALL):
        return value
    return SCOPE_PROJECT


def _normalize_bucket(bucket: Any) -> str:
    value = str(bucket or "").strip().lower()
    if value in (BUCKET_DAILY, BUCKET_CURATED):
        return value
    return BUCKET_DAILY


def _find_by_title(title: str, directory: Path, label: str) -> dict[str, Any] | None:
    """Find an existing memory with this title *inside one directory*.

    Deliberately not scope-wide: ``project`` scope also covers the global
    partition, so searching it here let a project write rewrite a global
    memory that happened to share the title.
    """
    target = normalize_title(title)
    for doc in docs_in_directory(directory, label):
        if normalize_title(doc.get("title", "")) == target:
            return doc
    return None


def _result(action: str, name: str, title: str, tags: list[str], scope: str,
            bucket: str, date: str, path: Path) -> dict[str, Any]:
    return {
        "content": [{
            "type": "text",
            "text": json.dumps({
                "action": action,
                "name": name,
                "title": title,
                "tags": tags,
                "scope": scope,
                "bucket": bucket,
                "date": date,
                "path": str(path),
            }, ensure_ascii=False, indent=2),
        }],
        "isError": False,
    }


def _failed(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def tool_memory_search(query: str, limit: int = 5, scope: str = SCOPE_PROJECT,
                       since: str = "", until: str = "") -> dict[str, Any]:
    since = str(since or "").strip()
    until = str(until or "").strip()
    for label, value in (("since", since), ("until", until)):
        if value and not as_date(value):
            return _failed(f"'{label}' must be YYYY-MM-DD, got {value!r}.")
    results = search_memories(str(query or ""), as_int(limit, 5, 1, 20),
                              _normalize_scope(scope), since, until)
    return {
        "content": [{
            "type": "text",
            "text": json.dumps(results, ensure_ascii=False, indent=2),
        }],
        "isError": False,
    }


@locked
def tool_memory_write(title: str, content: str, tags: list[str] | None = None,
                      scope: str = SCOPE_PROJECT, bucket: str = BUCKET_DAILY) -> dict[str, Any]:
    tags = as_tags(tags)
    title = normalize_title(title)
    content = str(content or "")
    scope = _normalize_scope(scope)
    bucket = _normalize_bucket(bucket)
    stamp = local_now_iso()
    directory, label = _partition_for(scope)

    if organize_mode() == ORGANIZE_DATED:
        existing = _find_by_title(title, directory, label)
        if existing is not None:
            path = Path(existing["path"])
            merged = tags or as_tags(existing.get("tags"))
            old_title = normalize_title(existing.get("title", title))
            existing_scope = existing.get("scope", label)
            if existing.get("format") == "legacy":
                write_text_atomic(path, render_doc(old_title, content, merged))
                return _result("updated", slugify(old_title), old_title, merged,
                               existing_scope, existing.get("bucket", ""),
                               existing.get("date", ""), path)
            if bucket == BUCKET_CURATED and existing.get("bucket") != BUCKET_CURATED:
                # Promotion: the fact now belongs in MEMORY.md, so move it
                # there instead of quietly updating a daily log. If the
                # curated file already carries the title, update it in place
                # rather than appending a second copy.
                target = directory / CURATED_FILENAME
                if not upsert_entry(target, old_title, content, merged, stamp):
                    append_entry(target, old_title, content, merged, stamp,
                                 header=CURATED_HEADER)
                delete_entry(path, old_title)
                drop_if_empty(path)
                return _result("updated", slugify(old_title), old_title, merged,
                               existing_scope, BUCKET_CURATED, "", target)
            upsert_entry(path, old_title, content, merged, stamp)
            return _result("updated", existing.get("name", slugify(old_title)),
                           old_title, merged, existing_scope,
                           existing.get("bucket", ""), existing.get("date", ""), path)

        if bucket == BUCKET_CURATED:
            target = directory / CURATED_FILENAME
            header = CURATED_HEADER
        else:
            target = directory / f"{local_date()}.md"
            header = f"# {local_date()}\n"
        append_entry(target, title, content, tags, stamp, header=header)
        return _result("created", slugify(title), title, tags, label, bucket,
                       local_date() if bucket == BUCKET_DAILY else "", target)

    # One file per memory. ``bucket`` has no meaning here: every memory is its
    # own file, so it is reported as empty rather than echoed back.
    base = slugify(title)
    path = directory / f"{base}.md"
    digest = hashlib.sha1(title.encode("utf-8")).hexdigest()[:8]
    collisions = 0
    while path.exists():
        old_title = normalize_title(read_doc(path).get("title", ""))
        if old_title == title:
            break
        # Sanitisation is not injective (for example ``a/b`` and ``ab``).
        # A natural title may itself occupy the disambiguated path, so keep
        # looking until the path is free or belongs to this exact title.
        collisions += 1
        suffix = digest if collisions == 1 else f"{digest}-{collisions}"
        path = directory / f"{base}-{suffix}.md"
    existed = path.exists()
    # Preserve the original title on overwrite so renaming the file is a
    # deliberate act rather than an accidental one.
    if existed:
        old = read_doc(path)
        title = normalize_title(old.get("title", title))
        tags = tags or as_tags(old.get("tags"))
    write_text_atomic(path, render_doc(title, content, tags))
    return _result("updated" if existed else "created", path.stem, title, tags,
                   label, "", "", path)


def _locate(name: str, scope: str = SCOPE_PROJECT) -> dict[str, Any] | None:
    """Find a memory by title inside ``scope``. Exact title wins over slug."""
    target = normalize_title(name)
    target_slug = slugify(target)
    fallback = None
    for doc in collect_docs(scope):
        if normalize_title(doc.get("title", "")) == target:
            return doc
        if fallback is None and slugify(doc.get("title", "")) == target_slug:
            fallback = doc
    return fallback


def tool_memory_read(name: str, scope: str = SCOPE_PROJECT) -> dict[str, Any]:
    target = normalize_title(name)
    scope = _normalize_scope(scope)
    if DATE_FILE_RE.match(target):
        docs = [d for d in collect_docs(scope) if d.get("date") == target]
        if docs:
            return {
                "content": [{
                    "type": "text",
                    "text": json.dumps({
                        "date": target,
                        "count": len(docs),
                        "memories": [
                            {
                                "title": d.get("title", ""),
                                "tags": d.get("tags", []),
                                "updated": d.get("updated", ""),
                                "scope": d.get("scope", ""),
                                "bucket": d.get("bucket", ""),
                                "body": d.get("body", ""),
                                "path": d.get("path", ""),
                            }
                            for d in docs
                        ],
                    }, ensure_ascii=False, indent=2),
                }],
                "isError": False,
            }
    doc = _locate(target, scope)
    if doc is None:
        return _failed(f"No memory named '{name}'.")
    return {
        "content": [{
            "type": "text",
            "text": json.dumps(doc, ensure_ascii=False, indent=2),
        }],
        "isError": False,
    }


def tool_memory_list(scope: str = SCOPE_PROJECT, date: str = "",
                     bucket: str = "") -> dict[str, Any]:
    # A filter that cannot be understood is an error, not a silent widening:
    # ``date="2026/01/01"`` used to drop the filter and list everything, which
    # reads as "nothing matched that day" when it means "bad argument".
    date = str(date or "").strip()
    if date and not as_date(date):
        return _failed(f"'date' must be YYYY-MM-DD, got {date!r}.")
    bucket = str(bucket or "").strip().lower()
    if bucket and bucket not in (BUCKET_DAILY, BUCKET_CURATED):
        return _failed(
            f"'bucket' must be '{BUCKET_DAILY}' or '{BUCKET_CURATED}', got {bucket!r}.")
    items = []
    for doc in collect_docs(_normalize_scope(scope)):
        if date and _doc_date(doc) != date:
            continue
        if bucket and doc.get("bucket", "") != bucket:
            continue
        items.append({
            "name": doc["name"],
            "title": doc.get("title", doc["name"]),
            "tags": doc.get("tags", []),
            "updated": doc.get("updated", ""),
            "date": _doc_date(doc),
            "bucket": doc.get("bucket", ""),
            "scope": doc.get("scope", ""),
            "path": doc.get("path", ""),
            "preview": doc.get("body", "")[:120].replace("\n", " "),
        })
    return {
        "content": [{
            "type": "text",
            "text": json.dumps({"count": len(items), "memories": items},
                               ensure_ascii=False, indent=2),
        }],
        "isError": False,
    }


@locked
def tool_memory_delete(name: str) -> dict[str, Any]:
    doc = _locate(name)
    if doc is None:
        return _failed(f"No memory named '{name}'.")
    path = Path(doc["path"])
    if doc.get("format") == "dated":
        try:
            deleted = delete_entry(path, doc.get("title", ""))
            drop_if_empty(path)
        except OSError as exc:
            return _failed(f"Could not delete memory '{name}': {exc}")
        if not deleted:
            return _failed(f"Could not delete memory '{name}' from {path}.")
    else:
        try:
            path.unlink()
        except OSError as exc:
            return _failed(f"Could not delete memory '{name}': {exc}")
    return {
        "content": [{"type": "text", "text": json.dumps({"deleted": doc.get("name", "")},
                                                       ensure_ascii=False)}],
        "isError": False,
    }


# --------------------------------------------------------------------------
# MCP plumbing
# --------------------------------------------------------------------------

SCOPE_NOTE = (
    "Scope: 'project' (default) covers the current project plus global "
    "memories; 'global' covers only cross-project memories; 'all' covers "
    "every project in a shared store."
)

ORGANIZE_NOTE = (
    "In dated mode each memory lives in a per-day file (or the curated "
    "MEMORY.md) as a level-2 heading; write to today's log by default and "
    "use bucket 'curated' for long-lived facts. A body line that starts with "
    "'## ' is demoted to '### ' (and a dangling code fence is closed) so one "
    "memory can never split into or swallow another."
)

TOOLS = [
    {
        "name": "memory_search",
        "description": (
            "Search stored memories by keyword. Call this BEFORE planning any "
            "non-trivial task to recall prior decisions, conventions, and "
            "lessons learned. Returns scored hits with excerpts. " + SCOPE_NOTE
            + " " + ORGANIZE_NOTE
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Keywords or a question."},
                "limit": {"type": "integer", "description": "Max hits (default 5).", "minimum": 1, "maximum": 20},
                "scope": {
                    "type": "string",
                    "enum": [SCOPE_PROJECT, SCOPE_GLOBAL, SCOPE_ALL],
                    "description": "Which memories to search. Default 'project'.",
                },
                "since": {"type": "string", "description": "Only memories dated on or after YYYY-MM-DD. Memories with no date are excluded."},
                "until": {"type": "string", "description": "Only memories dated on or before YYYY-MM-DD. Memories with no date are excluded."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "memory_write",
        "description": (
            "Store a memory: a durable fact, decision, convention, or lesson. "
            "Write to memory when the user states a preference or a rule that "
            "should apply in future sessions, or when a task resolves an "
            "ambiguity worth not repeating. Use scope 'global' only for rules "
            "that genuinely apply to every project. " + ORGANIZE_NOTE
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Short human-readable title."},
                "content": {"type": "string", "description": "The memory body: what and why."},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "Optional tags."},
                "scope": {
                    "type": "string",
                    "enum": [SCOPE_PROJECT, SCOPE_GLOBAL],
                    "description": "'project' (default) or 'global'.",
                },
                "bucket": {
                    "type": "string",
                    "enum": [BUCKET_DAILY, BUCKET_CURATED],
                    "description": "'daily' (default) appends to today's log; "
                                   "'curated' writes to the long-lived MEMORY.md.",
                },
            },
            "required": ["title", "content"],
        },
    },
    {
        "name": "memory_read",
        "description": (
            "Read one memory in full by title. Passing a date (YYYY-MM-DD) "
            "returns every memory recorded that day. " + SCOPE_NOTE
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "scope": {
                    "type": "string",
                    "enum": [SCOPE_PROJECT, SCOPE_GLOBAL, SCOPE_ALL],
                    "description": "Which partition to read from. Default 'project'.",
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "memory_list",
        "description": (
            "List stored memories with title, tags, date, bucket, scope, and "
            "a short preview. Filter with date (YYYY-MM-DD) or bucket."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "enum": [SCOPE_PROJECT, SCOPE_GLOBAL, SCOPE_ALL],
                    "description": "Default 'project'.",
                },
                "date": {"type": "string", "description": "Only memories from this YYYY-MM-DD."},
                "bucket": {
                    "type": "string",
                    "enum": [BUCKET_DAILY, BUCKET_CURATED],
                    "description": "Only memories in this bucket.",
                },
            },
        },
    },
    {
        "name": "memory_delete",
        "description": "Delete a memory by title. Use only when explicitly asked.",
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
]

DISPATCH = {
    "memory_search": lambda a: tool_memory_search(
        a.get("query", ""), a.get("limit", 5), a.get("scope", SCOPE_PROJECT),
        a.get("since", ""), a.get("until", "")),
    "memory_write": lambda a: tool_memory_write(
        a.get("title", ""), a.get("content", ""), a.get("tags"),
        a.get("scope", SCOPE_PROJECT), a.get("bucket", BUCKET_DAILY)),
    "memory_read": lambda a: tool_memory_read(a.get("name", ""), a.get("scope", SCOPE_PROJECT)),
    "memory_list": lambda a: tool_memory_list(
        a.get("scope", SCOPE_PROJECT), a.get("date", ""), a.get("bucket", "")),
    "memory_delete": lambda a: tool_memory_delete(a.get("name", "")),
}


def send(msg: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def respond(req_id: Any, result: dict[str, Any]) -> None:
    send({"jsonrpc": "2.0", "id": req_id, "result": result})


def error(req_id: Any, code: int, message: str) -> None:
    send({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})


MEMORY_PROTOCOL = (
    "This server is the project's persistent memory. Call memory_search with "
    "the task's key terms before planning non-trivial work; a hit records what "
    "was actually decided here and overrides your defaults. Call memory_write "
    "when the user states a preference or rule, when a decision resolves an "
    "ambiguity, or when you learn a non-obvious fact. Update an existing memory "
    "instead of adding a contradictory one, and never store secrets."
)


def handle(msg: dict[str, Any]) -> None:
    method = msg.get("method", "")
    req_id = msg.get("id")
    params = msg.get("params") or {}

    if method == "initialize":
        respond(req_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": MEMORY_PROTOCOL,
        })
    elif method in ("notifications/initialized", "initialized"):
        return  # notification, no reply
    elif method == "ping":
        respond(req_id, {})
    elif method == "tools/list":
        respond(req_id, {"tools": TOOLS})
    elif method == "tools/call":
        name = params.get("name", "")
        args = params.get("arguments") or {}
        handler = DISPATCH.get(name)
        if handler is None:
            error(req_id, -32602, f"Unknown tool: {name}")
            return
        try:
            respond(req_id, handler(args))
        except Exception as exc:  # noqa: BLE001 - surface errors to the client
            respond(req_id, {
                "content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
                "isError": True,
            })
    elif req_id is not None:
        error(req_id, -32601, f"Method not found: {method}")


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as exc:
            error(None, -32700, f"Parse error: {exc.msg}")
            continue
        if not isinstance(msg, dict):
            # A batch array or a bare scalar. Answering keeps the loop alive;
            # the previous version crashed while reporting the crash.
            error(None, -32600, "Invalid Request: expected a JSON object")
            continue
        try:
            handle(msg)
        except Exception as exc:  # noqa: BLE001 - never die on one bad message
            req_id = msg.get("id")
            if req_id is not None:
                error(req_id, -32603, f"{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
