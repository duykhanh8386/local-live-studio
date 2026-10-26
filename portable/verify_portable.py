"""Test the actual ZIP after relocation with system Python/FFmpeg off PATH."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from live.processes import ProcessGroup


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("zip", type=Path, nargs="?")
    args = parser.parse_args()
    path = args.zip or Path(json.loads((ROOT / "_build" / "last-build.json").read_text())["zip"])
    with path.open("rb") as handle:
        assert hashlib.file_digest(handle, "sha256").hexdigest() == path.with_suffix(".zip.sha256").read_text().split()[0]
    qa = ROOT / "qa" / f"portable-{time.time_ns()}" / "máy nhận có dấu"
    qa.mkdir(parents=True)
    with zipfile.ZipFile(path) as package:
        assert package.testzip() is None
        names = package.namelist()
        assert all(Path(name).parts[1] not in {"data", "qa", "tests"} for name in names)
        assert not any(name.endswith((".db", ".db-wal", ".db-shm", ".pyc")) for name in names)
        package.extractall(qa)
    bundle = qa / "LocalLiveStudio"
    python = bundle / "runtime" / "python" / "python.exe"
    ffmpeg = bundle / "runtime" / "ffmpeg" / "bin" / "ffmpeg.exe"
    ffprobe = ffmpeg.with_name("ffprobe.exe")
    env = os.environ.copy()
    env["PATH"] = str(Path(env["SYSTEMROOT"]) / "System32")
    env["PYTHONHOME"] = str(qa / "no-system-python")
    env["PYTHONPATH"] = str(qa / "no-system-packages")
    env["LIVE_FFMPEG"] = str(qa / "no-system-ffmpeg.exe")
    env["LIVE_FFPROBE"] = str(qa / "no-system-ffprobe.exe")
    env["LIVE_DATA_DIR"] = str(qa / "do-not-use-this-data")
    hidden = {"creationflags": subprocess.CREATE_NO_WINDOW}
    checked = subprocess.run([str(python), "-B", "-X", "utf8", str(bundle / "portable_entry.py"),
        "--check", "--verify"], cwd=qa, env=env, capture_output=True, text=True, encoding="utf-8",
        check=True, timeout=30, **hidden)
    print(checked.stdout, flush=True)
    clean_path = subprocess.run([str(python), "-B", "-c",
        "import json,sys;print(json.dumps(sys.path));print(sys.flags.isolated)"],
        cwd=qa, env=env, capture_output=True, text=True, check=True, timeout=10, **hidden)
    assert all(Path(p).resolve().is_relative_to(bundle) for p in json.loads(clean_path.stdout.splitlines()[0]))
    assert clean_path.stdout.splitlines()[1] == "1"
    video = qa / "video kiểm tra.mp4"
    subprocess.run([str(ffmpeg), "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
        "testsrc2=size=320x180:rate=30", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
        "-t", "1", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(video)],
        env=env, capture_output=True, check=True, timeout=15, **hidden)
    group = ProcessGroup()
    server_port = free_port()
    base = f"http://127.0.0.1:{server_port}"
    children = []
    token = ""

    def request(endpoint, method="GET", payload=None, auth=True):
        headers = {"X-Local-Token":token} if auth else {}
        body = None
        if payload is not None:
            body = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        query = urllib.request.Request(base + endpoint, method=method, headers=headers, data=body)
        with urllib.request.urlopen(query, timeout=5) as response:
            raw = response.read()
            return json.loads(raw) if response.headers.get_content_type() == "application/json" else raw

    def launch():
        launcher = bundle / "START_LIVE.bat"
        cmd = Path(env["SYSTEMROOT"]) / "System32" / "cmd.exe"
        command = f'"{cmd}" /d /s /c ""{launcher}" --no-browser --port {server_port}"'
        log = (qa / f"launcher-{len(children)}.log").open("wb")
        try:
            process = subprocess.Popen(command, executable=str(cmd), cwd=qa, env=env,
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, **hidden)
        finally:
            log.close()
        group.add(process)
        children.append(process)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                return request("/api/system", auth=False)
            except (OSError, urllib.error.URLError):
                if process.poll() is not None:
                    raise RuntimeError("Portable launcher exited early")
                time.sleep(.1)
        raise RuntimeError("Portable launcher did not become ready")

    def wait_for(stream_id, statuses):
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            job = next(j for j in request("/api/streams") if j["id"] == stream_id)
            if job["status"] in statuses:
                return job
            time.sleep(.1)
        raise RuntimeError(f"Stream failed to reach expected status: {job}")

    def diagnostics():
        result = subprocess.run([str(python), "-u", "-B", "-X", "utf8", str(bundle / "portable_entry.py"),
            "--diagnose", "--port", str(server_port)], cwd=qa, env=env,
            capture_output=True, text=True, encoding="utf-8", check=True, timeout=30, **hidden)
        assert (bundle / "data" / "diagnostic.txt").is_file()
        return result.stdout

    try:
        info = launch()
        token = info["token"]
        assert request("/api/health", auth=False) == {"status":"ok", "service":"local-live-studio"}
        assert Path(info["tools"]["ffmpeg"]).resolve() == ffmpeg.resolve()
        assert Path(info["tools"]["ffprobe"]).resolve() == ffprobe.resolve()
        assert request("/api/streams") == []
        assert "Đưa video lên sóng" in request("/").decode("utf-8")
        assert request("/static/app.js") and request("/static/style.css")
        assert request("/api/probe?path=" + urllib.parse.quote(str(video)))["has_audio"]
        healthy = diagnostics()
        assert "[OK] Local Live Studio trả về HTTP 200" in healthy
        assert "[OK] Có quyền ghi dữ liệu" in healthy
        assert token not in healthy
        # A second launch must fail clearly while keeping the first server alive.
        conflict = subprocess.run([str(python), "-u", "-B", "-X", "utf8", str(bundle / "portable_entry.py"),
            "--no-browser", "--port", str(server_port)], cwd=qa, env=env,
            capture_output=True, text=True, encoding="utf-8", timeout=15, **hidden)
        assert conflict.returncode == 1
        assert "đang được chương trình khác sử dụng" in conflict.stderr
        assert "Local Live Studio: http://" not in conflict.stdout
        assert "Traceback" in (bundle / "data" / "startup.log").read_text(encoding="utf-8")
        assert request("/api/health", auth=False)["status"] == "ok"
        # Real RTMP receiver and stream: all traffic is loopback, with synthetic keys.
        rtmp_port = free_port()
        key = "synthetic-portable-key"
        server = f"rtmp://127.0.0.1:{rtmp_port}/live"
        received = qa / "received.flv"
        receiver_log = (qa / "receiver.log").open("wb")
        try:
            receiver = subprocess.Popen([str(ffmpeg), "-hide_banner", "-loglevel", "error", "-listen", "1",
                "-i", server + "/" + key, "-c", "copy", "-f", "flv", str(received)], env=env,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=receiver_log, **hidden)
        finally:
            receiver_log.close()
        group.add(receiver)
        children.append(receiver)
        time.sleep(.5)
        payload = dict(name="Portable RTMP test", source_path=str(video), stream_key=key, server_url=server,
            start_at=datetime.now(timezone.utc).isoformat(), duration=3, bitrate=1000, encoder="libx264",
            resolution="original", auto_retry=False)
        stream_id = request("/api/streams?start_now=true", "POST", payload)["id"]
        done = wait_for(stream_id, {"completed", "failed"})
        assert done["status"] == "completed", done["last_error"]
        assert done["metrics"]["elapsed"] > 2
        receiver.wait(timeout=5)
        assert received.stat().st_size > 10000
        result = subprocess.run([str(ffprobe), "-v", "error", "-show_streams", "-show_format", "-of", "json",
            str(received)], env=env, capture_output=True, text=True, check=True, timeout=10, **hidden)
        media = json.loads(result.stdout)
        assert {s["codec_name"] for s in media["streams"]} == {"h264", "aac"}
        assert float(media["format"]["duration"]) > 2.5
        assert key not in json.dumps(request("/api/streams"))
        assert key not in json.dumps(request(f"/api/streams/{stream_id}/logs"))
        request(f"/api/streams/{stream_id}", "DELETE")
        assert video.exists()
        # Persist a cancelled future schedule and retain a DPAPI key when editing.
        payload.update(name="Portable persisted schedule", mode="continuous",
            start_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
        stream_id = request("/api/streams", "POST", payload)["id"]
        payload.update(name="Portable edited schedule", stream_key="")
        request(f"/api/streams/{stream_id}", "PUT", payload)
        request(f"/api/streams/{stream_id}/stop", "POST")
        assert wait_for(stream_id, {"stopped"})["name"] == "Portable edited schedule"
        assert (bundle / "data" / "streams.db").is_file()
        assert not Path(env["LIVE_DATA_DIR"]).exists()
        assert key.encode() not in (bundle / "data" / "streams.db").read_bytes()
        group.close()
        for child in children:
            child.wait(timeout=5)
        unavailable = diagnostics()
        assert "[LỖI] Không kết nối được tới cổng" in unavailable
        assert key not in unavailable and token not in unavailable
        # New launch with the same bundle keeps the local schedule.
        group = ProcessGroup()
        token = launch()["token"]
        persisted = wait_for(stream_id, {"stopped"})
        assert persisted["name"] == "Portable edited schedule"
        assert key not in json.dumps(persisted)
        report = {"result":"passed", "zip":path.name, "zip_bytes":path.stat().st_size,
            "checks":["ZIP CRC", "no personal data", "relocation with Unicode/spaces", "file SHA-256 manifest",
                "isolated Python without system packages", "PATH without Python/FFmpeg", "bundled dependencies",
                "START_LIVE.bat launch", "UI assets", "local video probe", "RTMP transfer", "H.264/AAC",
                "finite completion", "hidden keys/logs", "edit/cancel/delete", "local DPAPI storage", "restart persistence",
                "HTTP readiness endpoint", "TCP/HTTP diagnostics while running", "data write permissions",
                "occupied port without false readiness", "startup traceback saved", "connection refusal diagnostics"],
            "source_machine_only":True, "target_machine_live_test":False}
        report_path = path.with_suffix(".verification.json")
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False), flush=True)
    finally:
        group.close()
        for child in children:
            child.wait(timeout=5)
        print("Owned portable QA server and receiver stopped.", flush=True)


if __name__ == "__main__":
    main()
