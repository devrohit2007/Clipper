"""SQLite persistence for the web app: jobs, task queue, logs, presets, accounts, key/value."""
from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional

from .utilities import JobState

SCHEMA_VERSION = 1
SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT NOT NULL, title TEXT DEFAULT '', video_id TEXT DEFAULT '',
  status TEXT NOT NULL DEFAULT 'queued', stage TEXT DEFAULT '', progress REAL DEFAULT 0, message TEXT DEFAULT '',
  error TEXT DEFAULT '', options TEXT NOT NULL DEFAULT '{}', data TEXT NOT NULL DEFAULT '{"clips": {}}',
  created REAL NOT NULL, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, job_id INTEGER NOT NULL, clip_id TEXT DEFAULT '',
  status TEXT NOT NULL DEFAULT 'queued', error TEXT DEFAULT '', created REAL NOT NULL, started REAL, finished REAL);
CREATE TABLE IF NOT EXISTS logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER, ts REAL NOT NULL, level TEXT, msg TEXT);
CREATE INDEX IF NOT EXISTS logs_job ON logs(job_id, id);
CREATE TABLE IF NOT EXISTS presets (name TEXT PRIMARY KEY, settings TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS accounts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, token_file TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS scheduled (
  id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER, publish_at TEXT NOT NULL, job_id INTEGER,
  clip_id TEXT, youtube_id TEXT);
"""
MAX_LOG_ROWS = 20000


class Store:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.conn() as c:
            c.executescript(SCHEMA)
            c.execute("INSERT OR IGNORE INTO kv VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))

    @contextmanager
    def conn(self):
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()

    # ---- kv / settings
    def kv_get(self, key: str, default: Any = None) -> Any:
        with self.conn() as c:
            r = c.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(r["value"]) if r else default

    def kv_set(self, key: str, value: Any) -> None:
        with self.conn() as c:
            c.execute("INSERT INTO kv VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (key, json.dumps(value)))

    def kv_del(self, key: str) -> None:
        with self.conn() as c:
            c.execute("DELETE FROM kv WHERE key=?", (key,))

    def get_overrides(self) -> dict:
        return self.kv_get("settings", {})

    def set_overrides(self, d: dict) -> None:
        self.kv_set("settings", d)

    # ---- presets
    def presets(self) -> dict:
        with self.conn() as c:
            return {r["name"]: json.loads(r["settings"]) for r in c.execute("SELECT * FROM presets ORDER BY name")}

    def save_preset(self, name: str, settings: dict) -> None:
        with self.conn() as c:
            c.execute("INSERT INTO presets VALUES (?,?) ON CONFLICT(name) DO UPDATE SET settings=excluded.settings",
                      (name, json.dumps(settings)))

    def delete_preset(self, name: str) -> bool:
        with self.conn() as c:
            return c.execute("DELETE FROM presets WHERE name=?", (name,)).rowcount > 0

    # ---- accounts
    def accounts(self) -> list[dict]:
        with self.conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM accounts ORDER BY id")]

    def account(self, aid: int) -> Optional[dict]:
        with self.conn() as c:
            r = c.execute("SELECT * FROM accounts WHERE id=?", (aid,)).fetchone()
        return dict(r) if r else None

    def add_account(self, name: str, token_file: str) -> int:
        with self.conn() as c:
            return c.execute("INSERT INTO accounts (name, token_file, created) VALUES (?,?,?)",
                             (name, token_file, time.time())).lastrowid

    def rename_account(self, aid: int, name: str) -> None:
        with self.conn() as c:
            c.execute("UPDATE accounts SET name=? WHERE id=?", (name, aid))

    def delete_account(self, aid: int) -> None:
        with self.conn() as c:
            c.execute("DELETE FROM accounts WHERE id=?", (aid,))

    # ---- jobs
    def create_job(self, url: str, options: dict) -> int:
        now = time.time()
        with self.conn() as c:
            return c.execute("INSERT INTO jobs (url, options, created, updated) VALUES (?,?,?,?)",
                             (url, json.dumps(options), now, now)).lastrowid

    def job(self, jid: int) -> Optional[dict]:
        with self.conn() as c:
            r = c.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["options"] = json.loads(d["options"])
        d["data"] = json.loads(d["data"])
        return d

    def jobs(self, limit: int = 100) -> list[dict]:
        with self.conn() as c:
            rows = c.execute("SELECT id,url,title,video_id,status,stage,progress,message,error,created,updated,data "
                             "FROM jobs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            data = json.loads(d.pop("data"))
            clips = data.get("clips", {})
            d["clip_count"] = len(data.get("plan", []))
            d["uploaded"] = sum(1 for c in clips.values() if c.get("status") == "uploaded")
            d["rendered"] = sum(1 for c in clips.values() if c.get("status") in ("rendered", "uploaded", "upload_failed"))
            out.append(d)
        return out

    def update_job(self, jid: int, **cols: Any) -> None:
        allowed = {"title", "video_id", "status", "stage", "progress", "message", "error", "options"}
        cols = {k: (json.dumps(v) if k == "options" else v) for k, v in cols.items() if k in allowed}
        if not cols:
            return
        sets = ", ".join(f"{k}=?" for k in cols) + ", updated=?"
        with self.conn() as c:
            c.execute(f"UPDATE jobs SET {sets} WHERE id=?", (*cols.values(), time.time(), jid))

    def save_job_data(self, jid: int, data: dict) -> None:
        with self.conn() as c:
            c.execute("UPDATE jobs SET data=?, updated=? WHERE id=?",
                      (json.dumps(data, ensure_ascii=False), time.time(), jid))

    def delete_job(self, jid: int) -> None:
        with self.conn() as c:
            c.execute("DELETE FROM jobs WHERE id=?", (jid,))
            c.execute("DELETE FROM tasks WHERE job_id=?", (jid,))
            c.execute("DELETE FROM logs WHERE job_id=?", (jid,))

    # ---- tasks (persistent queue)
    def add_task(self, kind: str, job_id: int, clip_id: str = "") -> int:
        with self.conn() as c:
            return c.execute("INSERT INTO tasks (kind, job_id, clip_id, created) VALUES (?,?,?,?)",
                             (kind, job_id, clip_id, time.time())).lastrowid

    def claim_task(self) -> Optional[dict]:
        with self.conn() as c:
            r = c.execute("SELECT * FROM tasks WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
            if not r:
                return None
            c.execute("UPDATE tasks SET status='running', started=? WHERE id=?", (time.time(), r["id"]))
            return dict(r)

    def finish_task(self, tid: int, status: str, error: str = "") -> None:
        with self.conn() as c:
            c.execute("UPDATE tasks SET status=?, error=?, finished=? WHERE id=?", (status, error, time.time(), tid))

    def recover_tasks(self) -> int:
        """After a crash/kill: tasks that were running go back to the queue (pipeline steps are resumable)."""
        with self.conn() as c:
            return c.execute("UPDATE tasks SET status='queued' WHERE status='running'").rowcount

    def pending_tasks(self, job_id: Optional[int] = None) -> list[dict]:
        q, args = "SELECT * FROM tasks WHERE status IN ('queued','running')", ()
        if job_id is not None:
            q, args = q + " AND job_id=?", (job_id,)
        with self.conn() as c:
            return [dict(r) for r in c.execute(q + " ORDER BY id", args)]

    def cancel_queued(self, job_id: int) -> None:
        with self.conn() as c:
            c.execute("UPDATE tasks SET status='cancelled', finished=? WHERE job_id=? AND status='queued'",
                      (time.time(), job_id))

    # ---- logs
    def add_log(self, job_id: Optional[int], level: str, msg: str) -> None:
        with self.conn() as c:
            c.execute("INSERT INTO logs (job_id, ts, level, msg) VALUES (?,?,?,?)", (job_id, time.time(), level, msg[:2000]))
            c.execute("DELETE FROM logs WHERE id < (SELECT MAX(id) FROM logs) - ?", (MAX_LOG_ROWS,))

    def logs(self, job_id: int, since: int = 0, limit: int = 500) -> list[dict]:
        with self.conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT id, ts, level, msg FROM logs WHERE job_id=? AND id>? ORDER BY id LIMIT ?",
                (job_id, since, limit))]

    # ---- scheduled publish times (so several jobs never collide on one account)
    def add_scheduled(self, account_id: Optional[int], publish_at: str, job_id: int, clip_id: str, yt_id: str) -> None:
        with self.conn() as c:
            c.execute("INSERT INTO scheduled (account_id, publish_at, job_id, clip_id, youtube_id) VALUES (?,?,?,?,?)",
                      (account_id, publish_at, job_id, clip_id, yt_id))

    def last_scheduled(self, account_id: Optional[int], after_iso: str) -> Optional[str]:
        with self.conn() as c:
            r = c.execute("SELECT MAX(publish_at) AS m FROM scheduled WHERE publish_at > ? AND "
                          "COALESCE(account_id,0)=COALESCE(?,0)", (after_iso, account_id)).fetchone()
        return r["m"] if r and r["m"] else None


class DBJobState(JobState):
    """Same interface as the JSON-file JobState the CLI uses, stored in SQLite."""

    def __init__(self, store: Store, job_id: int):
        super().__init__(None)
        self.store, self.job_id = store, job_id
        j = store.job(job_id)
        self.data = j["data"] if j else {"clips": {}}
        self.data.setdefault("clips", {})

    def save(self) -> None:
        self.store.save_job_data(self.job_id, self.data)
