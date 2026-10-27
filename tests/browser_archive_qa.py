"""Run with python -X utf8 -m tests.browser_archive_qa (no YouTube traffic)."""
from __future__ import annotations

import json
import socket
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlparse
from unittest.mock import Mock, patch

import uvicorn
from playwright.sync_api import sync_playwright, expect

from live.api import create_app
from live.studio import chrome_path

ROOT = Path(__file__).resolve().parents[1]


def verify_stream_health_ui(page):
    page.locator("details.advanced").first.locator("summary").click()
    page.locator("#encoder").select_option("h264_qsv")
    expect(page.locator("#encoder-help")).to_contain_text("Arc A770")
    fixture = {"id": "health-fixture", "name": "Luồng mô phỏng", "source_path": "fixture.mp4",
               "media": {"name": "fixture.mp4", "width": 1920, "height": 1080, "duration": 60, "size": 1000},
               "encoder": "h264_qsv", "bitrate": 12000, "mode": "continuous", "duration": 60,
               "start_at": time.time(), "key_hint": "•••• test", "status": "streaming", "last_error": "",
               "metrics": {"elapsed": 20, "fps": 13, "speed": 0.43, "realtime_speed": 0.43,
                           "warning": "Phát chậm (0.43x trong khoảng 10 giây gần đây)"}}

    def serve(route):
        route.fulfill(json=[fixture])

    page.route("**/api/streams", serve)
    try:
        page.locator("#refresh").click()
        row = page.locator("#streams-body tr")
        expect(row).to_contain_text("Intel · Quick Sync / Arc")
        expect(row.locator(".status-chip")).to_have_text("Phát chậm")
        expect(row).to_contain_text("13 fps · 0.43x")
        fixture["metrics"].update(realtime_speed=1.0, warning="")
        page.locator("#refresh").click()
        expect(row.locator(".status-chip")).to_have_text("Đang phát")
        expect(row).to_contain_text("1.00x")
    finally:
        page.unroute("**/api/streams", serve)
        page.locator("#encoder").select_option("libx264")
        page.locator("#refresh").click()


def verify_update_ui(page):
    """A new server rejects the previous local token after the EXE restart."""
    progress = {"restarted": False, "stale_reads": 0}
    info = {"version": "1.0.0", "commit": "a" * 40, "packaged": True}
    update = {"installed": info, "status": "idle", "message": "Chờ kiểm tra",
              "total": 100, "downloaded": 0, "can_install": False,
              "release_url": "https://github.com/duykhanh8386/local-live-studio/releases"}

    def serve(route):
        path = urlparse(route.request.url).path
        if path == "/api/health":
            return route.fulfill(json={"service": "local-live-studio", "commit": ("b" if progress["restarted"] else "a") * 40})
        if path == "/api/system" and progress["restarted"]:
            response = route.fetch()
            data = response.json()
            data["token"] = "new-local-token"
            return route.fulfill(response=response, json=data)
        if progress["restarted"] and path not in {"/api/system", "/api/health"}:
            if route.request.headers.get("x-local-token") != "new-local-token":
                progress["stale_reads"] += 1
                return route.fulfill(status=403, json={"detail": "Hãy tải lại trang local."})
            if path == "/api/streams":
                return route.fulfill(json=[])
            if path == "/api/archives":
                return route.fulfill(json={"tasks": [], "browser_message": "Sẵn sàng", "session": {}})
        if path == "/api/update/check":
            update.update(status="available", message="Có bản mới")
        elif path == "/api/update/download":
            update.update(status="ready", message="Đã tải và xác minh", can_install=True, downloaded=100)
        elif path == "/api/update/install":
            progress["restarted"] = True
            info["commit"] = "b" * 40
            update.update(status="current", message="Bạn đang dùng bản mới nhất", can_install=False)
            return route.fulfill(json={"ok": True})
        elif path != "/api/update":
            return route.fallback()
        return route.fulfill(json=update)

    page.route("**/api/**", serve)
    page.locator("#update-check").click()
    expect(page.locator("#update-download")).to_be_enabled()
    page.locator("#update-download").click()
    expect(page.locator("#update-install")).to_be_enabled()
    page.locator("#update-install").click()
    with page.expect_navigation(wait_until="domcontentloaded"):
        page.locator("#confirm-action").click()
    expect(page.locator("#update-version")).to_contain_text("bbbbbbbb")
    assert progress["restarted"] and progress["stale_reads"] == 0, progress


def main():
    qa = ROOT / "qa" / "archives"
    qa.mkdir(parents=True, exist_ok=True)
    errors = []
    studio = Mock()
    studio.inspect.return_value = {"kind": "live"}
    studio.verify_login.return_value = {"channel_id": "UC" + "a" * 22}
    with tempfile.TemporaryDirectory(prefix="archive-ui-") as folder:
        app = create_app(Path(folder), start_scheduler=False, studio_factory=lambda: studio)
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        media = dict(path=str(Path(folder) / "test.mp4"), name="test.mp4", size=1000, duration=1,
                     width=320, height=180, video_codec="h264", has_audio=True, audio_codec="aac")
        with (
            patch("live.api.probe_video", return_value=media),
            patch("live.api.list_tuat_channels", return_value=[{"id": "UC" + "a" * 22, "name": "Kênh Tuất mô phỏng"}]),
            patch("live.archives.load_channel_session", return_value=[{"name": "SAPISID", "value": "synthetic-cookie"}]),
        ):
            server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False))
            thread = threading.Thread(target=server.run, daemon=True)
            thread.start()
            try:
                end = time.monotonic() + 10
                while not server.started:
                    if time.monotonic() > end:
                        raise RuntimeError("Preview did not start")
                    time.sleep(.05)
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(executable_path=chrome_path(), headless=True)
                    try:
                        context = browser.new_context(viewport={"width": 1440, "height": 1050})
                        context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(base + "/") else route.abort())
                        page = context.new_page()
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        page.goto(base)
                        expect(page.locator("#tool-status")).to_contain_text("sẵn sàng")
                        verify_stream_health_ui(page)
                        expect(page.locator("#archive-publish")).to_be_checked()
                        page.locator("#studio-email").fill("fixture@example.test")
                        page.locator("#studio-password").fill("synthetic-password")
                        page.locator("#studio-login").click()
                        expect(page.locator("#studio-password")).to_have_value("")
                        expect(page.locator("#studio-email")).to_have_value("")
                        expect(page.locator("#studio-status")).to_contain_text("Đã gửi bước đăng nhập", timeout=8000)
                        page.locator("#studio-cancel-login").click()
                        expect(page.locator("#studio-login")).to_be_enabled()
                        page.locator("#studio-open").click()
                        expect(page.locator("#studio-status")).to_contain_text("Studio đã mở", timeout=8000)
                        expect(page.locator("#studio-connect")).to_be_enabled()
                        page.locator("#studio-connect").click()
                        expect(page.locator("#studio-session")).to_contain_text("Chạy nền", timeout=8000)
                        expect(page.locator("#studio-connect")).to_be_disabled()
                        page.locator("#studio-tuat-load").click()
                        expect(page.locator("#studio-tuat-channel")).to_be_enabled()
                        page.locator("#studio-tuat-channel").select_option("UC" + "a" * 22)
                        page.locator("#studio-tuat-connect").click()
                        expect(page.locator("#studio-status")).to_contain_text("Đã dùng phiên từ tool Tuất", timeout=8000)
                        studio.import_session.assert_called_once()
                        page.locator("#archive-video").fill("https://youtu.be/Abc123_-XYZ")
                        page.locator("#archive-add").click()
                        expect(page.locator("#archives-body tr")).to_have_count(0)
                        page.locator("#archive-consent").check()
                        page.locator("#archive-add").click()
                        expect(page.locator("#archives-body tr")).to_have_count(1)
                        expect(page.locator("#archives-body")).to_contain_text("Chờ live kết thúc", timeout=8000)
                        page.locator('[data-action="archive-logs"]').click()
                        expect(page.locator("#logs-content")).to_contain_text("YouTube chưa kết thúc live")
                        page.locator('#logs-dialog [data-close="logs-dialog"]').first.click()
                        page.locator('[data-action="archive-cancel"]').click()
                        expect(page.locator("#archives-body")).to_contain_text("Đã dừng")

                        page.locator("#stream-name").fill("Lịch có xử lý bản lưu")
                        page.locator("#stream-key").fill("synthetic-key-not-a-youtube-key")
                        page.locator("#source-path").fill(media["path"])
                        page.locator(".archive-options summary").click()
                        page.locator("#youtube-video-id").fill("https://www.youtube.com/watch?v=Def123_-XYZ")
                        page.locator("#trim-after-live").check()
                        expect(page.locator("#publish-after-trim")).to_be_checked()
                        page.locator("#save-stream").click()
                        expect(page.locator("#streams-body tr")).to_have_count(1)
                        page.locator('[data-action="edit"]').click()
                        expect(page.locator("#youtube-video-id")).to_have_value("Def123_-XYZ")
                        expect(page.locator("#trim-after-live")).to_be_checked()
                        page.locator("#cancel-edit").click()
                        assert page.locator("#youtube-video-id").get_attribute("required") is None
                        page.locator("#archives-title").scroll_into_view_if_needed()
                        page.screenshot(path=str(qa / "desktop.png"), full_page=True)
                        page.set_viewport_size({"width": 390, "height": 844})
                        page.screenshot(path=str(qa / "mobile.png"), full_page=True)
                        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "Page overflows mobile viewport"
                        verify_update_ui(page)
                        assert not errors, errors
                        studio.trim.assert_not_called()
                        studio.publish.assert_not_called()
                        report = {"passed": True, "js_errors": errors,
                                  "checks": ["Intel encoder selection and slow-output warning recovery", "email/password form clears inputs", "Tuất channel selection and session verification", "login and save background session", "permanent-cut consent", "queue / logs / cancel", "schedule options roundtrip", "default auto-public", "desktop / mobile", "update buttons and new local token after restart"]}
                        (qa / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                        print(json.dumps(report, ensure_ascii=False))
                    finally:
                        browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                assert not thread.is_alive()


if __name__ == "__main__":
    main()
