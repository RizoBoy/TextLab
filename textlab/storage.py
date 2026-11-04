"""SQLite event journal. Writes happen on one dedicated worker thread."""
from __future__ import annotations

import json
import queue
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, label TEXT NOT NULL UNIQUE,
                    started_at TEXT NOT NULL, status TEXT NOT NULL,
                    snapshot TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    session_id TEXT NOT NULL REFERENCES sessions(id),
                    seq INTEGER NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(session_id, seq)
                );
                CREATE TABLE IF NOT EXISTS protocols (
                    name TEXT PRIMARY KEY, payload TEXT NOT NULL
                );
            """)
            # An interrupted process retains its last consistent snapshot.
            for row in db.execute("SELECT id, snapshot FROM sessions WHERE status IN ('running','paused','ready')").fetchall():
                snapshot = json.loads(row[1])
                snapshot["status"] = "interrupted"
                snapshot["finish_reason"] = "application_interrupted"
                db.execute("UPDATE sessions SET status='interrupted', snapshot=? WHERE id=?",
                           (json.dumps(snapshot, ensure_ascii=False), row[0]))
        self.jobs = queue.Queue()
        self.errors = queue.Queue()
        self.thread = threading.Thread(target=self._worker, name="textlab-storage", daemon=True)
        self.thread.start()

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @contextmanager
    def connect(self):
        db = self._connect()
        try:
            with db:
                yield db
        finally:
            db.close()

    def has_label(self, label: str) -> bool:
        with self.connect() as db:
            return db.execute("SELECT 1 FROM sessions WHERE label=?", (label,)).fetchone() is not None

    def enqueue(self, snapshot: dict, events: list[dict]) -> None:
        self.jobs.put((snapshot, events))

    def _worker(self):
        db = self._connect()
        try:
            while True:
                job = self.jobs.get()
                try:
                    if job is None:
                        return
                    snapshot, events = job
                    with db:
                        db.execute("""INSERT INTO sessions(id,label,started_at,status,snapshot)
                                      VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                                      status=excluded.status,snapshot=excluded.snapshot""",
                                   (snapshot["session_id"], snapshot["profile"]["session_label"],
                                    snapshot["started_at"], snapshot["status"],
                                    json.dumps(snapshot, ensure_ascii=False)))
                        db.executemany("INSERT OR IGNORE INTO events(session_id,seq,payload) VALUES (?,?,?)",
                                       [(snapshot["session_id"], e["seq"], json.dumps(e, ensure_ascii=False)) for e in events])
                except Exception as exc:
                    self.errors.put(str(exc))
                finally:
                    self.jobs.task_done()
        finally:
            db.close()

    def flush(self) -> None:
        self.jobs.join()
        if not self.errors.empty():
            raise OSError(self.errors.get_nowait())

    def close(self) -> None:
        try:
            self.flush()
        finally:
            self.jobs.put(None)
            self.thread.join(timeout=15)

    def list_sessions(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT snapshot FROM sessions ORDER BY started_at DESC").fetchall()
        return [json.loads(row[0]) for row in rows]

    def load(self, session_id: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT snapshot FROM sessions WHERE id=?", (session_id,)).fetchone()
            if row is None:
                raise KeyError(session_id)
            snapshot = json.loads(row[0])
            snapshot["events"] = [json.loads(r[0]) for r in db.execute(
                "SELECT payload FROM events WHERE session_id=? ORDER BY seq", (session_id,))]
        return snapshot

    def save_protocol(self, settings: dict) -> None:
        with self.connect() as db:
            db.execute("INSERT INTO protocols(name,payload) VALUES (?,?) ON CONFLICT(name) DO UPDATE SET payload=excluded.payload",
                       (settings["title"], json.dumps(settings, ensure_ascii=False)))

    def protocols(self) -> dict:
        with self.connect() as db:
            return {name: json.loads(payload) for name, payload in db.execute("SELECT name,payload FROM protocols ORDER BY name")}
