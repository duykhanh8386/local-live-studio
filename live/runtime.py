"""Locations shared by source, portable, and the standalone Windows EXE."""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def data_directory() -> Path:
    if value := os.environ.get("LIVE_DATA_DIR"):
        return Path(value)
    if getattr(sys, "frozen", False):
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "LocalLiveStudio" / "data"
    return ROOT / "data"


def build_info() -> dict:
    project = json.loads((ROOT / "project.json").read_text(encoding="utf-8"))
    metadata = ROOT / "BUILD_INFO.json"
    info = json.loads(metadata.read_text(encoding="utf-8")) if metadata.is_file() else {}
    commit = info.get("commit", "")
    if not commit and not getattr(sys, "frozen", False):
        try:
            commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True,
                text=True, timeout=3, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    return {**project, "commit": commit, "built_at": info.get("built_at", ""),
            "packaged": bool(getattr(sys, "frozen", False)), "target": "windows-x64"}


def prepare_bundled_tools():
    if not getattr(sys, "frozen", False):
        return
    for name in ("ffmpeg", "ffprobe"):
        binary = ROOT / "tools" / (name + ".exe")
        if not binary.is_file():
            raise RuntimeError(f"Bản EXE thiếu {name}. Hãy tải lại bản phát hành.")
        os.environ[f"LIVE_{name.upper()}"] = str(binary)
