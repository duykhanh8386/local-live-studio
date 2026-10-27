"""Standalone, loopback-only launcher. Run python app.py --no-browser for headless use."""
import argparse
import errno
import socket
import sys
import threading
import time
import webbrowser

import uvicorn

from live.api import create_app
from live.diagnostics import probe_http
from live.runtime import build_info, prepare_bundled_tools


class LocalServer(uvicorn.Server):
    """Announce readiness and open the browser only after our HTTP endpoint responds."""

    def __init__(self, config, *, port, open_browser=True, on_ready=None):
        super().__init__(config)
        self.port = port
        self.open_browser = open_browser
        self.on_ready = on_ready
        self.ready = threading.Event()
        self.finished = threading.Event()
        self.readiness_error = ""
        self.ready_thread = None

    async def startup(self, sockets=None):
        await super().startup(sockets=sockets)
        if self.started:
            self.ready_thread = threading.Thread(target=self._wait_for_http, daemon=True,
                                                 name="local-web-ready")
            self.ready_thread.start()

    def _wait_for_http(self):
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                try:
                    stream.reconfigure(encoding="utf-8", errors="replace")
                except Exception:
                    pass
        deadline = time.monotonic() + 30
        last_error = ""
        while not self.finished.is_set() and time.monotonic() < deadline:
            try:
                result = probe_http(self.port, timeout=1)
                if result["status"] == 200 and result["service"] == "local-live-studio":
                    self.ready.set()
                    if self.on_ready:
                        self.on_ready()
                    url = f"http://127.0.0.1:{self.port}/"
                    try:
                        print(f"\n[OK] Máy chủ đã sẵn sàng (HTTP 200).\nLocal Live Studio: {url}\n"
                              "Giữ cửa sổ này mở để máy phát và lịch hoạt động. Ctrl+C để dừng.\n", flush=True)
                    except Exception:
                        pass
                    if self.open_browser:
                        try:
                            if not webbrowser.open(url):
                                print(f"Không tự mở được trình duyệt. Mở địa chỉ: {url}", flush=True)
                        except Exception as exc:
                            print(f"Không tự mở được trình duyệt: {exc}. Mở địa chỉ: {url}", flush=True)
                    return
                last_error = f"HTTP {result['status']}, chưa nhận được phản hồi của Local Live Studio."
            except (OSError, ValueError) as exc:
                last_error = str(exc)
            self.finished.wait(.1)
        if not self.finished.is_set():
            self.readiness_error = f"Máy chủ không phản hồi HTTP sau 30 giây: {last_error}"
            try:
                print(f"\n[LỖI] {self.readiness_error}", flush=True)
            except Exception:
                pass
            self.should_exit = True


def main(on_ready=None):
    parser = argparse.ArgumentParser(description="Local Live Studio")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--max-streams", type=int, default=4)
    parser.add_argument("--self-check", action="store_true")
    parser.add_argument("--version", action="store_true")
    args = parser.parse_args()
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    prepare_bundled_tools()
    if args.version:
        import json
        print(json.dumps(build_info(), ensure_ascii=False))
        return
    if args.self_check:
        from live.media import executable
        import subprocess
        for name in ("ffmpeg", "ffprobe"):
            subprocess.run([executable(name), "-version"], check=True, capture_output=True, timeout=15,
                           creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        print("OK: bundled FFmpeg/FFprobe and application imports")
        return
    if not 1024 <= args.port <= 65535 or not 1 <= args.max_streams <= 20:
        parser.error("Port: 1024–65535. Số luồng đồng thời: 1–20.")
    print(f"Đang khởi động Local Live Studio trên cổng {args.port}…", flush=True)
    # Reserve the socket until Uvicorn exits so another program cannot take it.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            listener.bind(("127.0.0.1", args.port))
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE or getattr(exc, "winerror", None) == 10048:
                message = f"Cổng {args.port} đang được chương trình khác sử dụng. Chọn cổng khác bằng --port 8770."
            else:
                message = f"Không mở được cổng {args.port}: {exc}"
            raise RuntimeError(message) from exc
        print("Đang mở dữ liệu và bộ đặt lịch…", flush=True)
        application = create_app(max_streams=args.max_streams)
        config = uvicorn.Config(application, host="127.0.0.1", port=args.port,
                                access_log=False, log_level="info")
        server = LocalServer(config, port=args.port, open_browser=not args.no_browser,
                             on_ready=on_ready)
        application.state.listen_port = args.port
        application.state.request_exit = lambda: setattr(server, "should_exit", True)
        try:
            server.run(sockets=[listener])
        finally:
            server.finished.set()
            if server.ready_thread:
                server.ready_thread.join(timeout=2)
        if server.readiness_error:
            raise RuntimeError(server.readiness_error)
        if not server.started:
            raise RuntimeError("Máy chủ dừng trước khi khởi động thành công. Xem lỗi bên trên.")


if __name__ == "__main__":
    main()
