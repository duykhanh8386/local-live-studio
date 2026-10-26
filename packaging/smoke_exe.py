"""Smoke-test the actual EXE, bundled media tools and local API in a clean folder."""
import argparse
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("executable", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="local-live-exe-smoke-") as folder:
        directory = Path(folder).resolve()
        assert directory.is_relative_to(Path(tempfile.gettempdir()).resolve())
        target = directory / "máy nhận có dấu"
        target.mkdir()
        binary = target / "LocalLiveStudio.exe"
        shutil.copy2(args.executable, binary)
        env = os.environ.copy()
        env["LIVE_DATA_DIR"] = str(directory / "private-test-data")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        token = ""

        def api(path, method="GET", body=None):
            data = json.dumps(body).encode() if body is not None else None
            request = urllib.request.Request(base + path, data=data, method=method,
                headers={"X-Local-Token": token, "Content-Type": "application/json"})
            with opener.open(request, timeout=5) as response:
                return json.load(response)

        with (directory / "app.log").open("wb") as log:
            process = subprocess.Popen([str(binary), "--port", str(port), "--no-browser"], cwd=target,
                env=env, stdout=log, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                deadline = time.monotonic() + 120
                while True:
                    try:
                        assert api("/api/health")["service"] == "local-live-studio"
                        break
                    except Exception:
                        if process.poll() is not None or time.monotonic() > deadline:
                            raise RuntimeError((directory / "app.log").read_text(encoding="utf-8", errors="replace"))
                        time.sleep(.25)
                system = api("/api/system")
                token = system["token"]
                assert api("/api/streams") == []
                assert api("/api/archives")["tasks"] == []
                assert api("/api/update")["installed"]["packaged"]
                video = directory / "synthetic.mp4"
                subprocess.run([system["tools"]["ffmpeg"], "-v", "error", "-f", "lavfi", "-i",
                    "testsrc2=size=64x64:rate=10", "-t", "1", "-c:v", "libx264", str(video)],
                    check=True, capture_output=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
                assert api("/api/probe?path=" + urllib.parse.quote(str(video)))["duration"] > 0
                api("/api/streams", "POST", {"name": "Packaged smoke test", "source_path": str(video),
                    "stream_key": "synthetic-key-never-sent-to-youtube", "mode": "public", "duration": 3,
                    "start_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()})
                assert len(api("/api/streams")) == 1
                api("/api/app/stop", "POST")
                process.wait(timeout=45)
                assert process.returncode == 0
                print("Packaged EXE: HTTP, assets, libraries, FFmpeg, persistence and graceful exit OK")
            finally:
                if process.poll() is None:
                    try:
                        api("/api/app/stop", "POST")
                        process.wait(timeout=30)
                    except Exception:
                        # Only this test process tree; no process-name matching.
                        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True)
                        process.wait(timeout=10)


if __name__ == "__main__":
    main()
