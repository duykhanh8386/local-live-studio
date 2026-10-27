"""Real FFmpeg tests use generated media and local files/loopback RTMP only."""
from __future__ import annotations

import json
import ctypes
import os
import socket
import subprocess
import tempfile
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from live.api import create_app
from live.media import HIDDEN_PROCESS, destination, executable, probe_video, redact
from live.storage import SecretBox, Store
from live.engine import Engine

ROOT = Path(__file__).resolve().parents[1]


class LocalLiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        qa = ROOT / "qa"
        qa.mkdir(exist_ok=True)
        cls.temp = tempfile.TemporaryDirectory(prefix="tests-", dir=qa)
        cls.root = Path(cls.temp.name)
        cls.video = cls.root / "video có dấu.mp4"
        cls.silent = cls.root / "silent.mp4"
        for path, audio in ((cls.video, True), (cls.silent, False)):
            cmd = [executable("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y",
                   "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30"]
            if audio:
                cmd += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000"]
            cmd += ["-t", "1", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "30"]
            if audio:
                cmd += ["-c:a", "aac"]
            subprocess.run(cmd + [str(path)], check=True, capture_output=True, timeout=15, **HIDDEN_PROCESS)

    @classmethod
    def tearDownClass(cls):
        assert cls.root.resolve().is_relative_to((ROOT / "qa").resolve())
        cls.temp.cleanup()

    def setUp(self):
        self.folder = self.root / self._testMethodName
        self.app = create_app(self.folder, start_scheduler=False, max_streams=2,
            output_factory=lambda job, key: str(self.folder / (job["id"] + ".flv")))
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.client.headers["X-Local-Token"] = self.app.state.token
        self.engine = self.app.state.engine

    def tearDown(self):
        self.client.__exit__(None, None, None)

    def payload(self, **changes):
        data = dict(name="Luồng thử local", source_path=str(self.video),
            stream_key="synthetic-test-key-1234", mode="public",
            start_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            duration=3, bitrate=1000, encoder="libx264", resolution="original", auto_retry=False)
        data.update(changes)
        return data

    def add(self, **changes):
        response = self.client.post("/api/streams", json=self.payload(**changes))
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["id"]

    def job(self, stream_id):
        return next(j for j in self.client.get("/api/streams").json() if j["id"] == stream_id)

    def wait_status(self, stream_id, statuses, timeout=12):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            job = self.job(stream_id)
            if job["status"] in statuses:
                return job
            time.sleep(.1)
        self.fail(f"Timed out: {self.job(stream_id)}")

    def test_crud_timezone_persistence_and_key_privacy(self):
        stream_id = self.add(start_at="2026-10-11T20:00:00")
        job = self.job(stream_id)
        self.assertEqual(job["start_at"], datetime(2026, 10, 11, 13, tzinfo=timezone.utc).timestamp())
        self.assertNotIn("synthetic-test-key", json.dumps(job))
        self.assertNotIn("key_sealed", job)
        self.assertNotIn("target_id", job)
        stored = self.engine.store.get(stream_id)
        self.assertNotIn("synthetic-test-key", stored["key_sealed"])
        self.assertEqual(self.engine.secrets.open(stored["key_sealed"]), "synthetic-test-key-1234")
        response = self.client.put(f"/api/streams/{stream_id}",
            json=self.payload(name="Tên mới", stream_key=""))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.job(stream_id)["name"], "Tên mới")
        other = Store(self.folder)
        self.assertEqual(other.get(stream_id)["name"], "Tên mới")
        other.close()
        self.assertEqual(self.client.delete(f"/api/streams/{stream_id}").status_code, 200)
        self.assertTrue(self.video.exists())
        self.assertEqual(self.client.get(f"/api/streams/{stream_id}/logs").status_code, 404)

    def test_archive_options_validate_and_enqueue_after_real_ffmpeg_finishes(self):
        missing = self.client.post("/api/streams", json=self.payload(trim_after_live=True))
        self.assertEqual(missing.status_code, 400)
        studio = Mock()
        studio.inspect.return_value = {"kind": "live"}
        self.app.state.archives.factory = lambda: studio
        stream_id = self.add(duration=1, youtube_video_id="https://youtu.be/Abc123_-XYZ", trim_after_live=True)
        self.assertEqual(self.job(stream_id)["youtube_video_id"], "Abc123_-XYZ")
        self.assertFalse(self.client.get("/api/archives").json()["tasks"])
        self.engine.start(stream_id)
        self.wait_status(stream_id, {"completed"})
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not self.app.state.archives.snapshot()["tasks"]:
            time.sleep(.05)
        tasks = self.client.get("/api/archives").json()["tasks"]
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["stream_id"], stream_id)
        self.assertTrue(tasks[0]["publish_after"])
        studio.trim.assert_not_called()

    def test_local_api_protection_and_validation(self):
        self.assertEqual(self.client.get("/api/streams", headers={"X-Local-Token": "bad"}).status_code, 403)
        self.assertEqual(self.client.get("/api/system", headers={"Origin": "https://evil.example"}).status_code, 403)
        self.assertEqual(self.client.get("/api/system", headers={"Host": "evil.example"}).status_code, 400)
        self.assertEqual(self.client.get("/api/system", headers={"Origin": "http://testserver"}).status_code, 200)
        key = "PRIVATE-test-" + "x" * 600
        error = self.client.post("/api/streams", json=self.payload(stream_key=key))
        self.assertEqual(error.status_code, 422)
        self.assertNotIn(key, error.text)
        self.assertEqual(self.client.post("/api/streams", json=self.payload(server_url="file:///tmp")).status_code, 400)
        self.assertEqual(self.client.post("/api/streams", json=self.payload(name="  ")).status_code, 400)
        self.assertEqual(self.client.get("/api/probe", params={"path": "\\\\server\\share\\test.mp4"}).status_code, 400)
        self.assertEqual(self.client.get("/api/probe", params={"path": str(self.root / "missing.mp4")}).status_code, 400)

    def test_folder_browser_and_probe_errors(self):
        files = self.client.get("/api/files", params={"path": str(self.root)}).json()
        self.assertIn(self.video.name, [v["name"] for v in files["videos"]])
        self.assertEqual(self.client.get("/api/probe", params={"path": str(self.video)}).json()["width"], 320)
        with patch("live.media.subprocess.run", side_effect=subprocess.TimeoutExpired("ffprobe", 15)):
            self.assertEqual(self.client.get("/api/probe", params={"path": str(self.video)}).status_code, 400)

    def test_real_finite_encoding_loop_and_retained_metrics(self):
        stream_id = self.add()
        self.assertEqual(self.client.post(f"/api/streams/{stream_id}/start").status_code, 200)
        done = self.wait_status(stream_id, {"completed", "failed"})
        self.assertEqual(done["status"], "completed", done["last_error"])
        self.assertGreater(done["metrics"]["elapsed"], 2.5)
        output = self.probe_output(stream_id)
        self.assertGreater(float(output["format"]["duration"]), 2.5)
        self.assertEqual(output["streams"][0]["codec_name"], "h264")
        self.assertNotIn("synthetic-test-key", self.client.get(f"/api/streams/{stream_id}/logs").text)
        self.assertEqual(self.client.delete("/api/completed").json()["removed"], 1)

    def probe_output(self, stream_id):
        output = self.folder / (stream_id + ".flv")
        result = subprocess.run([executable("ffprobe"), "-v", "error", "-show_streams",
            "-show_format", "-of", "json", str(output)], capture_output=True, text=True,
            check=True, timeout=10, **HIDDEN_PROCESS)
        return json.loads(result.stdout)

    def test_real_copy_silent_video_adds_audio(self):
        stream_id = self.add(source_path=str(self.silent), encoder="copy")
        self.client.post(f"/api/streams/{stream_id}/start")
        done = self.wait_status(stream_id, {"completed", "failed"})
        self.assertEqual(done["status"], "completed", done["last_error"])
        streams = self.probe_output(stream_id)["streams"]
        self.assertEqual({s["codec_type"] for s in streams}, {"audio", "video"})
        self.assertEqual(next(s for s in streams if s["codec_type"] == "audio")["codec_name"], "aac")

    def test_intel_selection_persists_and_unknown_encoder_is_rejected(self):
        stream_id = self.add(encoder="h264_qsv")
        self.assertEqual(self.job(stream_id)["encoder"], "h264_qsv")
        response = self.client.post("/api/streams", json=self.payload(encoder="unsupported"))
        self.assertEqual(response.status_code, 400)

    @unittest.skipUnless(os.environ.get("LIVE_TEST_INTEL_QSV") == "1", "Requires Intel Quick Sync hardware")
    def test_real_intel_encoding_with_audio_and_requested_resolution(self):
        stream_id = self.add(encoder="h264_qsv", resolution="1080p", bitrate=12000)
        self.client.post(f"/api/streams/{stream_id}/start")
        done = self.wait_status(stream_id, {"completed", "failed"})
        self.assertEqual(done["status"], "completed", done["last_error"])
        output = self.probe_output(stream_id)
        video = next(s for s in output["streams"] if s["codec_type"] == "video")
        audio = next(s for s in output["streams"] if s["codec_type"] == "audio")
        self.assertEqual((video["codec_name"], video["height"], video["r_frame_rate"]), ("h264", 1080, "30/1"))
        self.assertEqual(audio["codec_name"], "aac")
        self.assertGreater(float(output["format"]["duration"]), 2.5)

    def test_continuous_stop_conflicts_capacity_and_owned_processes(self):
        unrelated = subprocess.Popen([executable("ffmpeg"), "-hide_banner", "-loglevel", "error",
            "-re", "-f", "lavfi", "-i", "anullsrc", "-f", "null", "-"],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **HIDDEN_PROCESS)
        try:
            first = self.add(mode="continuous")
            duplicate = self.add(mode="continuous")
            second = self.add(mode="continuous", stream_key="other-test-key")
            third = self.add(mode="continuous", stream_key="third-test-key")
            self.client.post(f"/api/streams/{first}/start")
            self.wait_status(first, {"streaming"})
            self.assertEqual(self.client.post(f"/api/streams/{duplicate}/start").status_code, 400)
            self.assertEqual(self.client.delete(f"/api/streams/{first}").status_code, 400)
            self.assertEqual(self.client.put(f"/api/streams/{first}", json=self.payload()).status_code, 400)
            self.client.post(f"/api/streams/{second}/start")
            self.assertEqual(self.client.post(f"/api/streams/{third}/start").status_code, 400)
            process = self.engine.runs[first].process
            self.client.post(f"/api/streams/{first}/stop")
            self.wait_status(first, {"stopped"})
            self.assertIsNotNone(process.poll())
            self.assertIsNone(unrelated.poll())
            self.client.post(f"/api/streams/{second}/stop")
            self.wait_status(second, {"stopped"})
        finally:
            unrelated.terminate()
            unrelated.wait(timeout=5)
            unrelated.stdin.close()

    def test_schedule_missed_cancelled_and_queued(self):
        cancelled = self.add()
        self.client.post(f"/api/streams/{cancelled}/stop")
        now = time.time()
        missed = self.add(start_at=datetime.fromtimestamp(now - 30, timezone.utc).isoformat())
        queued = self.add(mode="continuous", start_at=datetime.fromtimestamp(now - 1, timezone.utc).isoformat())
        with patch.object(self.engine, "start", side_effect=ValueError("capacity")):
            self.engine.tick(now)
        self.assertEqual(self.job(cancelled)["status"], "stopped")
        self.assertEqual(self.job(missed)["status"], "missed")
        self.assertEqual(self.job(queued)["status"], "queued")
        with patch.object(self.engine, "start") as start:
            self.engine.tick(now)
            start.assert_called_once_with(queued, scheduled=True)

    def test_real_scheduler_starts_future_job(self):
        stream_id = self.add(start_at=(datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat())
        self.engine.open(scheduler=True)
        done = self.wait_status(stream_id, {"completed", "failed"})
        self.assertEqual(done["status"], "completed", done["last_error"])

    def test_retry_and_stop_during_backoff(self):
        self.engine.output_factory = lambda job, key: "rtmp://127.0.0.1:1/live/" + key
        stream_id = self.add(mode="continuous", auto_retry=True)
        self.client.post(f"/api/streams/{stream_id}/start")
        retry = self.wait_status(stream_id, {"retrying", "failed"})
        self.assertEqual(retry["status"], "retrying")
        self.assertEqual(retry["retries"], 1)
        self.assertNotIn("synthetic-test-key", retry["last_error"])
        self.client.post(f"/api/streams/{stream_id}/stop")
        self.wait_status(stream_id, {"stopped"}, timeout=3)

    def test_start_now_is_atomic_and_failure_does_not_schedule(self):
        stream_id = self.add(mode="continuous")
        self.client.post(f"/api/streams/{stream_id}/start")
        response = self.client.post("/api/streams?start_now=true", json=self.payload())
        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.json()["error"])
        self.assertEqual(self.job(response.json()["id"])["status"], "failed")
        self.client.post(f"/api/streams/{stream_id}/stop")
        self.wait_status(stream_id, {"stopped"})

    def test_restart_marks_active_job_interrupted(self):
        stream_id = self.add()
        self.engine.store.update(stream_id, status="streaming")
        other = Store(self.folder)
        engine = Engine(other)
        engine.open(scheduler=False)
        self.assertEqual(engine.snapshot()[0]["status"], "interrupted")
        engine.shutdown()

    def test_shutdown_stops_live_and_persists_interrupted(self):
        stream_id = self.add(mode="continuous")
        folder = self.folder / "shutdown"
        store = Store(folder)
        store.save(self.engine.store.get(stream_id))
        engine = Engine(store, output_factory=lambda job, key: str(folder / "shutdown.flv"))
        engine.open(scheduler=False)
        try:
            engine.start(stream_id)
            end = time.monotonic() + 8
            while engine.snapshot()[0]["status"] != "streaming" and time.monotonic() < end:
                time.sleep(.1)
            self.assertEqual(engine.snapshot()[0]["status"], "streaming")
            process = engine.runs[stream_id].process
        finally:
            engine.shutdown()
        self.assertIsNotNone(process.poll())
        reopened = Store(folder)
        self.assertEqual(reopened.get(stream_id)["status"], "interrupted")
        reopened.close()

    @unittest.skipUnless(os.name == "nt", "Windows job-object lifecycle")
    def test_abrupt_host_exit_stops_owned_process(self):
        from ctypes import wintypes
        pid_file = self.folder / "child.pid"
        code = """import pathlib, subprocess, sys, time
from live.media import executable, HIDDEN_PROCESS
from live.processes import ProcessGroup
group = ProcessGroup()
child = subprocess.Popen([executable('ffmpeg'), '-hide_banner', '-loglevel', 'error',
    '-re', '-f', 'lavfi', '-i', 'anullsrc', '-f', 'null', '-'],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **HIDDEN_PROCESS)
group.add(child)
pathlib.Path(sys.argv[1]).write_text(str(child.pid))
time.sleep(30)
"""
        host = subprocess.Popen([sys.executable, "-c", code, str(pid_file)], cwd=ROOT,
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, **HIDDEN_PROCESS)
        child_handle = None
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        try:
            end = time.monotonic() + 6
            while not pid_file.exists() and time.monotonic() < end and host.poll() is None:
                time.sleep(.1)
            self.assertTrue(pid_file.exists(), "Helper failed to start contained FFmpeg")
            child_handle = kernel.OpenProcess(0x00100000, False, int(pid_file.read_text()))
            self.assertTrue(child_handle)
            self.assertEqual(kernel.WaitForSingleObject(child_handle, 0), 258)
            host.terminate()
            host.wait(timeout=5)
            self.assertEqual(kernel.WaitForSingleObject(child_handle, 5000), 0)
        finally:
            if host.poll() is None:
                host.terminate()
                host.wait(timeout=5)
            if child_handle:
                kernel.CloseHandle(child_handle)
            host.stderr.close()

    def test_real_rtmp_transfer_to_loopback_receiver(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        key = "loopback-key"
        server = f"rtmp://127.0.0.1:{port}/live"
        received = self.folder / "received.flv"
        receiver = subprocess.Popen([executable("ffmpeg"), "-hide_banner", "-loglevel", "error",
            "-listen", "1", "-i", destination(server, key), "-c", "copy", "-f", "flv", str(received)],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, **HIDDEN_PROCESS)
        try:
            time.sleep(.5)
            self.engine.output_factory = lambda job, key: destination(job["server_url"], key)
            stream_id = self.add(server_url=server, stream_key=key, duration=4)
            self.client.post(f"/api/streams/{stream_id}/start")
            done = self.wait_status(stream_id, {"completed", "failed"})
            self.assertEqual(done["status"], "completed", done["last_error"])
            receiver.wait(timeout=5)
            self.assertTrue(received.exists())
            self.assertGreater(received.stat().st_size, 10000)
        finally:
            if receiver.poll() is None:
                receiver.terminate()
                receiver.wait(timeout=5)
            receiver.stderr.close()

    def test_redaction_and_dpapi(self):
        key = "private/test/key"
        self.assertEqual(SecretBox().open(SecretBox().seal(key)), key)
        safe = redact("failed rtmps://example.com/live/private%2Ftest%2Fkey " + key, key)
        self.assertNotIn(key, safe)
        self.assertNotIn("private%2Ftest", safe)


if __name__ == "__main__":
    unittest.main(verbosity=2)
