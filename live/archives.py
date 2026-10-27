"""Durable post-live queue. Browser actions are confined to one worker thread."""
from __future__ import annotations

import re
import secrets
import threading
import time
from urllib.parse import parse_qs, urlparse

from .studio import StudioBrowser, StudioIssue
from .tuat_session import load_channel_session

TERMINAL = {"completed", "needs_login", "needs_review", "failed", "cancelled"}


def video_id(value: str) -> str:
    value = value.strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", value):
        return value
    parsed = urlparse(value)
    if parsed.scheme not in {"https", "http"}:
        raise ValueError("Nhập Video ID 11 ký tự hoặc đường dẫn video YouTube.")
    host = (parsed.hostname or "").lower()
    if host == "youtu.be":
        candidate = parsed.path.strip("/")
    elif host in {"youtube.com", "www.youtube.com", "m.youtube.com", "studio.youtube.com"}:
        parts = parsed.path.strip("/").split("/")
        candidate = (parse_qs(parsed.query).get("v", [""])[0] if parsed.path == "/watch" else
                     parts[1] if len(parts) >= 2 and parts[0] in {"video", "live", "shorts"} else "")
    else:
        candidate = ""
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", candidate):
        raise ValueError("Đường dẫn chưa có Video ID YouTube hợp lệ.")
    return candidate


class ArchiveQueue:
    def __init__(self, store, browser_factory=None, interval=60):
        self.store = store
        saved = store.setting("studio_session")
        self.session = {"connected": bool(saved.get("connected")), "background": bool(saved.get("background")),
                        "channel_id": str(saved.get("channel_id") or ""), "verified_at": saved.get("verified_at")}
        self.factory = browser_factory or (lambda: StudioBrowser(store.directory / "studio-profile",
            background=self.session["background"], channel_id=self.session["channel_id"]))
        self.browser = None
        self.interval = interval
        self.lock = threading.RLock()
        self.closed = threading.Event()
        self.wake = threading.Event()
        self.login = threading.Event()
        self.finish = threading.Event()
        self.cancel_login = threading.Event()
        self.login_pending = False
        self.auth_busy = False
        self.auth_source = ""
        self.auth_lock = threading.Lock()
        self.auth_command = None
        self.expected_login_channel = ""
        self.thread = None
        self.browser_message = ("Đã lưu phiên Studio. Sẽ kiểm tra hiệu lực khi chạy tác vụ nền."
                                if self.session["connected"] else "Chưa kết nối tài khoản YouTube.")

    def open(self):
        # A confirmed trim is never resubmitted blindly after a crash.
        with self.lock:
            for task in self.store.archives():
                if task["status"] not in TERMINAL:
                    task["next_at"] = 0
                    self.store.save_archive(task)
        self.thread = threading.Thread(target=self._run, daemon=True, name="studio-archive-worker")
        self.thread.start()

    def enqueue(self, value: str, publish_after=True, stream_id=None) -> dict:
        identifier = video_id(value)
        with self.lock:
            existing = next((t for t in self.store.archives() if t["video_id"] == identifier), None)
            if existing:
                # Keep the original consent/visibility setting; retries are explicit.
                return {**existing, "existing": True}
            now = time.time()
            task = dict(id=secrets.token_hex(8), video_id=identifier, stream_id=stream_id,
                        publish_after=publish_after, status="queued", message="Chờ kiểm tra bản lưu trong Studio.",
                        created_at=now, updated_at=now, next_at=0, deadline=now + 86400,
                        cuts=0, pending_claim=None, processing_seen=False, clean_checks=0)
            self.store.save_archive(task)
            self.store.log("archive:" + task["id"], task["message"])
            self.wake.set()
            return task

    def after_stream(self, job: dict, status: str):
        if not self.closed.is_set() and status in {"completed", "stopped"} and job.get("trim_after_live"):
            self.enqueue(job["youtube_video_id"], job.get("publish_after_trim", True), job["id"])

    def open_login(self, *, source="manual"):
        self.login_pending = True
        self.auth_busy = True
        self.auth_source = source
        self.browser_message = "Đang mở Studio. Đăng nhập và chọn đúng kênh trong cửa sổ mới."
        self.login.set()
        self.wake.set()

    def start_login(self, email, password):
        if not email or "@" not in email or not password:
            raise ValueError("Nhập Email và mật khẩu tài khoản YouTube.")
        self._queue_auth(("credentials", email, password))

    def import_tuat_channel(self, channel_id):
        if not re.fullmatch(r"UC[A-Za-z0-9_-]{22}", channel_id):
            raise ValueError("Chọn một kênh YouTube hợp lệ từ tool Tuất.")
        self._queue_auth(("tuat", channel_id, ""))

    def _queue_auth(self, command):
        with self.auth_lock:
            if self.closed.is_set():
                raise ValueError("Tool đang đóng.")
            if self.login_pending:
                raise ValueError("Đang đăng nhập. Hoàn tất hoặc bấm Hủy đăng nhập trước khi đổi cách kết nối.")
            self.auth_command = command
            self.open_login(source=command[0])

    def finish_login(self):
        if not self.login_pending:
            raise ValueError("Bắt đầu đăng nhập YouTube trước khi lưu phiên.")
        self.browser_message = "Đang xác minh phiên và chuyển sang chạy nền…"
        self.auth_busy = True
        self.finish.set()
        self.wake.set()

    def abort_login(self):
        with self.auth_lock:
            self.auth_command = None
        self.cancel_login.set()
        self.wake.set()

    def snapshot(self):
        return {"browser_message": self.browser_message, "tasks": self.store.archives(),
                "session": dict(self.session), "login_pending": self.login_pending,
                "auth_busy": self.auth_busy, "auth_source": self.auth_source}

    def cancel(self, task_id):
        # A submitted edit belongs to YouTube; this only stops further local actions.
        with self.lock:
            task = self.store.archive(task_id)
            self._update(task, "cancelled", "Đã dừng tự động. Lệnh cắt đã gửi cho YouTube vẫn có thể đang xử lý.")
        self.wake.set()

    def retry(self, task_id):
        with self.lock:
            task = self.store.archive(task_id)
            if task["status"] not in TERMINAL:
                raise ValueError("Chỉ tiếp tục tác vụ đã dừng hoặc cần kiểm tra.")
            if task["status"] == "needs_login" and not self.session.get("connected"):
                raise ValueError("Chưa kết nối phiên YouTube Studio. Vui lòng chọn kênh và hoàn tất kết nối ở trên trước khi bấm Tiếp tục.")
            task.update(deadline=time.time() + 86400, clean_checks=0)
            self._update(task, "queued", "Đang xếp hàng kiểm tra bản lưu trong Studio…", delay=0)
        self.wake.set()

    def _update(self, task, status, message, delay=None):
        task.update(status=status, message=message, updated_at=time.time(),
                    next_at=time.time() + (self.interval if delay is None else delay))
        self.store.save_archive(task)
        self.store.log("archive:" + task["id"], message)

    def _get_browser(self):
        if self.browser is None:
            self.browser = self.factory()
        return self.browser

    def _run(self):
        try:
            while not self.closed.is_set():
                self.wake.clear()
                self._session_commands()
                self.tick()
                self.wake.wait(1)
        finally:
            if self.browser:
                self.browser.close()

    def _session_commands(self):
        if self.login.is_set():
            self.login.clear()
            with self.auth_lock:
                command = self.auth_command or ("manual", "", "")
                self.auth_command = None
            self.expected_login_channel = command[1] if command[0] == "tuat" else ""
            try:
                if self.cancel_login.is_set() or self.closed.is_set():
                    raise StudioIssue("Đã hủy đăng nhập YouTube.", "needs_login")
                if command[0] == "credentials":
                    self._get_browser().login(command[1], command[2],
                        cancelled=lambda: self.cancel_login.is_set() or self.closed.is_set())
                    self.browser_message = "Đã gửi bước đăng nhập YouTube. Hoàn tất xác minh hai bước nếu có, chọn kênh rồi bấm Đăng nhập xong — chạy nền."
                elif command[0] == "tuat":
                    self.browser_message = "Đang mở cửa sổ Studio riêng và nhập phiên kênh đã chọn từ Tuất…"
                    cookies = load_channel_session(command[1])
                    try:
                        if self.cancel_login.is_set() or self.closed.is_set():
                            raise StudioIssue("Đã hủy kết nối kênh Tuất.", "needs_login")
                        self._get_browser().import_session(command[1], cookies, background=False)
                    finally:
                        cookies = None
                    self.finish.set()
                    self.browser_message = "Đang xác minh phiên Tuất trong cửa sổ Studio. Nếu được yêu cầu, chọn đúng kênh hoặc hoàn tất xác minh tại đó."
                else:
                    self._get_browser().open_login()
                    self.browser_message = "Studio đã mở. Đăng nhập và chọn kênh, rồi bấm Đăng nhập xong — chạy nền. Tác vụ cắt đang tạm dừng."
            except Exception as exc:
                message = str(exc) if isinstance(exc, StudioIssue) else "Không đăng nhập được YouTube. Kiểm tra cửa sổ Chrome/Edge hoặc dùng phiên kênh từ tool Tuất."
                # Even an automation exception may include a field's value.
                # Only generic errors or redacted Studio messages reach the UI.
                for sensitive in command[1:]:
                    if sensitive:
                        message = message.replace(sensitive, "[ẩn]")
                self.browser_message = message
                if command[0] == "tuat":
                    self.login_pending = False
                    self.auth_source = ""
                    self.expected_login_channel = ""
                self.auth_busy = False
            finally:
                command = None
                if not self.finish.is_set():
                    self.auth_busy = False
        if self.cancel_login.is_set():
            self.cancel_login.clear()
            self.finish.clear()
            if self.browser:
                self.browser.close()
                self.browser = None
            self.login_pending = False
            self.auth_busy = False
            self.auth_source = ""
            self.expected_login_channel = ""
            self.browser_message = "Đã hủy bước đăng nhập. Giữ thiết lập phiên đã lưu trước đó."
        if self.finish.is_set():
            self.finish.clear()
            try:
                browser = self._get_browser()
                channel_id = browser.verify_login()["channel_id"]
                if not re.fullmatch(r"UC[A-Za-z0-9_-]{22}", channel_id):
                    raise StudioIssue("Studio chưa trả về Channel ID hợp lệ.", "needs_login")
                if self.expected_login_channel and channel_id != self.expected_login_channel:
                    raise StudioIssue("Phiên Tuất mở sang kênh khác; chưa kết nối. Load lại đúng kênh trong tool Tuất.", "needs_login")
                browser.use_saved_session(channel_id)
                # Verify the same profile is usable after the visible window
                # closes. A successful login form alone is not enough.
                if browser.verify_login()["channel_id"] != channel_id:
                    raise StudioIssue("Phiên chạy nền đang ở kênh khác. Mở Studio và chọn lại kênh.", "needs_login")
                if self.closed.is_set() or self.cancel_login.is_set():
                    return
                self.session = dict(connected=True, background=True, channel_id=channel_id, verified_at=time.time())
                self.store.save_setting("studio_session", self.session)
                self.login_pending = False
                self.browser_message = (f"Đã dùng phiên từ tool Tuất của kênh {channel_id}. Tool đang chạy nền tự động xử lý video."
                                        if self.expected_login_channel else
                                        f"Đã lưu phiên kênh {channel_id}. Tool đang chạy nền tự động xử lý video.")
                self.expected_login_channel = ""
                with self.lock:
                    for task in self.store.archives():
                        if task["status"] == "needs_login" or task["status"] not in TERMINAL:
                            task.update(clean_checks=0, deadline=time.time() + 86400)
                            self._update(task, "queued", "Đã kết nối Studio thành công. Đang tự động kiểm tra bản lưu…", delay=0)
            except Exception as exc:
                self.session = {**self.session, "connected": False}
                self.store.save_setting("studio_session", self.session)
                self.browser_message = (str(exc) if isinstance(exc, StudioIssue) else
                    "Chưa xác nhận được phiên chạy nền. Mở lại Studio, đăng nhập và chọn kênh.")
            finally:
                self.auth_busy = False

    def tick(self, now=None):
        now = time.time() if now is None else now
        for snapshot in reversed(self.store.archives()):
            if self.closed.is_set() or self.login_pending or self.cancel_login.is_set():
                break
            # Serialize cancel/retry with a single bounded browser operation.
            with self.lock:
                task = self.store.archive(snapshot["id"])
                if task["status"] in TERMINAL or task["next_at"] > now:
                    continue
                if now > task["deadline"]:
                    self._update(task, "needs_review", "Đã chờ 24 giờ. Kiểm tra Studio rồi bấm Tiếp tục nếu cần.")
                    continue
                try:
                    self._step(task)
                except StudioIssue as exc:
                    self._update(task, exc.status, str(exc))
                    if exc.status == "needs_login":
                        self.session = {**self.session, "connected": False}
                        self.store.save_setting("studio_session", self.session)
                        self.browser_message = "Phiên YouTube cần đăng nhập lại. Dùng form Đăng nhập YouTube hoặc lấy phiên kênh từ tool Tuất."
                except Exception:
                    self._update(task, "needs_review", "Mất kết nối hoặc giao diện Studio thay đổi. Kiểm tra Studio trước khi tiếp tục; chưa xác nhận thành công.")

    def _step(self, task):
        browser = self._get_browser()
        state = browser.inspect(task["video_id"])
        self.browser_message = ("Đang dùng phiên Studio đã lưu để xử lý nền." if self.session["background"] else
                                "Đã kết nối Studio. Có thể lưu phiên bằng nút Đăng nhập xong — chạy nền.")
        kind = state["kind"]
        if kind == "live":
            task["clean_checks"] = 0
            self._update(task, "waiting_live", "YouTube chưa kết thúc live. Bật Auto-stop hoặc kết thúc live trong Studio.")
            return
        if kind == "processing":
            task["clean_checks"] = 0
            if task["pending_claim"]:
                task["processing_seen"] = True
            self._update(task, "processing", "YouTube đang kiểm tra hoặc xử lý bản lưu. Chưa coi là hết claim.")
            return
        if kind == "clean":
            task.update(clean_checks=task["clean_checks"] + 1, pending_claim=None, processing_seen=False)
            if task["clean_checks"] < 2:
                self._update(task, "verifying", "Studio báo không có claim. Sẽ đọc lại trước khi hoàn tất / công khai.")
                return
            if task["publish_after"]:
                self._update(task, "publishing", "Đã kiểm tra lại không có claim. Đang đặt chế độ Công khai.", delay=0)
                # Re-read the copyright page immediately before changing visibility.
                if browser.inspect(task["video_id"])["kind"] != "clean":
                    task["clean_checks"] = 0
                    self._update(task, "queued", "Trạng thái vừa thay đổi. Chưa chuyển Công khai.")
                    return
                if self.closed.is_set():
                    raise StudioIssue("Ứng dụng đang đóng. Chưa gửi lệnh Công khai.")
                if self.login_pending or self.cancel_login.is_set():
                    raise StudioIssue("Đã tạm dừng để đăng nhập Studio. Chưa gửi lệnh Công khai.", "queued")
                browser.publish(task["video_id"])
                message = "Đã xác nhận Công khai và Studio không có claim tại lần kiểm tra này."
            else:
                message = "Studio không có claim tại lần kiểm tra này. Giữ chế độ hiển thị hiện tại."
            self._update(task, "completed", message)
            return
        if kind != "claim" or not state.get("fingerprint"):
            raise StudioIssue("Chưa đọc được kết quả kiểm tra bản quyền. Mở Studio để kiểm tra.")
        task["clean_checks"] = 0
        fingerprint = state["fingerprint"]
        if task["pending_claim"] == fingerprint:
            if task["processing_seen"]:
                raise StudioIssue("YouTube đã xử lý nhưng claim cũ vẫn còn. Cần kiểm tra thủ công; không tự cắt lặp cùng đoạn.")
            raise StudioIssue("Lệnh cắt trước chưa có kết quả rõ ràng. Kiểm tra Studio; không tự gửi lại cùng đoạn.")
        if task["cuts"] >= 20:
            raise StudioIssue("Đã gửi 20 lượt cắt cho video này. Kiểm tra bản lưu trước khi xử lý tiếp.")
        self._update(task, "preparing", "Chuẩn bị cắt đoạn bị claim: " + state.get("description", "")[:400])

        def before_confirm():
            if self.closed.is_set():
                raise StudioIssue("Ứng dụng đang đóng. Chưa gửi xác nhận cắt.")
            if self.login_pending or self.cancel_login.is_set():
                raise StudioIssue("Đã tạm dừng để đăng nhập Studio. Chưa gửi xác nhận cắt.", "queued")
            # Commit immediately before the irreversible click, including on
            # network failure/crash. A missing Trim menu doesn't leave a false
            # pending operation that would prevent a safe retry.
            task.update(pending_claim=fingerprint, processing_seen=False, cuts=task["cuts"] + 1)
            self._update(task, "submitting", "Gửi xác nhận cắt đoạn bị claim: " + state.get("description", "")[:400])

        browser.trim(task["video_id"], fingerprint, before_confirm=before_confirm)
        if task["pending_claim"] != fingerprint:
            raise StudioIssue("Chưa có xác nhận đã gửi lệnh cắt.")
        self._update(task, "processing", "Đã gửi lệnh cắt. Chờ YouTube xử lý và kiểm tra lại claim.")

    def shutdown(self):
        self.closed.set()
        with self.auth_lock:
            self.auth_command = None
        self.wake.set()
        if self.thread:
            self.thread.join()
