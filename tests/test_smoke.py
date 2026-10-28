#!/usr/bin/env python3
"""Regression tests for the memory kit. Standard library only.

    python3 tests/test_smoke.py
    python3 -m unittest discover -s tests -v

Every test here exists because the corresponding bug shipped once:

* the Windows interpreter lookup iterated a single ``Path`` and crashed;
* ``--uninstall`` deleted the whole memory store;
* a malformed ``mcp.json`` was silently replaced with a one-server object;
* a project-scope write could rewrite a global memory with the same title;
* ``bucket: "curated"`` never promoted an existing entry;
* one bad JSON line took the server process down;
* a string in ``tags`` became one tag per character;
* a newline in a title forged an extra ``##`` entry;
* titles longer than 80 characters collided on one file;
* two processes writing at once could interleave their read-modify-write;
* reinstalling replaced the user's curated global memories with the template;
* lock open/timeout failures allowed a write to proceed without exclusivity;
* different titles with the same slug overwrote one another;
* deleting the last entry also deleted manual content in its daily file;
* failed deletes were reported as successful;
* an unclosed code fence hid every later entry in the day's log;
* ``---`` inside a title truncated the frontmatter parse;
* a ``<!-- tags: -->`` line in the body was eaten and turned into a tag;
* the lock file lived in the temp directory, so ``TMPDIR`` split it in two;
* ``memory_list`` silently ignored an unparseable ``date`` filter;
* ``memory_read`` ignored its ``scope`` argument;
* a ``##`` line in a body split one memory into two;
* file-mode stamps were UTC while dated stamps were local;
* search scored raw keyword counts, so a long note that repeated one word
  outranked the memory that answered the question;
* every CJK character was a search term, so "池化" matched "数据可视化";
* CJK bigrams were built across spaces, inventing "本迁" from "回滚脚本 迁移";
* "score" never matched "scored", "migration" never matched "migrations";
* the same fact recorded on three days filled three result slots;
* a date filter let undated memories through;
* a query made only of stopwords listed the whole store.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

KIT = Path(__file__).resolve().parent.parent
SERVER = KIT / "mcp" / "memory_server.py"
INSTALLER = KIT / "install_user.py"


def run_server(messages: list[dict], env: dict, cwd: Path) -> tuple[subprocess.CompletedProcess, list[dict]]:
    """Send requests to the server on stdin, return parsed responses."""
    payload = "".join(json.dumps(message) + "\n" for message in messages)
    proc = subprocess.run(
        [sys.executable, str(SERVER)],
        input=payload, capture_output=True, text=True, env=env, cwd=str(cwd),
    )
    replies = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
    return proc, replies


def run_server_raw(payload: str, env: dict, cwd: Path) -> tuple[subprocess.CompletedProcess, list[dict]]:
    proc = subprocess.run(
        [sys.executable, str(SERVER)],
        input=payload, capture_output=True, text=True, env=env, cwd=str(cwd),
    )
    replies = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
    return proc, replies


def tool_text(reply: dict) -> str:
    return reply["result"]["content"][0]["text"]


def tool_json(reply: dict) -> dict:
    return json.loads(tool_text(reply))


class ServerTest(unittest.TestCase):
    """Behaviour of mcp/memory_server.py over the real stdio protocol."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="memkit-"))
        self.store = self.tmp / "store"
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith("MEMORY_")}
        self.env.update({
            "MEMORY_ROOT": str(self.store),
            "MEMORY_LAYOUT": "partitioned",
            "MEMORY_ORGANIZE": "dated",
        })

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers ---------------------------------------------------------
    def call(self, tool: str, **arguments) -> dict:
        _proc, replies = run_server(
            [{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": tool, "arguments": arguments}}],
            self.env, self.repo,
        )
        self.assertEqual(len(replies), 1, replies)
        return replies[0]

    def write(self, **arguments) -> dict:
        return tool_json(self.call("memory_write", **arguments))

    def project_files(self) -> list[Path]:
        return sorted((self.store / "projects").glob("*/*.md"))

    # -- tests -----------------------------------------------------------
    def test_write_read_delete_roundtrip(self) -> None:
        created = self.write(title="Migrations need rollback", content="pair them")
        self.assertEqual(created["action"], "created")
        self.assertEqual(created["bucket"], "daily")

        listed = json.loads(tool_text(self.call("memory_list")))
        self.assertEqual(listed["count"], 1)
        self.assertEqual(listed["memories"][0]["title"], "Migrations need rollback")

        read = json.loads(tool_text(self.call("memory_read", name="Migrations need rollback")))
        self.assertIn("pair them", read["body"])

        self.call("memory_delete", name="Migrations need rollback")
        self.assertEqual(json.loads(tool_text(self.call("memory_list")))["count"], 0)
        # The emptied daily log is removed rather than left as noise.
        self.assertEqual(self.project_files(), [])

    def test_delete_last_entry_preserves_manual_file_content(self) -> None:
        created = self.write(title="target", content="body")
        daily = Path(created["path"])
        daily.write_text(
            "# 2026-10-27\n\nmanual preamble that must survive\n\n## target\n\nbody\n",
            encoding="utf-8",
        )

        self.call("memory_delete", name="target")

        self.assertTrue(daily.exists())
        remaining = daily.read_text(encoding="utf-8")
        self.assertIn("manual preamble that must survive", remaining)
        self.assertNotIn("## target", remaining)

    def test_dated_delete_failure_is_reported(self) -> None:
        self.env["MEMORY_LAYOUT"] = "flat"
        self.write(title="target", content="body")
        sys.path.insert(0, str(SERVER.parent))
        try:
            import memory_server
        finally:
            sys.path.pop(0)

        with mock.patch.dict(os.environ, self.env):
            with mock.patch.object(memory_server, "delete_entry", return_value=False):
                reply = memory_server.tool_memory_delete("target")

        self.assertTrue(reply["isError"], reply)
        self.assertIn("could not delete", reply["content"][0]["text"].lower())

    def test_file_delete_failure_is_reported(self) -> None:
        self.env["MEMORY_LAYOUT"] = "flat"
        self.env["MEMORY_ORGANIZE"] = "file"
        self.store = self.tmp / "store-file-delete-error"
        self.env["MEMORY_ROOT"] = str(self.store)
        self.write(title="target", content="body")
        sys.path.insert(0, str(SERVER.parent))
        try:
            import memory_server
        finally:
            sys.path.pop(0)

        with mock.patch.dict(os.environ, self.env):
            with mock.patch.object(Path, "unlink", side_effect=OSError("read-only filesystem")):
                reply = memory_server.tool_memory_delete("target")

        self.assertTrue(reply["isError"], reply)
        self.assertIn("read-only filesystem", reply["content"][0]["text"])
        self.assertEqual(json.loads(tool_text(self.call("memory_list")))["count"], 1)

    def test_search_matches_chinese(self) -> None:
        self.write(title="数据库迁移",
                   content="迁移脚本要配套回滚脚本，否则线上出问题无法快速恢复。")
        hits = json.loads(tool_text(self.call("memory_search", query="回滚 迁移")))
        self.assertEqual(len(hits), 1)
        self.assertIn("回滚", hits[0]["excerpt"])

    def test_project_write_cannot_rewrite_global_memory(self) -> None:
        self.write(title="Package manager", content="always use pnpm", scope="global")
        self.write(title="Package manager", content="this repo uses npm", scope="project")

        global_text = "".join(path.read_text(encoding="utf-8")
                              for path in (self.store / "global").glob("*.md"))
        self.assertIn("always use pnpm", global_text)
        self.assertNotIn("this repo uses npm", global_text)

        project_text = "".join(path.read_text(encoding="utf-8") for path in self.project_files())
        self.assertIn("this repo uses npm", project_text)

        global_hits = json.loads(tool_text(self.call("memory_list", scope="global")))
        self.assertEqual(global_hits["count"], 1)

    def test_curated_promotes_existing_entry(self) -> None:
        self.write(title="Commits are English", content="Conventional Commits.")
        promoted = self.write(title="Commits are English",
                              content="Conventional Commits, English only.",
                              bucket="curated")
        self.assertEqual(promoted["bucket"], "curated")

        curated = (Path(promoted["path"])).read_text(encoding="utf-8")
        self.assertIn("English only", curated)
        # The daily copy is gone; the fact lives in exactly one place.
        daily = [path for path in self.project_files() if path.name != "MEMORY.md"]
        for path in daily:
            self.assertNotIn("Commits are English", path.read_text(encoding="utf-8"))

        listed = json.loads(tool_text(self.call("memory_list", bucket="curated")))
        self.assertEqual(listed["count"], 1)

        # Promoting the same title again updates the curated copy in place.
        again = self.write(title="Commits are English", content="English only, always.",
                           bucket="curated")
        curated = Path(again["path"]).read_text(encoding="utf-8")
        self.assertEqual(curated.count("## Commits are English"), 1)
        self.assertIn("English only, always.", curated)

    def test_string_tags_are_split_not_exploded(self) -> None:
        created = self.write(title="tagged", content="x", tags="db, migration; sql")
        self.assertEqual(created["tags"], ["db", "migration", "sql"])

    def test_newline_in_title_cannot_forge_an_entry(self) -> None:
        created = self.write(title="bad\ntitle ## forged", content="body")
        self.assertEqual(created["title"], "bad title ## forged")

        listed = json.loads(tool_text(self.call("memory_list")))
        self.assertEqual(listed["count"], 1)

    def test_code_fence_headings_are_not_entries(self) -> None:
        self.write(title="showdown", content="example:\n\n```\n## not an entry\n```\n\nend")
        listed = json.loads(tool_text(self.call("memory_list")))
        self.assertEqual(listed["count"], 1)

    def test_bad_input_does_not_kill_the_server(self) -> None:
        payload = "".join([
            "[1, 2, 3]\n",
            "{not json at all\n",
            json.dumps({"jsonrpc": "2.0", "id": 7, "method": "ping"}) + "\n",
        ])
        proc, replies = run_server_raw(payload, self.env, self.repo)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        codes = sorted(reply["error"]["code"] for reply in replies if "error" in reply)
        self.assertEqual(codes, [-32700, -32600])
        # The ping after the garbage still gets an answer.
        self.assertTrue(any(reply.get("id") == 7 for reply in replies))

    def test_initialize_advertises_the_protocol(self) -> None:
        _proc, replies = run_server(
            [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}],
            self.env, self.repo,
        )
        result = replies[0]["result"]
        self.assertIn("protocolVersion", result)
        self.assertIn("memory_search", result["instructions"])

    def test_invalid_date_filter_is_reported(self) -> None:
        reply = self.call("memory_search", query="x", since="2026/01/01")
        self.assertTrue(reply["result"]["isError"])
        self.assertIn("YYYY-MM-DD", tool_text(reply))

    def test_project_meta_records_the_origin(self) -> None:
        self.write(title="anything", content="x")
        metas = list((self.store / "projects").glob("*/project.json"))
        self.assertEqual(len(metas), 1)
        meta = json.loads(metas[0].read_text(encoding="utf-8"))
        self.assertEqual(meta["name"], "repo")

    def test_long_titles_do_not_collide(self) -> None:
        prefix = "a very long memory title that keeps going and going " * 2
        first = self.write(title=prefix + "alpha", content="first body")
        second = self.write(title=prefix + "omega", content="second body")
        # Truncating at 80 characters gave both titles the same slug.
        self.assertNotEqual(first["name"], second["name"])
        self.assertEqual(json.loads(tool_text(self.call("memory_list")))["count"], 2)

        # In file mode the slug *is* the path, which is where the collision
        # used to destroy the earlier memory.
        self.env["MEMORY_ORGANIZE"] = "file"
        self.store = self.tmp / "store-file"
        self.env["MEMORY_ROOT"] = str(self.store)
        first = self.write(title=prefix + "alpha", content="first body")
        second = self.write(title=prefix + "omega", content="second body")
        self.assertNotEqual(first["path"], second["path"])
        for title, body in ((prefix + "alpha", "first body"),
                            (prefix + "omega", "second body")):
            stored = json.loads(tool_text(self.call("memory_read", name=title)))
            self.assertEqual(stored["body"], body)

    def test_file_mode_slug_collisions_preserve_distinct_titles(self) -> None:
        self.env["MEMORY_ORGANIZE"] = "file"
        self.store = self.tmp / "store-file-collisions"
        self.env["MEMORY_ROOT"] = str(self.store)

        first = self.write(title="a/b", content="first body")
        second = self.write(title="ab", content="second body")

        self.assertNotEqual(first["path"], second["path"])
        self.assertEqual(json.loads(tool_text(self.call("memory_list")))["count"], 2)
        for title, body in (("a/b", "first body"), ("ab", "second body")):
            stored = json.loads(tool_text(self.call("memory_read", name=title)))
            self.assertEqual(stored["title"], title)
            self.assertEqual(stored["body"], body)

    def test_file_mode_disambiguated_path_collision_preserves_all_titles(self) -> None:
        self.env["MEMORY_ORGANIZE"] = "file"
        self.store = self.tmp / "store-file-secondary-collision"
        self.env["MEMORY_ROOT"] = str(self.store)
        digest = hashlib.sha1(b"a/b").hexdigest()[:8]
        natural_title = f"ab-{digest}"

        self.write(title="ab", content="base body")
        self.write(title=natural_title, content="natural body")
        self.write(title="a/b", content="slash body")

        self.assertEqual(json.loads(tool_text(self.call("memory_list")))["count"], 3)
        for title, body in (("ab", "base body"), (natural_title, "natural body"),
                            ("a/b", "slash body")):
            stored = json.loads(tool_text(self.call("memory_read", name=title)))
            self.assertEqual(stored["title"], title)
            self.assertEqual(stored["body"], body)

    def test_dated_mode_slug_collisions_preserve_distinct_titles(self) -> None:
        self.write(title="a/b", content="first body")
        self.write(title="ab", content="second body")

        self.assertEqual(json.loads(tool_text(self.call("memory_list")))["count"], 2)
        for title, body in (("a/b", "first body"), ("ab", "second body")):
            stored = json.loads(tool_text(self.call("memory_read", name=title)))
            self.assertEqual(stored["title"], title)
            self.assertEqual(stored["body"], body)

    # -- P0: an entry must never corrupt the file it lands in --------------
    def test_unclosed_fence_does_not_swallow_later_entries(self) -> None:
        self.write(title="entry with unclosed fence",
                   content="see:\n\n```bash\necho hi")
        self.write(title="second entry", content="must stay visible")

        # The fence is closed on the way in, so the next entry is not code.
        stored = json.loads(tool_text(self.call("memory_read", name="second entry")))
        self.assertEqual(stored["body"], "must stay visible")
        self.assertIn("```", Path(stored["path"]).read_text(encoding="utf-8"))
        self.assertEqual(json.loads(tool_text(self.call("memory_list")))["count"], 2)

        # Re-writing the title updates in place instead of appending a copy.
        self.write(title="second entry", content="must stay visible")
        self.assertEqual(json.loads(tool_text(self.call("memory_list")))["count"], 2)

    def test_unclosed_fence_in_hand_edited_file_keeps_later_entries(self) -> None:
        created = self.write(title="first", content="body")
        daily = Path(created["path"])
        daily.write_text(
            "# 2026-10-28\n\n## first\n\n```\nno closing fence\n\n"
            "## second\n\nmust still be an entry\n",
            encoding="utf-8",
        )

        listed = json.loads(tool_text(self.call("memory_list")))
        self.assertEqual([m["title"] for m in listed["memories"]], ["first", "second"])

    def test_dashes_in_title_survive_frontmatter(self) -> None:
        self.env["MEMORY_ORGANIZE"] = "file"
        self.store = self.tmp / "store-file-dashes"
        self.env["MEMORY_ROOT"] = str(self.store)
        title = "A---B dash title"

        self.write(title=title, content="body one")

        stored = json.loads(tool_text(self.call("memory_read", name=title)))
        self.assertEqual(stored["title"], title)
        self.assertEqual(stored["body"], "body one")

    def test_meta_comment_in_body_is_not_metadata(self) -> None:
        self.write(title="body keeps comments",
                   content="before\n\n<!-- tags: demo -->\n\nafter")

        stored = json.loads(tool_text(self.call("memory_read", name="body keeps comments")))
        self.assertIn("<!-- tags: demo -->", stored["body"])
        self.assertEqual(stored["tags"], [])

        # The real header block still parses.
        self.write(title="tagged properly", content="x", tags=["db", "sql"])
        proper = json.loads(tool_text(self.call("memory_read", name="tagged properly")))
        self.assertEqual(proper["tags"], ["db", "sql"])
        self.assertEqual(proper["body"], "x")

    def test_body_heading_is_demoted_so_one_write_is_one_memory(self) -> None:
        self.write(title="has subheading",
                   content="intro\n\n## Sub heading\n\ntail")

        self.assertEqual(json.loads(tool_text(self.call("memory_list")))["count"], 1)
        stored = json.loads(tool_text(self.call("memory_read", name="has subheading")))
        self.assertIn("### Sub heading", stored["body"])
        self.assertIn("tail", stored["body"])

    # -- P1: lock consistency, argument validity, scope, timestamps --------
    def test_lock_path_does_not_depend_on_tmpdir(self) -> None:
        sys.path.insert(0, str(SERVER.parent))
        try:
            import memory_server
        finally:
            sys.path.pop(0)

        # Two processes with different TMPDIR values must still meet on one
        # lock file, otherwise both hold the "lock" and interleave their
        # read-modify-write cycles.
        with mock.patch.dict(os.environ, self.env):
            with mock.patch.object(tempfile, "tempdir", str(self.tmp / "tmp-a")):
                first = memory_server.lock_path()
            with mock.patch.object(tempfile, "tempdir", str(self.tmp / "tmp-b")):
                second = memory_server.lock_path()

        self.assertEqual(first, second)
        self.assertNotIn(str(self.tmp / "tmp-a"), str(first))
        # Beside the store, so the store <-> lock mapping cannot drift.
        self.assertEqual(first.parent.parent, self.store.parent)

    def test_list_rejects_bad_date_and_bucket(self) -> None:
        wrong_date = self.call("memory_list", date="2026/01/01")
        self.assertTrue(wrong_date["result"]["isError"], wrong_date)
        self.assertIn("YYYY-MM-DD", tool_text(wrong_date))

        wrong_bucket = self.call("memory_list", bucket="nonsense")
        self.assertTrue(wrong_bucket["result"]["isError"], wrong_bucket)
        self.assertIn("bucket", tool_text(wrong_bucket))

    def test_read_honours_scope(self) -> None:
        self.write(title="Package manager", content="global body", scope="global")
        self.write(title="Package manager", content="project body", scope="project")

        project = json.loads(tool_text(self.call("memory_read", name="Package manager")))
        self.assertEqual(project["body"], "project body")

        shared = json.loads(tool_text(
            self.call("memory_read", name="Package manager", scope="global")))
        self.assertEqual(shared["body"], "global body")
        self.assertEqual(shared["scope"], "global")

    def test_file_mode_timestamp_is_local(self) -> None:
        self.env["MEMORY_ORGANIZE"] = "file"
        self.store = self.tmp / "store-file-stamp"
        self.env["MEMORY_ROOT"] = str(self.store)
        self.write(title="stamped", content="x")

        stored = json.loads(tool_text(self.call("memory_read", name="stamped")))
        # A UTC stamp filed a memory written at 01:00 +08:00 under the
        # previous day, where the since/until filters never saw it.
        self.assertFalse(stored["updated"].endswith("Z"), stored["updated"])
        self.assertEqual(stored["updated"][:10], datetime.now().strftime("%Y-%m-%d"))

    def test_lock_is_exclusive_across_processes(self) -> None:
        # A second VS Code window is a second process. The lock is what keeps
        # their read-modify-write cycles from interleaving.
        holder_script = (
            "import sys, time\n"
            f"sys.path.insert(0, {str(SERVER.parent)!r})\n"
            "import memory_server\n"
            "with memory_server.store_lock():\n"
            "    print('held', flush=True)\n"
            "    time.sleep(0.8)\n"
        )
        holder = subprocess.Popen([sys.executable, "-c", holder_script],
                                  stdout=subprocess.PIPE, text=True,
                                  env=self.env, cwd=str(self.repo))
        self.assertEqual(holder.stdout.readline().strip(), "held")

        probe_script = (
            "import sys\n"
            f"sys.path.insert(0, {str(SERVER.parent)!r})\n"
            "import memory_server\n"
            "with memory_server.store_lock():\n"
            "    print('got', flush=True)\n"
        )
        started = time.monotonic()
        probe = subprocess.run([sys.executable, "-c", probe_script],
                               capture_output=True, text=True,
                               env=self.env, cwd=str(self.repo), timeout=60)
        waited = time.monotonic() - started
        holder.communicate(timeout=30)
        self.assertEqual(probe.stdout.strip(), "got", probe.stderr)
        self.assertGreater(waited, 0.5, "store_lock did not block the second process")

    def test_lock_timeout_rejects_write_instead_of_writing_unlocked(self) -> None:
        holder_script = (
            "import sys\n"
            f"sys.path.insert(0, {str(SERVER.parent)!r})\n"
            "import memory_server\n"
            "with memory_server.store_lock():\n"
            "    print('held', flush=True)\n"
            "    sys.stdin.readline()\n"
        )
        with subprocess.Popen(
            [sys.executable, "-c", holder_script],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
            env=self.env, cwd=str(self.repo),
        ) as holder:
            try:
                self.assertEqual(holder.stdout.readline().strip(), "held")
                reply = self.call("memory_write", title="blocked", content="must not be written")
                self.assertTrue(reply["result"]["isError"], reply)
                self.assertIn("lock", tool_text(reply).lower())
            finally:
                holder.stdin.write("\n")
                holder.stdin.flush()
                holder.wait(timeout=30)

        listed = json.loads(tool_text(self.call("memory_list", scope="project")))
        self.assertEqual(listed["count"], 0)

    def test_lock_open_failure_rejects_write(self) -> None:
        sys.path.insert(0, str(SERVER.parent))
        try:
            import memory_server
        finally:
            sys.path.pop(0)
        with mock.patch.dict(os.environ, self.env):
            with mock.patch.object(memory_server, "lock_path", side_effect=OSError("unavailable")):
                reply = memory_server.tool_memory_write("blocked", "must not be written")

        self.assertTrue(reply["isError"], reply)
        self.assertIn("unavailable", reply["content"][0]["text"])
        self.assertFalse(any(self.store.rglob("*.md")))

    def test_concurrent_writers_do_not_lose_entries(self) -> None:
        # Each writer is a separate process. Every write rewrites the whole
        # daily file, so this also catches a lock that deadlocks or errors.
        workers, per_worker = 6, 5
        pending = []
        for worker in range(workers):
            messages = [
                {"jsonrpc": "2.0", "id": index, "method": "tools/call",
                 "params": {"name": "memory_write",
                            "arguments": {"title": f"worker {worker} item {index}",
                                          "content": "body padding " * 4}}}
                for index in range(per_worker)
            ]
            payload = "".join(json.dumps(message) + "\n" for message in messages)
            proc = subprocess.Popen(
                [sys.executable, str(SERVER)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, env=self.env, cwd=str(self.repo),
            )
            pending.append((proc, payload))

        for proc, payload in pending:
            _out, err = proc.communicate(payload, timeout=120)
            self.assertEqual(proc.returncode, 0, err)

        listed = json.loads(tool_text(self.call("memory_list", scope="project")))
        self.assertEqual(listed["count"], workers * per_worker)


class InstallerTest(unittest.TestCase):
    """Behaviour of install_user.py, exercised through its CLI."""

    def setUp(self) -> None:
        self.home = Path(tempfile.mkdtemp(prefix="memkit-home-"))
        self.mcp = self.home / "Library" / "Application Support" / "Code" / "User" / "mcp.json"
        self.mcp.parent.mkdir(parents=True)
        self.env = dict(os.environ)
        self.env["HOME"] = str(self.home)
        self.env["USERPROFILE"] = str(self.home)
        self.env.pop("COPILOT_HOME", None)

    def tearDown(self) -> None:
        shutil.rmtree(self.home, ignore_errors=True)

    def install(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(INSTALLER), *args],
                              capture_output=True, text=True, env=self.env)

    def test_windows_python_lookup_returns_one_directory(self) -> None:
        sys.path.insert(0, str(KIT))
        try:
            import install_user
        finally:
            sys.path.pop(0)
        # A single Path, not an iterable of characters: the old code wrote
        # ``for base in (Path(...) / "Programs" / "Python")``, which raised
        # TypeError on Windows before anything was installed.
        dirs = install_user.windows_python_dirs()
        self.assertEqual(len(dirs), 1)
        self.assertTrue(str(dirs[0]).endswith("Python"))

    def test_broken_config_aborts_without_writing(self) -> None:
        self.mcp.write_text('{ "servers": { "other": { "command": "x" }\n', encoding="utf-8")
        before = self.mcp.read_bytes()
        result = self.install()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("Nothing was written", result.stderr)
        self.assertEqual(self.mcp.read_bytes(), before)

    def test_existing_servers_survive_install(self) -> None:
        self.mcp.write_text(json.dumps({"servers": {"other": {"command": "x"}}}), encoding="utf-8")
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(self.mcp.read_text(encoding="utf-8"))
        self.assertIn("other", data["servers"])
        self.assertIn("memory", data["servers"])

    def test_reinstall_preserves_global_curated_memories(self) -> None:
        self.assertEqual(self.install().returncode, 0)
        curated = self.home / ".copilot" / "memory" / "global" / "MEMORY.md"
        original = "# Global memory\n\n## User fact\n\nmust survive reinstall\n"
        curated.write_text(original, encoding="utf-8")

        result = self.install()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(curated.read_text(encoding="utf-8"), original)

    def test_uninstall_keeps_memories_and_purge_removes_them(self) -> None:
        self.assertEqual(self.install().returncode, 0)
        store = self.home / ".copilot" / "memory"
        marker = store / "projects" / "demo" / "2026-01-01.md"
        marker.parent.mkdir(parents=True)
        marker.write_text("# 2026-01-01\n\n## keep me\n\nbody\n", encoding="utf-8")

        result = self.install("--uninstall")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(marker.exists(), "uninstall must not delete the user's memories")
        self.assertIn("memories kept", result.stdout)
        self.assertFalse((self.home / ".copilot" / "memory-kit").exists())

        self.assertEqual(self.install().returncode, 0)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("# 2026-01-01\n\n## keep me\n\nbody\n", encoding="utf-8")
        result = self.install("--uninstall", "--purge")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(store.exists())

    def test_purge_requires_uninstall(self) -> None:
        result = self.install("--purge")
        self.assertEqual(result.returncode, 2)


class SearchRankingTest(unittest.TestCase):
    """How memory_search ranks.

    Called in-process rather than over stdio: a ranking case needs a handful
    of memories and several queries, and a subprocess per call made the
    suite slow enough that ranking cases would not get written.

    Every case here is a query the counter-based scorer got wrong -- it
    matched raw keyword occurrences with no term weighting, no document
    length normalisation and every CJK character as a term.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="memkit-rank-"))
        self.store = self.tmp / "store"
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith("MEMORY_")}
        self.env.update({
            "MEMORY_ROOT": str(self.store),
            "MEMORY_LAYOUT": "flat",
            "MEMORY_ORGANIZE": "dated",
        })
        sys.path.insert(0, str(SERVER.parent))
        try:
            import memory_server
        finally:
            sys.path.pop(0)
        self.ms = memory_server

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers ---------------------------------------------------------
    def remember(self, title: str, body: str, tags: list[str] | None = None,
                 date: str = "2026-10-20") -> None:
        path = self.store / f"{date}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        with mock.patch.dict(os.environ, self.env):
            self.ms.append_entry(path, title, body, tags or [],
                                 f"{date}T10:00:00+08:00", header=f"# {date}\n")

    def search(self, query: str, **arguments) -> list[dict]:
        with mock.patch.dict(os.environ, self.env):
            reply = self.ms.tool_memory_search(query, **arguments)
        self.assertFalse(reply["isError"], reply)
        return json.loads(reply["content"][0]["text"])

    def titles(self, query: str, **arguments) -> list[str]:
        return [hit["title"] for hit in self.search(query, **arguments)]

    # -- tests -----------------------------------------------------------
    def test_search_folds_english_inflections(self) -> None:
        # "score" returned nothing while the store said "scored"; "write"
        # returned nothing while the store said "Writers".
        self.remember("Scoring rules", "Documents are scored, then ranked.")
        self.remember("Writers must lock", "The writer takes the lock.")

        for query in ("score", "scored", "scoring"):
            self.assertEqual(self.titles(query), ["Scoring rules"], query)
        for query in ("write", "writers"):
            self.assertEqual(self.titles(query), ["Writers must lock"], query)

    def test_search_bigram_does_not_cross_a_space(self) -> None:
        # Gluing every CJK character in the text together invented "本迁" out
        # of "回滚脚本 迁移", which then matched "版本迁移".
        self.remember("版本迁移记录", "这次改动涉及版本迁移。")
        self.remember("脚本位置", "迁移脚本放在 tools 下。")

        self.assertEqual(self.titles("本迁"), ["版本迁移记录"])
        self.assertEqual(self.titles("滚脚"), [])

    def test_query_characters_are_not_terms_on_their_own(self) -> None:
        # Querying "池化" matched "数据可视化" through the shared character 化,
        # and "数据库" could not be told apart from "数据可视化".
        self.remember("数据库迁移", "迁移脚本要配套回滚。")
        self.remember("数据可视化主题", "图表配色要对比度足够。")
        self.remember("池化配置", "连接池默认 10。")

        self.assertEqual(self.titles("池化"), ["池化配置"])
        self.assertEqual(self.titles("数据库"), ["数据库迁移"])

    def test_single_character_query_still_matches(self) -> None:
        # Dropping unigrams outright would have made a one-character query
        # useless; the index keeps them at a reduced weight.
        self.remember("锁文件路径", "TMPDIR 不同，锁要放在库旁边。")
        self.remember("UI 主题", "浅色主题下不要用深色面板。")

        self.assertEqual(self.titles("锁"), ["锁文件路径"])

    def test_phrase_beats_repetition_and_length(self) -> None:
        # A long note that repeats "database" twelve times outranked the note
        # that actually said "database migration".
        self.remember("Long ops notes", "database " * 12)
        self.remember("数据库迁移流程", "database migration steps: dump, verify.")

        for query in ("database migration", "migration database"):
            self.assertEqual(self.titles(query)[0], "数据库迁移流程", query)

    def test_duplicate_facts_are_collapsed(self) -> None:
        # The same rule recorded on three days occupied the top of every
        # result list three times over.
        body = "Commit messages are written in English."
        for day in ("2026-10-18", "2026-10-19", "2026-10-20"):
            self.remember(f"提交信息用英文 {day}", body, date=day)

        hits = self.search("commit messages english")
        self.assertEqual(len(hits), 1, hits)
        self.assertEqual(len(hits[0]["also"]), 2, hits[0])

    def test_date_filter_excludes_undated_memories(self) -> None:
        # The filter skipped its own comparison when a memory had no date, so
        # an undated note came back from a range that excluded it.
        self.remember("dated note", "a dated body", date="2026-10-20")
        legacy = self.store / "old-note.md"
        legacy.write_text("---\ntitle: legacy note\n---\n\nno updated field\n",
                          encoding="utf-8")

        self.assertIn("legacy note", self.titles("note"))
        self.assertEqual(self.titles("note", since="2026-10-19"), ["dated note"])

    def test_stopword_query_returns_nothing(self) -> None:
        # "the" was a term, so a query made of stopwords listed the store.
        self.remember("Some rule", "the body has the words in it")

        self.assertEqual(self.titles("the"), [])
        self.assertEqual(self.titles("how do i"), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
