"""Transactional browser identities and explicitly registered tasks."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from contextlib import contextmanager
import json
from pathlib import Path
import secrets
import sqlite3
import threading
import time
import uuid


class ConflictError(Exception):
    pass


class OwnershipError(Exception):
    pass


class MissingError(Exception):
    pass


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL, last_seen REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS claims (
                    id TEXT PRIMARY KEY, host_id TEXT NOT NULL, job_id TEXT NOT NULL,
                    session_id TEXT NOT NULL, payload TEXT NOT NULL,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    released_at REAL, ended_at REAL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS active_job_claim
                    ON claims(host_id, job_id) WHERE released_at IS NULL AND ended_at IS NULL;
                CREATE TABLE IF NOT EXISTS changes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL,
                    session_id TEXT NOT NULL, action TEXT NOT NULL,
                    claim_id TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS shared_board (
                    id INTEGER PRIMARY KEY CHECK(id=1), text TEXT NOT NULL DEFAULT '',
                    revision INTEGER NOT NULL DEFAULT 0, updated_at REAL,
                    updated_by TEXT NOT NULL DEFAULT ''
                );
                INSERT OR IGNORE INTO shared_board(id) VALUES(1);
            """)

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def session_key(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def session(self, token: str | None) -> tuple[str, str, bool]:
        now = time.time()
        with self._lock, self._connect() as db:
            if token and len(token) <= 128:
                row = db.execute("SELECT name FROM sessions WHERE id=?", (self.session_key(token),)).fetchone()
                if row:
                    db.execute("UPDATE sessions SET last_seen=? WHERE id=?", (now, self.session_key(token)))
                    return token, row["name"], False
            token = secrets.token_urlsafe(32)
            db.execute("INSERT INTO sessions(id,created_at,last_seen) VALUES(?,?,?)",
                       (self.session_key(token), now, now))
            return token, "", True

    def identity(self, token: str, name: str) -> None:
        with self._lock, self._connect() as db:
            db.execute("UPDATE sessions SET name=?,last_seen=? WHERE id=?",
                       (name, time.time(), self.session_key(token)))

    def board(self):
        with self._connect() as db:
            row = db.execute("SELECT text,revision,updated_at,updated_by FROM shared_board WHERE id=1").fetchone()
        result = dict(row)
        if result["updated_at"] is not None:
            result["updated_at"] = datetime.fromtimestamp(result["updated_at"], timezone.utc).isoformat()
        return result

    def update_board(self, text: str, revision: int, editor_name: str):
        if not isinstance(text, str) or len(text) > 20000 or "\x00" in text:
            raise ValueError("共享备注最多 20000 个字符，不能包含空字符")
        if type(revision) is not int or revision < 0:
            raise ValueError("共享备注版本无效，请重新读取")
        if not isinstance(editor_name, str) or len(editor_name.strip()) > 40 or "\x00" in editor_name:
            raise ValueError("修改者姓名最多 40 个字符")
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            updated = db.execute("UPDATE shared_board SET text=?,revision=revision+1,updated_at=?,updated_by=? WHERE id=1 AND revision=?",
                (text.replace("\r\n", "\n"), time.time(), editor_name.strip(), revision))
            if updated.rowcount != 1:
                raise ConflictError("其他成员已更新共享备注。你的草稿已保留，请核对最新版后再保存")
            row = db.execute("SELECT text,revision,updated_at,updated_by FROM shared_board WHERE id=1").fetchone()
        return {**dict(row), "updated_at": datetime.fromtimestamp(row["updated_at"], timezone.utc).isoformat()}

    @staticmethod
    def _decode(row, session_id: str | None = None):
        payload = json.loads(row["payload"])
        return {**payload, "id": row["id"], "host_id": row["host_id"],
                "job_id": row["job_id"], "created_at": row["created_at"],
                "updated_at": row["updated_at"], "ended_at": row["ended_at"],
                "released_at": row["released_at"],
                "can_edit": session_id is not None and session_id == row["session_id"]}

    def claims(self, token: str | None = None, *, recent: bool = False):
        key = self.session_key(token) if token else None
        where = "" if recent else "WHERE released_at IS NULL AND ended_at IS NULL"
        with self._connect() as db:
            rows = db.execute(f"SELECT * FROM claims {where} ORDER BY updated_at DESC LIMIT 200").fetchall()
        return [self._decode(row, key) for row in rows]

    def claim(self, claim_id: str, token: str | None = None):
        with self._connect() as db:
            row = db.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()
        if row is None:
            raise MissingError("登记不存在")
        return self._decode(row, self.session_key(token) if token else None)

    def create(self, token: str, host_id: str, job_id: str, payload: dict):
        now, claim_id, session_id = time.time(), uuid.uuid4().hex, self.session_key(token)
        encoded = json.dumps(payload, ensure_ascii=False)
        try:
            with self._lock, self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("INSERT INTO claims VALUES(?,?,?,?,?,?,?,NULL,NULL)",
                           (claim_id, host_id, job_id, session_id, encoded, now, now))
                db.execute("INSERT INTO changes(at,session_id,action,claim_id,payload) VALUES(?,?,?,?,?)",
                           (now, session_id, "create", claim_id, encoded))
        except sqlite3.IntegrityError as exc:
            raise ConflictError("这个任务已被其他会话登记，请刷新后查看") from exc
        return claim_id

    def update(self, token: str, claim_id: str, payload: dict | None):
        now, key = time.time(), self.session_key(token)
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()
            if row is None:
                raise MissingError("登记不存在")
            if row["session_id"] != key:
                raise OwnershipError("仅登记时使用的浏览器会话可以修改或释放登记")
            if row["released_at"] is not None or row["ended_at"] is not None:
                raise ConflictError("登记已结束或已释放，请刷新")
            if payload is None:
                db.execute("UPDATE claims SET released_at=?,updated_at=? WHERE id=?", (now, now, claim_id))
                action, encoded = "release", row["payload"]
            else:
                encoded = json.dumps(payload, ensure_ascii=False)
                db.execute("UPDATE claims SET payload=?,updated_at=? WHERE id=?", (encoded, now, claim_id))
                action = "update"
            db.execute("INSERT INTO changes(at,session_id,action,claim_id,payload) VALUES(?,?,?,?,?)",
                       (now, key, action, claim_id, encoded))

    def end_job(self, host_id: str, job_id: str):
        now = time.time()
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT * FROM claims WHERE host_id=? AND job_id=? AND released_at IS NULL AND ended_at IS NULL",
                              (host_id, job_id)).fetchall()
            for row in rows:
                db.execute("UPDATE claims SET ended_at=?,updated_at=? WHERE id=?", (now, now, row["id"]))
                db.execute("INSERT INTO changes(at,session_id,action,claim_id,payload) VALUES(?,?,?,?,?)",
                           (now, row["session_id"], "processes_ended", row["id"], row["payload"]))
