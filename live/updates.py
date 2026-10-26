"""Download the exact main-branch build, verify it, then replace only our EXE."""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .runtime import ROOT, build_info

ASSET = "LocalLiveStudio-Windows-x64.exe"
MAX_EXE = 1024**3


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, url):
        target = super().redirect_request(request, fp, code, message, headers, url)
        if target and urllib.parse.urlparse(request.full_url).netloc != urllib.parse.urlparse(url).netloc:
            target.remove_header("Authorization")
        return target


class UpdateService:
    def __init__(self, directory: Path, *, info=None, opener=None, installed_exe=None):
        self.directory = directory / "updates"
        self.info = info or build_info()
        self.repo = self.info["repository"]
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repo):
            raise ValueError("Địa chỉ repo cập nhật không hợp lệ.")
        self.api = f"https://api.github.com/repos/{self.repo}"
        self.opener = opener or urllib.request.build_opener(SafeRedirect()).open
        self.installed_exe = installed_exe or Path(sys.executable)
        self.lock = threading.RLock()
        self.operation = threading.Lock()
        self.closed = threading.Event()
        self.worker = None
        self.release = None
        self.prepared = None
        self.state = {"status": "idle", "message": "Bấm Kiểm tra cập nhật để xem commit mới nhất.",
                      "installed": self.info, "latest_commit": "", "downloaded": 0, "total": 0,
                      "release_url": f"https://github.com/{self.repo}/releases", "can_install": False}

    def snapshot(self):
        with self.lock:
            return dict(self.state)

    def _state(self, **values):
        with self.lock:
            self.state.update(values)

    def _request(self, url, *, binary=False):
        headers = {"User-Agent": "LocalLiveStudio-updater", "Accept": "application/octet-stream" if binary else "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28", "Cache-Control": "no-cache"}
        # A private repo can use a machine-local token. Never send it to a CDN,
        # log it, save it in settings, or return it to the web UI.
        if urllib.parse.urlparse(url).hostname == "api.github.com" and os.environ.get("LIVE_GITHUB_TOKEN"):
            token = os.environ["LIVE_GITHUB_TOKEN"].strip()
            if re.search(r"\s", token):
                raise ValueError("LIVE_GITHUB_TOKEN không hợp lệ.")
            headers["Authorization"] = "Bearer " + token
        return self.opener(urllib.request.Request(url, headers=headers), timeout=20)

    def _json(self, url, *, binary=False):
        with self._request(url, binary=binary) as response:
            data = response.read(2 * 1024 * 1024 + 1)
        if len(data) > 2 * 1024 * 1024:
            raise ValueError("Phản hồi cập nhật quá lớn.")
        return json.loads(data)

    def _asset_url(self, asset, commit):
        expected = f"https://github.com/{self.repo}/releases/download/build-{commit}/{asset['name']}"
        if asset.get("browser_download_url") != expected:
            raise ValueError("File cập nhật không thuộc đúng repo / commit.")
        if os.environ.get("LIVE_GITHUB_TOKEN"):
            url = asset.get("url", "")
            if not re.fullmatch(re.escape(self.api) + r"/releases/assets/\d+", url):
                raise ValueError("Đường dẫn tải bản riêng tư không hợp lệ.")
            return url
        return expected

    def _check(self):
        self._state(status="checking", message="Đang đọc commit mới nhất và bản EXE tương ứng…", can_install=False)
        head = self._json(self.api + "/commits/" + urllib.parse.quote(self.info["branch"], safe=""))
        commit = head.get("sha", "")
        if not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise ValueError("GitHub chưa trả về commit hợp lệ.")
        title = (str(head.get("commit", {}).get("message", "")).splitlines() or [""])[0][:200]
        self._state(latest_commit=commit, commit_message=title)
        if commit == self.info.get("commit") and self.info["packaged"]:
            self._state(status="current", message="Bạn đang dùng bản của commit mới nhất.")
            return False
        try:
            release = self._json(self.api + "/releases/tags/build-" + commit)
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
            self._state(status="building", message=f"Commit {commit[:8]} đã có. GitHub đang đóng gói hoặc chưa phát hành EXE; hãy kiểm tra lại sau.")
            return False
        if release.get("draft") or release.get("tag_name") != "build-" + commit:
            raise ValueError("Bản phát hành không khớp commit mới nhất.")
        assets = {asset["name"]: asset for asset in release.get("assets", [])}
        if not {ASSET, "update.json"}.issubset(assets):
            self._state(status="building", message="GitHub chưa tải đủ file của bản mới. Hãy kiểm tra lại sau.")
            return False
        manifest = self._json(self._asset_url(assets["update.json"], commit), binary=True)
        if (manifest.get("schema") != 1 or manifest.get("repository") != self.repo or manifest.get("commit") != commit or
                manifest.get("target") != "windows-x64" or manifest.get("file") != ASSET or
                not re.fullmatch(r"[0-9a-f]{64}", str(manifest.get("sha256", ""))) or
                type(manifest.get("size")) is not int or not 0 < manifest["size"] <= MAX_EXE or
                assets[ASSET].get("size") != manifest["size"]):
            raise ValueError("Thông tin hoặc checksum của bản cập nhật không hợp lệ.")
        self.release = {"manifest": manifest, "url": self._asset_url(assets[ASSET], commit)}
        self._state(status="available", total=manifest["size"], downloaded=0,
                    message=f"Có bản mới từ commit {commit[:8]}." if self.info["packaged"] else
                            f"Có EXE từ commit {commit[:8]}. Bản mã nguồn dùng liên kết Tải EXE để cài lần đầu.")
        return True

    def _download(self):
        if not self.info["packaged"]:
            raise ValueError("Bản mã nguồn chưa hỗ trợ tự thay chương trình. Tải EXE từ trang phát hành để dùng cập nhật trong tool.")
        if not self._check():
            return
        manifest = self.release["manifest"]
        self.directory.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(self.directory).free < manifest["size"] + 50 * 1024**2:
            raise ValueError("Ổ lưu cập nhật không đủ dung lượng trống.")
        stage = self.directory / manifest["commit"]
        stage.mkdir(exist_ok=True)
        temporary, final = stage / "download.part", stage / ASSET
        self._state(status="downloading", message="Đang tải EXE và kiểm tra SHA-256…", downloaded=0)
        digest, size, prefix = hashlib.sha256(), 0, b""
        try:
            with self._request(self.release["url"], binary=True) as response, temporary.open("wb") as output:
                while chunk := response.read(1024 * 1024):
                    if self.closed.is_set():
                        raise ValueError("Ứng dụng đã đóng; lượt tải được dừng.")
                    prefix = (prefix + chunk)[:2]
                    size += len(chunk)
                    if size > manifest["size"]:
                        raise ValueError("File tải về lớn hơn thông tin phát hành.")
                    output.write(chunk)
                    digest.update(chunk)
                    self._state(downloaded=size)
                output.flush()
                os.fsync(output.fileno())
            if size != manifest["size"] or digest.hexdigest() != manifest["sha256"] or prefix != b"MZ":
                raise ValueError("File cập nhật tải chưa đủ hoặc sai SHA-256; chương trình hiện tại được giữ nguyên.")
            temporary.replace(final)
            self.prepared = {**manifest, "path": str(final.resolve())}
            self._state(status="ready", message="Đã tải và xác minh. Bấm Cài & khởi động lại khi các luồng live đã dừng.", can_install=True)
        finally:
            temporary.unlink(missing_ok=True)

    def start(self, action):
        if action not in {"check", "download"}:
            raise ValueError("Thao tác cập nhật không hợp lệ.")
        if not self.operation.acquire(blocking=False):
            return self.snapshot()

        def work():
            try:
                (self._check if action == "check" else self._download)()
            except Exception as error:
                if isinstance(error, urllib.error.HTTPError):
                    message = ("GitHub đang giới hạn truy cập hoặc token chưa đủ quyền. Thử lại sau." if error.code in {403, 429} else
                               "Không đọc được repo. Repo riêng tư cần LIVE_GITHUB_TOKEN trên máy này." if error.code == 404 else
                               f"GitHub trả HTTP {error.code}; hãy thử lại.")
                elif isinstance(error, (ValueError, json.JSONDecodeError)):
                    message = str(error)
                else:
                    message = "Không kết nối hoặc ghi được bản cập nhật. Kiểm tra mạng, ổ đĩa và thử lại."
                self._state(status="failed", message=message, can_install=bool(self.prepared))
            finally:
                self.operation.release()

        self.worker = threading.Thread(target=work, daemon=True, name="github-updater")
        self.worker.start()
        return self.snapshot()

    def launch_installer(self, *, port, max_streams):
        if not self.info["packaged"] or os.name != "nt" or not self.prepared:
            raise ValueError("Cần tải và xác minh bản EXE trước khi cài cập nhật.")
        manifest = self.prepared
        staged = Path(manifest["path"])
        if not staged.resolve().is_relative_to(self.directory.resolve()):
            raise ValueError("Đường dẫn file cập nhật không hợp lệ.")
        if not staged.is_file() or staged.stat().st_size != manifest["size"]:
            raise ValueError("File cập nhật đã thay đổi. Hãy tải lại trước khi cài.")
        with staged.open("rb") as handle:
            if hashlib.file_digest(handle, "sha256").hexdigest() != manifest["sha256"]:
                raise ValueError("File cập nhật đã thay đổi. Hãy tải lại trước khi cài.")
        # Re-check main immediately before applying: never silently install an
        # older successful build while the newest commit is still building.
        head = self._json(self.api + "/commits/" + urllib.parse.quote(self.info["branch"], safe=""))
        if head.get("sha") != manifest["commit"]:
            raise ValueError("Repo vừa có commit mới hơn. Bấm Kiểm tra cập nhật và tải lại.")
        executable = self.installed_exe.resolve()
        probe = executable.parent / (".update-write-check-" + secrets.token_hex(8))
        created = False
        try:
            with probe.open("xb"):
                created = True
        except OSError:
            raise ValueError("Thư mục chứa EXE không cho ghi. Chuyển tool sang thư mục bạn có quyền ghi trước khi cập nhật.") from None
        finally:
            if created:
                probe.unlink(missing_ok=True)
        configuration = {"pid": os.getpid(), "parent_pid": os.getppid(), "source": str(staged), "target": str(executable),
                         "sha256": manifest["sha256"], "commit": manifest["commit"],
                         "port": int(port), "max_streams": int(max_streams), "data_dir": str(self.directory.parent.resolve())}
        config_file = staged.parent / "install.json"
        config_file.write_text(json.dumps(configuration), encoding="utf-8")
        helper = staged.parent / "install-update.ps1"
        shutil.copy2(ROOT / "packaging" / "install-update.ps1", helper)
        process = subprocess.Popen(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", str(helper), "-Config", str(config_file)], cwd=staged.parent,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW)
        self._state(status="installing", message="Đang đóng chương trình để thay EXE. Trang sẽ kết nối lại khi bản mới sẵn sàng.", can_install=False)
        return process.pid

    def shutdown(self):
        self.closed.set()
        if self.worker:
            self.worker.join()
