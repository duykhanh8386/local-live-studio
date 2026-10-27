"""Read a user-selected Tuất channel locally; never expose cookies to the UI."""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from .studio import StudioIssue


def candidate_database_paths() -> list[Path]:
    if configured := os.environ.get("LIVE_TUAT_CHANNELS_DB"):
        path = Path(configured).expanduser()
        return [path]
    appdata = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    candidates = [
        appdata / "TVAutomation-7F47B95D" / "channels.db",
        appdata / "YoutubeAutomation" / "channels.db",
        appdata / "TVAutomation" / "channels.db",
    ]
    existing = [p for p in candidates if p.is_file()]
    return existing if existing else [candidates[0]]


def database_path() -> Path:
    candidates = candidate_database_paths()
    for p in candidates:
        if p.is_file():
            return p
    return candidates[0]


def _open(path: Path | None = None):
    target = (path or database_path()).resolve()
    if not target.is_file():
        raise StudioIssue("Chưa tìm thấy dữ liệu kênh của tool Tuất trên máy này. Hãy đăng nhập và load kênh trong tool Tuất trước.", "needs_login")
    # mode=ro also prevents creating or migrating Tuất's database.
    return sqlite3.connect(target.as_uri() + "?mode=ro", uri=True, timeout=3)


def list_channels():
    candidates = candidate_database_paths()
    channels_by_id = {}
    for path in reversed(candidates):
        if not path.is_file():
            continue
        try:
            with closing(_open(path)) as connection:
                rows = connection.execute("SELECT id, name FROM channels ORDER BY name, id").fetchall()
            for identifier, name in rows:
                channel_id = str(identifier or "")
                if re.fullmatch(r"UC[A-Za-z0-9_-]{22}", channel_id):
                    channels_by_id[channel_id] = {"id": channel_id, "name": name or channel_id}
        except (sqlite3.Error, OSError):
            continue
    if not channels_by_id and not any(p.is_file() for p in candidates):
        return []
    return sorted(channels_by_id.values(), key=lambda item: (item["name"].casefold(), item["id"]))


def normalize_cookies(raw, *, now=None):
    timestamp = time.time() if now is None else now
    if not isinstance(raw, list) or len(raw) > 512:
        raise StudioIssue("Phiên YouTube trong tool Tuất không hợp lệ. Hãy load lại kênh.", "needs_login")
    cookies = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        domain = str(item.get("domain") or "").lower()
        host = domain.lstrip(".")
        if not any(host == allowed or host.endswith("." + allowed) for allowed in ("youtube.com", "google.com")):
            continue
        name, value = item.get("name"), item.get("value")
        if not isinstance(name, str) or not name or not isinstance(value, str):
            continue
        cookie = dict(name=name, value=value, secure=bool(item.get("secure")), httpOnly=bool(item.get("httpOnly")))
        path = str(item.get("path") or "/")
        if not path.startswith("/"):
            continue
        if item.get("hostOnly") or name.startswith("__Host-"):
            cookie["url"] = "https://" + host + path
        else:
            cookie.update(domain=domain, path=path)
        same_site = str(item.get("sameSite") or "").lower()
        cookie["sameSite"] = {"lax": "Lax", "strict": "Strict"}.get(same_site, "None")
        if cookie["sameSite"] == "None":
            cookie["secure"] = True
        expires = item.get("expirationDate", item.get("expiry", item.get("expires")))
        if expires is not None:
            try:
                expires = float(expires)
            except (ValueError, TypeError, OverflowError):
                continue
            if not math.isfinite(expires):
                continue
            if expires > 0:
                if expires <= timestamp:
                    continue
                cookie["expires"] = expires
        cookies.append(cookie)
    if not any(cookie["name"] == "SAPISID" and cookie["value"] for cookie in cookies):
        raise StudioIssue("Phiên YouTube trong tool Tuất đã hết hạn hoặc thiếu cookie đăng nhập. Load lại kênh trong tool Tuất rồi kết nối lại.", "needs_login")
    return cookies


def load_channel_session(channel_id):
    if not re.fullmatch(r"UC[A-Za-z0-9_-]{22}", str(channel_id or "")):
        raise StudioIssue("Channel ID không hợp lệ.")
    candidates = candidate_database_paths()
    if not any(p.is_file() for p in candidates):
        raise StudioIssue("Chưa tìm thấy dữ liệu kênh của tool Tuất trên máy này. Hãy đăng nhập và load kênh trong tool Tuất trước.", "needs_login")
    row = None
    for path in candidates:
        if not path.is_file():
            continue
        try:
            with closing(_open(path)) as connection:
                found = connection.execute("SELECT cookies_json FROM channels WHERE id=?", (channel_id,)).fetchone()
            if found and found[0]:
                row = found
                break
        except (sqlite3.Error, OSError):
            continue
    if not row:
        raise StudioIssue("Kênh đã chọn không còn trong tool Tuất. Lấy lại danh sách kênh.", "needs_login")
    if len(row[0] or "") > 2 * 1024 * 1024:
        raise ValueError()
    return normalize_cookies(json.loads(row[0]))
