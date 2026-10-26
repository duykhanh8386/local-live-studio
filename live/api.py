from __future__ import annotations

import hashlib
import os
import secrets
import string
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .engine import Engine
from .archives import ArchiveQueue, video_id
from .media import VIDEO_EXTENSIONS, destination, executable, local_path, probe_video
from .storage import Store
from .runtime import data_directory
from .updates import UpdateService

ROOT = Path(__file__).resolve().parent.parent
VIETNAM = timezone(timedelta(hours=7))


class ScheduleInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    source_path: str = Field(min_length=1, max_length=2000)
    stream_key: str = Field(default="", max_length=512)
    server_url: str = "rtmps://a.rtmps.youtube.com:443/live2"
    mode: str = "public"
    start_at: str
    duration: int = Field(default=14400, ge=1, le=604800)
    bitrate: int = Field(default=6000, ge=1000, le=30000)
    encoder: str = "libx264"
    resolution: str = "1080p"
    auto_retry: bool = True
    youtube_video_id: str = Field(default="", max_length=500)
    trim_after_live: bool = False
    publish_after_trim: bool = True


class ArchiveInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    video: str = Field(min_length=1, max_length=500)
    publish_after: bool = True
    accept_permanent_cut: bool = False


def create_app(data_dir: Path | None = None, *, start_scheduler: bool = True,
               max_streams: int = 4, output_factory=None, studio_factory=None) -> FastAPI:
    store = Store(data_dir or data_directory())
    engine = Engine(store, max_streams, output_factory)
    archives = ArchiveQueue(store, studio_factory)
    updater = UpdateService(store.directory)
    engine.on_finished = archives.after_stream
    token = secrets.token_urlsafe(32)

    @asynccontextmanager
    async def lifespan(app):
        engine.open(start_scheduler)
        archives.open()
        for job in store.list():
            archives.after_stream(job, job["status"])
        try:
            yield
        finally:
            updater.shutdown()
            archives.shutdown()
            engine.shutdown()

    app = FastAPI(title="Local Live Studio", lifespan=lifespan)
    app.state.engine = engine
    app.state.archives = archives
    app.state.updater = updater
    app.state.request_exit = None
    app.state.listen_port = 8765
    app.state.token = token
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "testserver"])

    @app.middleware("http")
    async def protect_local(request: Request, call_next):
        # A hostile website must not be able to read files or control local FFmpeg.
        origin = request.headers.get("origin")
        if origin and origin != f"{request.url.scheme}://{request.headers.get('host')}":
            return JSONResponse({"detail": "Chỉ nhận yêu cầu từ web local."}, status_code=403)
        if request.url.path.startswith("/api/") and request.url.path not in {"/api/system", "/api/health"}:
            if request.headers.get("x-local-token") != token:
                return JSONResponse({"detail": "Hãy tải lại trang local."}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        details = "; ".join(f"{'.'.join(map(str, e['loc'][1:]))}: {e['msg']}" for e in exc.errors())
        return JSONResponse({"detail": details}, status_code=422)

    @app.exception_handler(KeyError)
    async def not_found(request, exc):
        return JSONResponse({"detail": "Không tìm thấy lịch phát hoặc tác vụ bản lưu."}, status_code=404)

    @app.exception_handler(ValueError)
    async def bad_request(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    def prepare(payload: ScheduleInput, previous: dict | None = None) -> dict:
        values = payload.model_dump()
        if values["mode"] not in {"public", "continuous"}:
            raise ValueError("Chọn Live theo thời lượng hoặc Live 24/7.")
        if values["encoder"] not in {"libx264", "h264_nvenc", "copy"}:
            raise ValueError("Bộ mã hóa chưa được hỗ trợ.")
        if values["resolution"] not in {"original", "720p", "1080p"}:
            raise ValueError("Độ phân giải không hợp lệ.")
        values["youtube_video_id"] = video_id(values["youtube_video_id"]) if values["youtube_video_id"].strip() else ""
        if values["trim_after_live"] and not values["youtube_video_id"]:
            raise ValueError("Cần Video ID YouTube để tự cắt claim sau live; stream key không xác định được video.")
        try:
            start = datetime.fromisoformat(values["start_at"])
        except ValueError:
            raise ValueError("Thời gian bắt đầu không hợp lệ.")
        if start.tzinfo is None:
            start = start.replace(tzinfo=VIETNAM)
        key = values.pop("stream_key").strip()
        if not key and previous:
            key = engine.secrets.open(previous["key_sealed"])
        values["server_url"] = values["server_url"].strip().rstrip("/")
        destination(values["server_url"], key)
        media = probe_video(values["source_path"])
        if values["encoder"] == "copy" and (
            media["video_codec"] != "h264" or media["audio_codec"] not in {"", "aac", "mp3"}
        ):
            raise ValueError("Copy cần H.264 + AAC/MP3. Chọn CPU để mã hóa video này.")
        values.update(id=previous["id"] if previous else secrets.token_hex(4),
            name=values["name"].strip(), source_path=media["path"], media=media,
            start_at=start.timestamp(), key_sealed=engine.secrets.seal(key),
            key_hint="•••• " + key[-4:] if len(key) > 4 else "••••",
            target_id=hashlib.sha256((values["server_url"].lower() + "/" + key).encode()).hexdigest(),
            status="scheduled", created_at=previous["created_at"] if previous else time.time(),
            started_at=None, ended_at=None, last_error="", retries=0)
        if not values["name"]:
            raise ValueError("Tên luồng không được trống.")
        return values

    @app.get("/api/health")
    def health():
        return {"status": "ok", "service": "local-live-studio", "commit": updater.info.get("commit", "")}

    @app.get("/api/system")
    def system():
        tools = {}
        for name in ("ffmpeg", "ffprobe"):
            try:
                tools[name] = executable(name)
            except ValueError:
                tools[name] = None
        return {"token": token, "tools": tools, "timezone": "Asia/Ho_Chi_Minh",
                "max_streams": engine.max_streams, "now": time.time(),
                "default_server": "rtmps://a.rtmps.youtube.com:443/live2"}

    @app.get("/api/update")
    def update_status():
        return updater.snapshot()

    @app.post("/api/update/check")
    def check_update():
        return updater.start("check")

    @app.post("/api/update/download")
    def download_update():
        return updater.start("download")

    @app.post("/api/update/install")
    def install_update():
        if not app.state.request_exit:
            raise ValueError("Phiên máy chủ này chưa hỗ trợ khởi động lại. Hãy dùng bản EXE.")
        with engine.lock:
            if engine.runs:
                raise ValueError("Dừng các luồng live đang chạy trước khi cài cập nhật.")
            if archives.login_pending or not archives.lock.acquire(blocking=False):
                raise ValueError("Studio đang thao tác hoặc đăng nhập. Chờ bước hiện tại xong rồi cài cập nhật.")
            try:
                updater.launch_installer(port=app.state.listen_port, max_streams=engine.max_streams)
                engine.closed.set()
                archives.closed.set()
            finally:
                archives.lock.release()
        timer = threading.Timer(.5, app.state.request_exit)
        timer.daemon = True
        timer.start()
        return {"ok": True, "message": "Đang khởi động lại để cập nhật."}

    @app.post("/api/app/stop")
    def stop_application():
        if not app.state.request_exit:
            raise ValueError("Đóng cửa sổ chương trình để thoát phiên máy chủ này.")
        timer = threading.Timer(.3, app.state.request_exit)
        timer.daemon = True
        timer.start()
        return {"ok": True}

    @app.get("/api/files")
    def files(path: str = ""):
        if not path:
            roots = ([f"{letter}:\\" for letter in string.ascii_uppercase
                      if Path(f"{letter}:\\").exists()] if os.name == "nt" else [str(Path.home()), "/"])
            return {"path": "", "parent": None, "directories": [{"name": r, "path": r} for r in roots], "videos": []}
        folder = local_path(path)
        if not folder.is_dir():
            raise ValueError("Thư mục không tồn tại.")
        directories, videos = [], []
        try:
            for entry in folder.iterdir():
                try:
                    if entry.is_dir():
                        directories.append({"name": entry.name, "path": str(entry)})
                    elif entry.is_file() and entry.suffix.lower() in VIDEO_EXTENSIONS:
                        videos.append({"name": entry.name, "path": str(entry), "size": entry.stat().st_size})
                except OSError:
                    continue
        except PermissionError:
            raise ValueError("Không có quyền đọc thư mục này.")
        return {"path": str(folder), "parent": str(folder.parent) if folder.parent != folder else "",
                "directories": sorted(directories, key=lambda x: x["name"].casefold()),
                "videos": sorted(videos, key=lambda x: x["name"].casefold())}

    @app.get("/api/probe")
    def probe(path: str):
        return probe_video(path)

    @app.get("/api/streams")
    def streams():
        return engine.snapshot()

    @app.get("/api/archives")
    def archive_tasks():
        return archives.snapshot()

    @app.post("/api/studio/open")
    def open_studio():
        archives.open_login()
        return {"ok": True}

    @app.post("/api/studio/connect")
    def connect_studio():
        archives.finish_login()
        return {"ok": True}

    @app.post("/api/studio/cancel-login")
    def cancel_studio_login():
        archives.abort_login()
        return {"ok": True}

    @app.post("/api/archives", status_code=201)
    def add_archive(payload: ArchiveInput):
        if not payload.accept_permanent_cut:
            raise ValueError("Cần đồng ý cắt vĩnh viễn đoạn video/âm thanh bị claim trước khi tự xử lý.")
        return archives.enqueue(payload.video, payload.publish_after)

    @app.post("/api/archives/{task_id}/retry")
    def retry_archive(task_id: str):
        archives.retry(task_id)
        return {"ok": True}

    @app.post("/api/archives/{task_id}/cancel")
    def cancel_archive(task_id: str):
        archives.cancel(task_id)
        return {"ok": True}

    @app.get("/api/archives/{task_id}/logs")
    def archive_logs(task_id: str):
        store.archive(task_id)
        return store.logs("archive:" + task_id)

    @app.post("/api/streams", status_code=201)
    def add(payload: ScheduleInput, start_now: bool = False):
        record = prepare(payload)
        return persist(record, start_now)

    @app.put("/api/streams/{stream_id}")
    def edit(stream_id: str, payload: ScheduleInput, start_now: bool = False):
        return persist(prepare(payload, store.get(stream_id)), start_now, editing=True)

    def persist(record: dict, start_now: bool, editing: bool = False):
        with engine.lock:
            if start_now:
                record["status"] = "stopped"
            if editing:
                engine.edit(record["id"], record)
            else:
                store.save(record)
            store.log(record["id"], "Đã lưu lịch phát trên máy này.")
            error = ""
            if start_now:
                try:
                    engine.start(record["id"])
                except ValueError as exc:
                    error = str(exc)
                    store.update(record["id"], status="failed", last_error=error)
            return {"id": record["id"], "error": error}

    @app.post("/api/streams/{stream_id}/start")
    def start(stream_id: str):
        engine.start(stream_id)
        return {"ok": True}

    @app.post("/api/streams/{stream_id}/stop")
    def stop(stream_id: str):
        engine.stop(stream_id)
        return {"ok": True}

    @app.delete("/api/streams/{stream_id}")
    def delete(stream_id: str):
        engine.delete(stream_id)
        return {"ok": True}

    @app.get("/api/streams/{stream_id}/logs")
    def logs(stream_id: str):
        store.get(stream_id)
        return store.logs(stream_id)

    @app.delete("/api/completed")
    def clear_completed():
        removed = 0
        with engine.lock:
            for job in store.list():
                if job["status"] in {"completed", "missed"} and job["id"] not in engine.runs:
                    engine.delete(job["id"])
                    removed += 1
        return {"removed": removed}

    @app.get("/")
    def index():
        return FileResponse(ROOT / "static" / "index.html")

    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
    return app
