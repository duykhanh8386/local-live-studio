from __future__ import annotations

import base64
import ctypes
import json
import os
import sqlite3
import threading
import time
from pathlib import Path


class SecretBox:
    """Use Windows user-scoped DPAPI; never return keys in public records."""

    def _windows(self, data: bytes, encrypt: bool) -> bytes:
        from ctypes import wintypes

        class Blob(ctypes.Structure):
            _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

        buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
        source = Blob(len(data), buffer)
        target = Blob()
        crypt = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        function = crypt.CryptProtectData if encrypt else crypt.CryptUnprotectData
        function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                             ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
                             ctypes.POINTER(Blob)]
        function.restype = wintypes.BOOL
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
            raise OSError(ctypes.get_last_error(), "Không thể xử lý stream key bằng DPAPI.")
        try:
            return ctypes.string_at(target.data, target.size)
        finally:
            kernel.LocalFree(target.data)

    def seal(self, key: str) -> str:
        data = key.encode("utf-8")
        prefix = "dpapi:" if os.name == "nt" else "local:"
        if os.name == "nt":
            data = self._windows(data, True)
        return prefix + base64.b64encode(data).decode("ascii")

    def open(self, token: str) -> str:
        kind, encoded = token.split(":", 1)
        data = base64.b64decode(encoded)
        if kind == "dpapi":
            if os.name != "nt":
                raise ValueError("Stream key này thuộc tài khoản Windows đã lưu lịch.")
            data = self._windows(data, False)
        elif kind != "local":
            raise ValueError("Định dạng stream key không hợp lệ.")
        return data.decode("utf-8")


class Store:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.directory = directory.resolve()
        self.lock = threading.RLock()
        self.connection = sqlite3.connect(directory / "streams.db", check_same_thread=False)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS streams(id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS logs(seq INTEGER PRIMARY KEY AUTOINCREMENT,
                stream_id TEXT NOT NULL, timestamp REAL NOT NULL, message TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS archives(id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS settings(name TEXT PRIMARY KEY, payload TEXT NOT NULL);
        """)
        self.connection.commit()

    def list(self) -> list[dict]:
        with self.lock:
            return [json.loads(row[0]) for row in self.connection.execute(
                "SELECT payload FROM streams ORDER BY rowid DESC"
            ).fetchall()]

    def get(self, stream_id: str) -> dict:
        with self.lock:
            row = self.connection.execute(
                "SELECT payload FROM streams WHERE id=?", (stream_id,)
            ).fetchone()
            if row is None:
                raise KeyError(stream_id)
            return json.loads(row[0])

    def save(self, record: dict) -> dict:
        with self.lock:
            self.connection.execute(
                "INSERT INTO streams(id,payload) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                (record["id"], json.dumps(record, ensure_ascii=False)),
            )
            self.connection.commit()
        return record

    def update(self, stream_id: str, **changes) -> dict:
        with self.lock:
            record = self.get(stream_id)
            record.update(changes)
            return self.save(record)

    def delete(self, stream_id: str):
        with self.lock:
            self.get(stream_id)
            self.connection.execute("DELETE FROM streams WHERE id=?", (stream_id,))
            self.connection.execute("DELETE FROM logs WHERE stream_id=?", (stream_id,))
            self.connection.commit()

    def log(self, stream_id: str, message: str):
        with self.lock:
            self.connection.execute("INSERT INTO logs(stream_id,timestamp,message) VALUES(?,?,?)",
                                    (stream_id, time.time(), message[-2000:]))
            self.connection.execute("""DELETE FROM logs WHERE stream_id=? AND seq NOT IN
                (SELECT seq FROM logs WHERE stream_id=? ORDER BY seq DESC LIMIT 300)""",
                                    (stream_id, stream_id))
            self.connection.commit()

    def logs(self, stream_id: str) -> list[dict]:
        with self.lock:
            return [dict(timestamp=row[0], message=row[1]) for row in self.connection.execute(
                "SELECT timestamp,message FROM logs WHERE stream_id=? ORDER BY seq", (stream_id,)
            ).fetchall()]

    def close(self):
        with self.lock:
            self.connection.close()

    def archives(self) -> list[dict]:
        with self.lock:
            return [json.loads(row[0]) for row in self.connection.execute(
                "SELECT payload FROM archives ORDER BY rowid DESC").fetchall()]

    def archive(self, task_id: str) -> dict:
        with self.lock:
            row = self.connection.execute("SELECT payload FROM archives WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            return json.loads(row[0])

    def save_archive(self, record: dict) -> dict:
        with self.lock:
            self.connection.execute(
                "INSERT INTO archives(id,payload) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                (record["id"], json.dumps(record, ensure_ascii=False)))
            self.connection.commit()
            return record

    def setting(self, name: str) -> dict:
        with self.lock:
            row = self.connection.execute("SELECT payload FROM settings WHERE name=?", (name,)).fetchone()
            return json.loads(row[0]) if row else {}

    def save_setting(self, name: str, value: dict):
        with self.lock:
            self.connection.execute(
                "INSERT INTO settings(name,payload) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET payload=excluded.payload",
                (name, json.dumps(value, ensure_ascii=False)))
            self.connection.commit()
