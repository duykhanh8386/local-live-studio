"""Bootstrap the bundled interpreter and tools without a machine installation."""
from __future__ import annotations

import argparse
import faulthandler
import hashlib
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOG_PATH = None


class LogStream:
    """Write to disk first so a paused Windows console still leaves diagnostics."""

    def __init__(self, original, logfile, lock):
        self.original, self.logfile, self.lock = original, logfile, lock

    def write(self, value):
        with self.lock:
            self.logfile.write(value)
            self.logfile.flush()
            return self.original.write(value)

    def flush(self):
        with self.lock:
            self.logfile.flush()
            self.original.flush()

    def __getattr__(self, name):
        return getattr(self.original, name)


def configure_logging():
    global LOG_PATH
    arguments = sys.argv[1:]
    name = "diagnostic.txt" if "--diagnose" in arguments else (
        "setup.log" if "--check" in arguments else "startup.log")
    originals = (sys.stdout, sys.stderr)
    for stream in originals:
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    LOG_PATH = ROOT / "data" / name
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        logfile = LOG_PATH.open("a", encoding="utf-8", buffering=1)
    except OSError:
        LOG_PATH = Path(tempfile.gettempdir()) / f"LocalLiveStudio-{os.getpid()}-{name}"
        logfile = LOG_PATH.open("a", encoding="utf-8", buffering=1)
    lock = threading.RLock()
    sys.stdout, sys.stderr = (LogStream(stream, logfile, lock) for stream in originals)
    stamp = datetime.now(timezone(timedelta(hours=7))).isoformat(timespec="seconds")
    print(f"\n--- Local Live Studio · {stamp} · PID {os.getpid()} ---\nLog: {LOG_PATH}", flush=True)
    return logfile, originals


def prepare():
    if os.name != "nt" or struct.calcsize("P") != 8 or sys.getwindowsversion().major < 10:
        raise RuntimeError("Bản portable này cần Windows 10/11 64-bit.")
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    os.chdir(ROOT)
    for name in ("ffmpeg", "ffprobe"):
        path = ROOT / "runtime" / "ffmpeg" / "bin" / f"{name}.exe"
        if not path.is_file():
            raise RuntimeError(f"Thiếu {name}.exe. Giải nén lại toàn bộ ZIP trước khi chạy.")
        os.environ[f"LIVE_{name.upper()}"] = str(path)
    os.environ["LIVE_DATA_DIR"] = str(ROOT / "data")
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"


def verify_files():
    manifest = json.loads((ROOT / "BUNDLE_MANIFEST.json").read_text(encoding="utf-8"))
    for name, expected in manifest["files"].items():
        path = (ROOT / name).resolve()
        if not path.is_relative_to(ROOT) or not path.is_file():
            raise RuntimeError(f"Thiếu file trong gói: {name}")
        with path.open("rb") as handle:
            if hashlib.file_digest(handle, "sha256").hexdigest() != expected:
                raise RuntimeError(f"File không khớp gói gốc: {name}. Giải nén lại ZIP.")
    print(f"OK · {len(manifest['files'])} file khớp SHA-256.")


def check():
    import fastapi
    import pydantic
    import uvicorn
    import playwright.sync_api
    from live.storage import SecretBox
    from live.processes import ProcessGroup

    print(f"Python {sys.version.split()[0]} · {sys.executable}")
    print(f"FastAPI {fastapi.__version__} · Pydantic {pydantic.__version__} · Uvicorn {uvicorn.__version__}")
    for name in ("ffmpeg", "ffprobe"):
        result = subprocess.run([os.environ[f"LIVE_{name.upper()}"], "-version"], capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            raise RuntimeError(f"Không chạy được {name} trên máy này.")
        print(result.stdout.splitlines()[0])
    box = SecretBox()
    if box.open(box.seal("portable-check")) != "portable-check":
        raise RuntimeError("Không kiểm tra được DPAPI của tài khoản Windows.")
    group = ProcessGroup()
    group.close()
    print("OK · DPAPI, nhóm tiến trình và thư viện đã sẵn sàng.")


def diagnose(port):
    from live.diagnostics import probe_http

    print(f"\nKIỂM TRA WEB: http://127.0.0.1:{port}/\nThư mục ứng dụng: {ROOT}", flush=True)
    print(f"Windows: {sys.getwindowsversion()} · Python: {sys.executable}", flush=True)
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=3):
            print(f"[OK] Kết nối TCP tới cổng {port} thành công.", flush=True)
    except OSError as exc:
        print(f"[LỖI] Không kết nối được tới cổng {port}: {exc}\n"
              "Giữ START_LIVE.bat mở và xem lỗi khởi động bên dưới.", flush=True)
    else:
        try:
            result = probe_http(port, timeout=3)
            if result["status"] == 200 and result["service"] == "local-live-studio":
                print("[OK] Local Live Studio trả về HTTP 200. Máy chủ web đang chạy.\n"
                      f"Nếu trình duyệt vẫn lỗi, mở http://127.0.0.1:{port}/ trong trình duyệt khác.", flush=True)
            else:
                print(f"[LỖI] HTTP {result['status']}; chưa xác nhận được phiên bản mới của Local Live Studio.\n"
                      "Đóng cửa sổ chương trình bản cũ rồi chạy START_LIVE.bat trong bản mới.", flush=True)
        except (OSError, ValueError) as exc:
            print(f"[LỖI] Cổng có kết nối TCP nhưng không nhận được HTTP hợp lệ: {exc}", flush=True)
    try:
        with tempfile.TemporaryFile(dir=ROOT / "data") as writable:
            writable.write(b"local-live-write-check")
            writable.flush()
        print("[OK] Có quyền ghi dữ liệu.", flush=True)
    except OSError as exc:
        print(f"[LỖI] Không ghi được dữ liệu: {exc}. Giải nén vào thư mục có quyền ghi.", flush=True)
    try:
        check()
    except Exception as exc:
        print(f"[LỖI] Kiểm tra thành phần: {exc}", flush=True)
    startup = ROOT / "data" / "startup.log"
    print("\nCÁC DÒNG KHỞI ĐỘNG GẦN NHẤT:", flush=True)
    if startup.is_file():
        with startup.open("rb") as handle:
            handle.seek(max(0, startup.stat().st_size - 32768))
            tail = handle.read().decode("utf-8", errors="replace")
        print("\n".join(tail.splitlines()[-60:]), flush=True)
    else:
        print("Chưa có startup.log. Chạy START_LIVE.bat trong thư mục này trước.", flush=True)
    print(f"\nKết quả đã lưu tại: {LOG_PATH}\nGửi các dòng [LỖI] và phần khởi động gần nhất để xác định nguyên nhân.", flush=True)


def main():
    prepare()
    if "--diagnose" in sys.argv[1:]:
        parser = argparse.ArgumentParser(description="Kiểm tra web Local Live Studio")
        parser.add_argument("--diagnose", action="store_true")
        parser.add_argument("--port", type=int, default=8765)
        args = parser.parse_args()
        if not 1024 <= args.port <= 65535:
            parser.error("Cổng phải trong khoảng 1024–65535.")
        diagnose(args.port)
        return
    if "--check" in sys.argv[1:]:
        check()
        if "--verify" in sys.argv[1:]:
            verify_files()
        print("Sẵn sàng. Nhấp đúp START_LIVE.bat để mở web.")
        return
    # Explicit Python entry point; this never invokes pip or the system Python.
    print("Đang nạp thư viện máy chủ…", flush=True)
    faulthandler.dump_traceback_later(30, file=sys.stderr.logfile)
    from app import main as launch
    launch(on_ready=faulthandler.cancel_dump_traceback_later)


if __name__ == "__main__":
    logfile = None
    original_streams = (sys.stdout, sys.stderr)
    try:
        logfile, original_streams = configure_logging()
        faulthandler.enable(file=logfile)
        main()
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        traceback.print_exc()
        print(f"\n[LỖI] Không khởi động được: {exc}\nChạy DIAGNOSE_LIVE.bat; log lỗi: {LOG_PATH}", file=sys.stderr, flush=True)
        raise SystemExit(1)
    finally:
        faulthandler.cancel_dump_traceback_later()
        faulthandler.disable()
        sys.stdout, sys.stderr = original_streams
        if logfile:
            logfile.close()
