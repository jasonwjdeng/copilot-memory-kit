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
* two processes writing at once could interleave their read-modify-write.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
