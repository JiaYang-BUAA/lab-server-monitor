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

from .board_segments import initial_segments, reconcile_segments


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
                CREATE TABLE IF NOT EXISTS server_names (
                    host_id TEXT PRIMARY KEY, name TEXT NOT NULL, updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS members (
                    name TEXT PRIMARY KEY COLLATE NOCASE, created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS job_groups (
                    id TEXT PRIMARY KEY, host_id TEXT NOT NULL, name TEXT NOT NULL,
                    job_ids TEXT NOT NULL, created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS board_segments (
                    id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL
                );
            """)
            # Existing installations have claim names but no member directory.
            # Import them once; repeated startup is harmless and keeps the names
            # available after a claim has ended or been released.
            for row in db.execute("SELECT payload,created_at FROM claims ORDER BY created_at").fetchall():
                try:
                    name = json.loads(row["payload"]).get("owner_name")
                except (ValueError, AttributeError):
                    continue
                if isinstance(name, str) and 1 <= len(name.strip()) <= 40 and "\x00" not in name:
                    db.execute("INSERT OR IGNORE INTO members(name,created_at) VALUES(?,?)",
                               (name.strip(), row["created_at"]))
            exists = db.execute("SELECT 1 FROM board_segments WHERE id=1").fetchone()
            if not exists:
                old = db.execute("SELECT text,updated_at FROM shared_board WHERE id=1").fetchone()
                whole_board_time = (datetime.fromtimestamp(old["updated_at"], timezone.utc).isoformat()
                                    if old["updated_at"] is not None else None)
                db.execute("INSERT INTO board_segments(id,payload) VALUES(1,?)",
                           (json.dumps(initial_segments(old["text"], whole_board_time), ensure_ascii=False),))

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

    @staticmethod
    def _member_name(name):
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 40 or "\x00" in name:
            raise ValueError("成员姓名需为 1–40 个字符")
        return name.strip()

    def members(self):
        with self._connect() as db:
            rows = db.execute("SELECT name FROM members ORDER BY created_at,name COLLATE NOCASE").fetchall()
        return [row["name"] for row in rows]

    def add_member(self, name):
        name = self._member_name(name)
        with self._lock, self._connect() as db:
            db.execute("INSERT OR IGNORE INTO members(name,created_at) VALUES(?,?)", (name, time.time()))
            row = db.execute("SELECT name FROM members WHERE name=? COLLATE NOCASE", (name,)).fetchone()
        return row["name"]

    def server_names(self):
        with self._connect() as db:
            rows = db.execute("SELECT host_id,name FROM server_names").fetchall()
        return {row["host_id"]: row["name"] for row in rows}

    def rename_server(self, host_id: str, name: str):
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 40 or "\x00" in name:
            raise ValueError("服务器名称需为 1–40 个字符")
        name = name.strip()
        with self._lock, self._connect() as db:
            db.execute("INSERT INTO server_names(host_id,name,updated_at) VALUES(?,?,?) "
                       "ON CONFLICT(host_id) DO UPDATE SET name=excluded.name,updated_at=excluded.updated_at",
                       (host_id, name, time.time()))
        return name

    def job_groups(self, host_id: str | None = None):
        with self._connect() as db:
            rows = db.execute("SELECT * FROM job_groups WHERE (? IS NULL OR host_id=?) ORDER BY created_at",
                              (host_id, host_id)).fetchall()
        return [{"id": row["id"], "host_id": row["host_id"], "name": row["name"],
                 "job_ids": json.loads(row["job_ids"])} for row in rows]

    def create_job_group(self, host_id: str, job_ids: list[str], name: str):
        if not isinstance(job_ids, list) or len(job_ids) < 2 or len(job_ids) > 32 or any(
                not isinstance(job_id, str) or not job_id for job_id in job_ids) or len(set(job_ids)) != len(job_ids):
            raise ValueError("请选择同一服务器上的 2–32 个不同任务")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 120 or "\x00" in name:
            raise ValueError("归组名称需为 1–120 个字符")
        group = {"id": uuid.uuid4().hex[:24], "host_id": host_id, "name": name.strip(), "job_ids": job_ids}
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT job_ids FROM job_groups WHERE host_id=?", (host_id,)).fetchall()
            assigned = {item for row in rows for item in json.loads(row["job_ids"])}
            if assigned.intersection(job_ids):
                raise ConflictError("所选任务已有归组，请刷新后重试")
            placeholders = ",".join("?" for _ in job_ids)
            claimed = db.execute(f"SELECT 1 FROM claims WHERE host_id=? AND job_id IN ({placeholders}) "
                                 "AND released_at IS NULL AND ended_at IS NULL LIMIT 1", (host_id, *job_ids)).fetchone()
            if claimed:
                raise ConflictError("所选任务已有登记，请先释放原登记再归组")
            db.execute("INSERT INTO job_groups(id,host_id,name,job_ids,created_at) VALUES(?,?,?,?,?)",
                       (group["id"], host_id, name.strip(), json.dumps(job_ids), time.time()))
        return group

    def delete_job_group(self, group_id: str):
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT host_id FROM job_groups WHERE id=?", (group_id,)).fetchone()
            if row is None:
                raise MissingError("归组不存在")
            active = db.execute("SELECT 1 FROM claims WHERE host_id=? AND job_id=? "
                                "AND released_at IS NULL AND ended_at IS NULL LIMIT 1",
                                (row["host_id"], group_id)).fetchone()
            if active:
                raise ConflictError("归组已被认领，请先释放登记")
            db.execute("DELETE FROM job_groups WHERE id=?", (group_id,))

    def board(self):
        with self._connect() as db:
            row = db.execute("SELECT text,revision,updated_at,updated_by FROM shared_board WHERE id=1").fetchone()
            segments = json.loads(db.execute("SELECT payload FROM board_segments WHERE id=1").fetchone()["payload"])
        result = dict(row)
        if result["updated_at"] is not None:
            result["updated_at"] = datetime.fromtimestamp(result["updated_at"], timezone.utc).isoformat()
        result["paragraphs"] = segments
        return result

    def update_board(self, text: str, revision: int, editor_name: str):
        if not isinstance(text, str) or len(text) > 20000 or "\x00" in text:
            raise ValueError("共享备注最多 20000 个字符，不能包含空字符")
        if type(revision) is not int or revision < 0:
            raise ValueError("共享备注版本无效，请重新读取")
        if not isinstance(editor_name, str) or not 1 <= len(editor_name.strip()) <= 40 or "\x00" in editor_name:
            raise ValueError("请填写 1–40 个字符的修改者姓名")
        text = text.replace("\r\n", "\n")
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = json.loads(db.execute("SELECT payload FROM board_segments WHERE id=1").fetchone()["payload"])
            now = time.time()
            moment = datetime.fromtimestamp(now, timezone.utc).isoformat()
            updated = db.execute("UPDATE shared_board SET text=?,revision=revision+1,updated_at=?,updated_by=? WHERE id=1 AND revision=?",
                (text, now, editor_name.strip(), revision))
            if updated.rowcount != 1:
                raise ConflictError("其他成员已更新共享备注。你的草稿已保留，请核对最新版后再保存")
            segments = reconcile_segments(previous, text, editor_name.strip(), moment)
            db.execute("UPDATE board_segments SET payload=? WHERE id=1",
                       (json.dumps(segments, ensure_ascii=False),))
            row = db.execute("SELECT text,revision,updated_at,updated_by FROM shared_board WHERE id=1").fetchone()
        return {**dict(row), "updated_at": moment, "paragraphs": segments}

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
                db.execute("INSERT OR IGNORE INTO members(name,created_at) VALUES(?,?)",
                           (self._member_name(payload["owner_name"]), now))
                db.execute("INSERT INTO changes(at,session_id,action,claim_id,payload) VALUES(?,?,?,?,?)",
                           (now, session_id, "create", claim_id, encoded))
        except sqlite3.IntegrityError as exc:
            raise ConflictError("这个任务已被其他会话登记，请刷新后查看") from exc
        return claim_id

    def update(self, token: str, claim_id: str, payload: dict | None, expected_updated_at=None):
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
            if expected_updated_at is not None and row["updated_at"] != expected_updated_at:
                raise ConflictError("其他成员已修改此任务，请刷新后重试")
            now = max(now, row["updated_at"] + 0.000001)
            if payload is None:
                db.execute("UPDATE claims SET released_at=?,updated_at=? WHERE id=?", (now, now, claim_id))
                action, encoded = "release", row["payload"]
            else:
                encoded = json.dumps(payload, ensure_ascii=False)
                db.execute("UPDATE claims SET payload=?,updated_at=? WHERE id=?", (encoded, now, claim_id))
                db.execute("INSERT OR IGNORE INTO members(name,created_at) VALUES(?,?)",
                           (self._member_name(payload["owner_name"]), now))
                action = "update"
            db.execute("INSERT INTO changes(at,session_id,action,claim_id,payload) VALUES(?,?,?,?,?)",
                       (now, key, action, claim_id, encoded))

    def upsert_annotation(self, token: str, host_id: str, job_id: str, task_name: str,
                          owner_name: str, notes: str, expected_updated_at):
        """Update only the public name/note fields without transferring claim ownership."""
        now, key = time.time(), self.session_key(token)
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM claims WHERE host_id=? AND job_id=? "
                             "AND released_at IS NULL AND ended_at IS NULL", (host_id, job_id)).fetchone()
            if row is None:
                if expected_updated_at is not None:
                    raise ConflictError("任务登记已变化，请刷新后重试")
                claim_id = uuid.uuid4().hex
                payload = {"owner_name": owner_name, "task_name": task_name,
                           "expected_end": None, "notes": notes, "log_path": "",
                           "log_kind": "abaqus", "total_units": None}
                encoded = json.dumps(payload, ensure_ascii=False)
                db.execute("INSERT INTO claims VALUES(?,?,?,?,?,?,?,NULL,NULL)",
                           (claim_id, host_id, job_id, key, encoded, now, now))
                action = "annotation_create"
            else:
                if expected_updated_at != row["updated_at"]:
                    raise ConflictError("其他成员已修改此任务，请刷新后重试")
                claim_id = row["id"]
                payload = json.loads(row["payload"])
                payload.update(owner_name=owner_name, notes=notes)
                encoded = json.dumps(payload, ensure_ascii=False)
                now = max(now, row["updated_at"] + 0.000001)
                db.execute("UPDATE claims SET payload=?,updated_at=? WHERE id=?", (encoded, now, claim_id))
                action = "annotation_update"
            db.execute("INSERT OR IGNORE INTO members(name,created_at) VALUES(?,?)", (owner_name, now))
            db.execute("INSERT INTO changes(at,session_id,action,claim_id,payload) VALUES(?,?,?,?,?)",
                       (now, key, action, claim_id, encoded))
            result = db.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()
        return self._decode(result, key)

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
