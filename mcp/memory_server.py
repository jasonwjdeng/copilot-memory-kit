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
import os
import re
import sys
import tempfile
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "local-memory"
SERVER_VERSION = "1.2.0"

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
    """Write via a temp file so a crash cannot leave a truncated store."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


LOCK_TIMEOUT = 5.0


def lock_path() -> Path:
    """Advisory lock file for this store, kept out of the store itself.

    Two VS Code windows -- or VS Code plus another agent -- can share one
    store, and each write is a read-modify-write of a whole file. Parking the
    lock next to the memories would put a `.lock` file in the user's repo,
    so it lives in the temp directory under a digest of the store path.
    """
    root = memory_root()
    try:
        key = str(root.resolve())
    except OSError:
        key = str(root)
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
    return Path(tempfile.gettempdir()) / f"copilot-memory-{digest}.lock"


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

    Best effort by design: on timeout the caller proceeds without the lock and
    says so on stderr. A held lock that never releases would otherwise block
    the tool call -- and with it the editor session -- forever.
    """
    try:
        path = lock_path()
        fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o644)
    except OSError as exc:
        print(f"memory: cannot open lock file: {exc}", file=sys.stderr)
        yield
        return

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
                    print(f"memory: lock timeout after {timeout:g}s; "
                          "writing without it", file=sys.stderr)
                    break
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
        with store_lock():
            return func(*args, **kwargs)
    return wrapper


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
    """Parse the optional YAML-ish frontmatter into a dict + body."""
    meta: dict[str, Any] = {}
    body = text
    if not text.startswith("---"):
        return meta, body
    parts = text.split("---", 2)
    if len(parts) < 3:
        return meta, body
    header, body = parts[1], parts[2].lstrip("\n")
    for line in header.splitlines():
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
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
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

def local_now_iso() -> str:
    """Local time with offset -- a diary entry belongs to a local day."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def local_date() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def render_entry(title: str, body: str, tags: list[str], updated: str) -> str:
    """Render one entry as a level-2 heading block."""
    lines = [f"## {normalize_title(title)}", ""]
    if tags:
        lines.append(f"<!-- tags: {', '.join(tags)} -->")
    if updated:
        lines.append(f"<!-- updated: {updated} -->")
    lines.append("")
    lines.append((body or "").strip())
    lines.append("")
    # Trailing blank line so the next entry starts after a real gap.
    return "\n".join(lines) + "\n"


def _extract_meta(section: str) -> tuple[list[str], str, str]:
    """Pull tags / updated out of the meta comments, return clean body."""
    tags: list[str] = []
    updated = ""
    for match in META_COMMENT_RE.finditer(section):
        key = match.group(1).lower()
        value = match.group(2).strip()
        if key == "tags":
            tags = [t.strip() for t in value.split(",") if t.strip()]
        elif key == "updated":
            updated = value
    body = META_COMMENT_RE.sub("", section)
    return tags, updated, body.strip()


def _fenced_lines(text: str) -> set[int]:
    """Line numbers (0-based) that sit inside a fenced code block.

    A ``##`` heading inside a code fence is documentation, not an entry.
    """
    inside: set[int] = set()
    fence: str | None = None
    for number, line in enumerate(text.splitlines()):
        stripped = line.strip()
        if fence is None:
            if stripped.startswith("```") or stripped.startswith("~~~"):
                fence = stripped[:3]
                inside.add(number)
            continue
        inside.add(number)
        if stripped.startswith(fence):
            fence = None
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
    """True when a file uses the frontmatter (one memory per file) format."""
    try:
        with path.open("r", encoding="utf-8") as handle:
            return handle.read(4).lstrip().startswith("---")
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
    """Delete a daily log that no longer holds any entry."""
    if path.name == CURATED_FILENAME or not path.exists():
        return
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    if _entry_matches(text):
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

def tokenize(text: str) -> list[str]:
    """Split into comparable units.

    Latin words are lowercased whole words. CJK is split per character AND
    into 2-grams, because CJK has no word boundaries and single characters
    carry too little signal for a relevant hit.
    """
    text = unicodedata.normalize("NFKC", text).lower()
    tokens: list[str] = []
    for match in re.finditer(r"[a-z0-9_]+", text):
        tokens.append(match.group(0))
    cjk = re.findall(r"[\u4e00-\u9fff\u3040-\u30ff]", text)
    tokens.extend(cjk)
    cjk_seq = "".join(cjk)
    tokens.extend(cjk_seq[i:i + 2] for i in range(len(cjk_seq) - 1))
    return tokens


def score(query: str, title: str, body: str, tags: list[str]) -> float:
    q_tokens = set(tokenize(query))
    if not q_tokens:
        return 0.0
    title_tokens = tokenize(title)
    body_tokens = tokenize(body)
    tag_tokens = tokenize(" ".join(tags))

    def hits(tokens: list[str]) -> int:
        if not tokens:
            return 0
        bucket: dict[str, int] = {}
        for tok in tokens:
            bucket[tok] = bucket.get(tok, 0) + 1
        return sum(bucket.get(q, 0) for q in q_tokens)

    total = (
        3.0 * hits(title_tokens)
        + 1.0 * hits(tag_tokens)
        + 0.5 * hits(body_tokens)
    )
    # Normalise by query size so long queries do not automatically win.
    return total / (len(q_tokens) ** 0.5)


def _doc_date(doc: dict[str, Any]) -> str:
    if doc.get("date"):
        return doc["date"]
    updated = doc.get("updated", "")
    return updated[:10] if updated else ""


def search_memories(query: str, limit: int = 5, scope: str = SCOPE_PROJECT,
                    since: str = "", until: str = "") -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for doc in collect_docs(scope):
        date = _doc_date(doc)
        if since and date and date < since:
            continue
        if until and date and date > until:
            continue
        s = score(query, doc.get("title", ""), doc.get("body", ""), doc.get("tags", []))
        if s > 0:
            results.append({
                "name": doc["name"],
                "title": doc.get("title", doc["name"]),
                "tags": doc.get("tags", []),
                "updated": doc.get("updated", ""),
                "date": date,
                "bucket": doc.get("bucket", ""),
                "scope": doc.get("scope", ""),
                "score": round(s, 3),
                "excerpt": excerpt_around(doc.get("body", ""), query),
                "path": doc.get("path", ""),
            })
    results.sort(key=lambda r: r["score"], reverse=True)
    return results[:limit]


def excerpt_around(body: str, query: str, width: int = 220) -> str:
    """Return a window of body text centred on the first query hit."""
    if not body:
        return ""
    q_tokens = tokenize(query)
    lowered = body.lower()
    pos = -1
    for tok in q_tokens:
        if len(tok) < 2 and not re.match(r"[\u4e00-\u9fff\u3040-\u30ff]", tok):
            continue
        pos = lowered.find(tok.lower())
        if pos != -1:
            break
    if pos == -1:
        return body[:width].replace("\n", " ").strip()
    start = max(0, pos - width // 3)
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
    target_slug = slugify(target)
    fallback = None
    for doc in docs_in_directory(directory, label):
        if normalize_title(doc.get("title", "")) == target:
            return doc
        if fallback is None and slugify(doc.get("title", "")) == target_slug:
            fallback = doc
    return fallback


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
    path = directory / f"{slugify(title)}.md"
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


def _locate(name: str) -> dict[str, Any] | None:
    """Find a memory by title. Project scope wins over global scope."""
    target = normalize_title(name)
    target_slug = slugify(target)
    fallback = None
    for doc in collect_docs(SCOPE_PROJECT):
        if normalize_title(doc.get("title", "")) == target:
            return doc
        if fallback is None and slugify(doc.get("title", "")) == target_slug:
            fallback = doc
    return fallback


def tool_memory_read(name: str, scope: str = SCOPE_PROJECT) -> dict[str, Any]:
    target = normalize_title(name)
    if DATE_FILE_RE.match(target):
        docs = [d for d in collect_docs(_normalize_scope(scope)) if d.get("date") == target]
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
    doc = _locate(target)
    if doc is None and _normalize_scope(scope) == SCOPE_ALL:
        for candidate in collect_docs(SCOPE_ALL):
            if normalize_title(candidate.get("title", "")) == target:
                doc = candidate
                break
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
    date = as_date(date)
    bucket = _normalize_bucket(bucket) if str(bucket or "").strip() else ""
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
        delete_entry(path, doc.get("title", ""))
        drop_if_empty(path)
    else:
        try:
            path.unlink()
        except OSError:
            pass
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
    "use bucket 'curated' for long-lived facts."
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
                "since": {"type": "string", "description": "Only memories dated on or after YYYY-MM-DD."},
                "until": {"type": "string", "description": "Only memories dated on or before YYYY-MM-DD."},
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
            "returns every memory recorded that day."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "scope": {
                    "type": "string",
                    "enum": [SCOPE_PROJECT, SCOPE_ALL],
                    "description": "Default 'project'.",
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
