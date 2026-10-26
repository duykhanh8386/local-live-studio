from __future__ import annotations

import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from .media import HIDDEN_PROCESS, build_command, destination, probe_video, redact
from .storage import SecretBox, Store
from .processes import ProcessGroup

ACTIVE = {"starting", "streaming", "retrying", "stopping"}


@dataclass
class Run:
    stop: threading.Event = field(default_factory=threading.Event)
    process: subprocess.Popen | None = None
    thread: threading.Thread | None = None
    shutdown: bool = False
    metrics: dict = field(default_factory=dict)


class Engine:
    def __init__(self, store: Store, max_streams: int = 4,
                 output_factory: Callable[[dict, str], str] | None = None):
        self.store = store
        self.secrets = SecretBox()
        self.max_streams = max_streams
        self.output_factory = output_factory or (lambda job, key: destination(job["server_url"], key))
        self.lock = threading.RLock()
        self.runs: dict[str, Run] = {}
        self.closed = threading.Event()
        self.scheduler: threading.Thread | None = None
        self.processes = ProcessGroup()
        self.on_finished = None

    def open(self, scheduler: bool = True):
        for job in self.store.list():
            if job["status"] in ACTIVE:
                self.store.update(job["id"], status="interrupted", ended_at=time.time(),
                                  last_error="Phiên phát trước đã ngắt. Bấm Chạy để tiếp tục.")
        if scheduler:
            self.scheduler = threading.Thread(target=self._schedule, daemon=True, name="live-scheduler")
            self.scheduler.start()

    def _schedule(self):
        while not self.closed.is_set():
            self.tick()
            self.closed.wait(1)

    def tick(self, now: float | None = None):
        now = time.time() if now is None else now
        for snapshot in sorted(self.store.list(), key=lambda j: j["start_at"]):
            with self.lock:
                try:
                    job = self.store.get(snapshot["id"])
                except KeyError:
                    continue
                if job["status"] not in {"scheduled", "queued"} or job["start_at"] > now:
                    continue
                if job["mode"] == "public" and job["start_at"] + job["duration"] <= now:
                    self.store.update(job["id"], status="missed", ended_at=now,
                                      last_error="Đã qua toàn bộ thời gian đặt lịch khi máy phát chưa chạy.")
                    continue
                try:
                    self.start(job["id"], scheduled=True)
                except ValueError as exc:
                    self.store.update(job["id"], status="queued", last_error=str(exc))
                except Exception:
                    self.store.update(job["id"], status="failed", last_error="Không thể khởi động lịch phát.")

    def start(self, stream_id: str, *, scheduled: bool = False):
        with self.lock:
            if self.closed.is_set():
                raise ValueError("Máy phát đang đóng.")
            job = self.store.get(stream_id)
            if stream_id in self.runs:
                raise ValueError("Luồng này đang chạy hoặc đang dừng.")
            if len(self.runs) >= self.max_streams:
                raise ValueError(f"Đang chờ: máy đã chạy đủ {self.max_streams} luồng đồng thời.")
            for active_id in self.runs:
                if self.store.get(active_id)["target_id"] == job["target_id"]:
                    raise ValueError("Stream key này đang được một luồng khác sử dụng.")
            now = time.time()
            deadline = None if job["mode"] == "continuous" else (
                job["start_at"] + job["duration"] if scheduled else now + job["duration"]
            )
            run = Run(metrics={"elapsed": 0, "fps": 0, "speed": 0, "bitrate": 0,
                               "bytes_sent": 0, "frame": 0, "deadline": deadline})
            self.runs[stream_id] = run
            self.store.update(stream_id, status="starting", started_at=now, ended_at=None,
                              last_error="", retries=0, metrics={})
            run.thread = threading.Thread(target=self._worker, args=(job, run, deadline),
                                          daemon=True, name=f"live-{stream_id}")
            run.thread.start()

    def stop(self, stream_id: str):
        with self.lock:
            job = self.store.get(stream_id)
            run = self.runs.get(stream_id)
            if run:
                run.stop.set()
                self.store.update(stream_id, status="stopping")
            elif job["status"] in {"scheduled", "queued"}:
                self.store.update(stream_id, status="stopped", ended_at=time.time())

    def edit(self, stream_id: str, replacement: dict):
        with self.lock:
            if stream_id in self.runs:
                raise ValueError("Dừng luồng trước khi sửa lịch.")
            self.store.get(stream_id)
            return self.store.save(replacement)

    def delete(self, stream_id: str):
        with self.lock:
            if stream_id in self.runs:
                raise ValueError("Dừng luồng trước khi xóa lịch.")
            self.store.delete(stream_id)

    def snapshot(self) -> list[dict]:
        jobs = self.store.list()
        with self.lock:
            for job in jobs:
                job.pop("key_sealed", None)
                job.pop("target_id", None)
                run = self.runs.get(job["id"])
                job["metrics"] = dict(run.metrics) if run else job.get("metrics", {})
        return jobs

    @staticmethod
    def _terminate(process: subprocess.Popen):
        if process.poll() is not None:
            return
        try:
            process.stdin.write("q\n")
            process.stdin.flush()
            process.wait(timeout=2)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)

    def _worker(self, job: dict, run: Run, deadline: float | None):
        stream_id = job["id"]
        final_status = "failed"
        key = ""
        try:
            key = self.secrets.open(job["key_sealed"])
            media = probe_video(job["source_path"])
            output = self.output_factory(job, key)
            attempt = 0
            elapsed_before = 0
            bytes_before = 0
            while not run.stop.is_set():
                remaining = None if deadline is None else deadline - time.time()
                if remaining is not None and remaining <= 0:
                    final_status = "completed"
                    break
                command = build_command(job, media, output, remaining)
                self.store.log(stream_id, f"Bắt đầu phát {media['name']} · lần kết nối {attempt + 1}.")
                run.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", bufsize=1,
                    **HIDDEN_PROCESS)
                process = run.process
                self.processes.add(process)
                last_progress = [time.monotonic()]
                errors = []

                def read_errors():
                    for line in process.stderr:
                        safe = redact(line.strip(), key)
                        if safe:
                            errors.append(safe)
                            del errors[:-8]
                            self.store.log(stream_id, safe)

                def read_progress():
                    values = {}
                    marked_live = False
                    for line in process.stdout:
                        name, _, value = line.strip().partition("=")
                        values[name] = value
                        if name != "progress":
                            continue
                        last_progress[0] = time.monotonic()
                        try:
                            elapsed = float(values.get("out_time_us", 0)) / 1_000_000
                            size = max(0, int(values.get("total_size", 0)))
                            frame = int(values.get("frame", 0))
                            bitrate = re.sub(r"[^\d.]", "", values.get("bitrate", "0"))
                            metrics = {"elapsed": elapsed_before + max(0, elapsed), "bytes_sent": bytes_before + size,
                                       "fps": float(values.get("fps", 0)), "frame": frame,
                                       "speed": float(values.get("speed", "0").rstrip("x")),
                                       "bitrate": float(bitrate or 0)}
                        except ValueError:
                            continue
                        with self.lock:
                            run.metrics.update(metrics)
                        if frame > 0 and not marked_live and not run.stop.is_set():
                            self.store.update(stream_id, status="streaming", last_error="")
                            marked_live = True

                readers = [threading.Thread(target=read_errors, daemon=True),
                           threading.Thread(target=read_progress, daemon=True)]
                for reader in readers:
                    reader.start()
                completed = False
                stalled = False
                while process.poll() is None:
                    if run.stop.is_set():
                        self._terminate(process)
                        break
                    if deadline is not None and time.time() >= deadline + 1:
                        completed = True
                        self._terminate(process)
                        break
                    if time.monotonic() - last_progress[0] > 60:
                        stalled = True
                        self._terminate(process)
                        break
                    run.stop.wait(0.2)
                for reader in readers:
                    reader.join(timeout=4)
                for pipe in (process.stdin, process.stdout, process.stderr):
                    pipe.close()
                if run.stop.is_set():
                    break
                if completed or (process.returncode == 0 and deadline is not None):
                    final_status = "completed"
                    break
                error = "Không nhận được tiến độ từ FFmpeg trong 60 giây." if stalled else (
                    errors[-1] if errors else f"FFmpeg ngắt kết nối (mã {process.returncode})."
                )
                if not job["auto_retry"] or attempt >= 5:
                    raise ValueError(error)
                elapsed_before = run.metrics.get("elapsed", 0)
                bytes_before = run.metrics.get("bytes_sent", 0)
                attempt += 1
                delay = min(30, 2 ** attempt)
                self.store.update(stream_id, status="retrying", retries=attempt, last_error=error)
                self.store.log(stream_id, f"Thử kết nối lại sau {delay} giây: {error}")
                if run.stop.wait(delay):
                    break
        except Exception as exc:
            message = redact(str(exc), key)
            self.store.update(stream_id, last_error=message)
            self.store.log(stream_id, message)
        finally:
            if run.process is not None and run.process.poll() is None:
                self._terminate(run.process)
            if run.stop.is_set():
                final_status = "interrupted" if run.shutdown else "stopped"
            with self.lock:
                self.store.update(stream_id, status=final_status, ended_at=time.time(),
                                  metrics=dict(run.metrics))
                self.store.log(stream_id, f"Kết thúc phiên phát: {final_status}.")
            if self.on_finished:
                try:
                    self.on_finished(job, final_status)
                except Exception:
                    self.store.log(stream_id, "Không tạo được tác vụ bản lưu. Mở phần Xử lý bản lưu để thêm Video ID.")
            with self.lock:
                self.runs.pop(stream_id, None)

    def shutdown(self):
        self.closed.set()
        if self.scheduler:
            self.scheduler.join(timeout=3)
        with self.lock:
            runs = list(self.runs.values())
            for run in runs:
                run.shutdown = True
                run.stop.set()
        for run in runs:
            if run.thread:
                # Local ffprobe has a 15s timeout; join before closing its database.
                run.thread.join()
        self.processes.close()
        self.store.close()
