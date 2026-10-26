"""Run with python -X utf8 -m tests.browser_archive_qa (no YouTube traffic)."""
from __future__ import annotations

import json
import socket
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import Mock, patch

import uvicorn
from playwright.sync_api import sync_playwright, expect

from live.api import create_app
from live.studio import chrome_path

ROOT = Path(__file__).resolve().parents[1]


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
        with patch("live.api.probe_video", return_value=media):
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
                        expect(page.locator("#archive-publish")).to_be_checked()
                        page.locator("#studio-open").click()
                        expect(page.locator("#studio-status")).to_contain_text("Studio đã mở", timeout=8000)
                        expect(page.locator("#studio-connect")).to_be_enabled()
                        page.locator("#studio-connect").click()
                        expect(page.locator("#studio-session")).to_contain_text("Chạy nền", timeout=8000)
                        expect(page.locator("#studio-connect")).to_be_disabled()
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
                        assert not errors, errors
                        studio.trim.assert_not_called()
                        studio.publish.assert_not_called()
                        report = {"passed": True, "js_errors": errors,
                                  "checks": ["login and save background session", "permanent-cut consent", "queue / logs / cancel", "schedule options roundtrip", "default auto-public", "desktop / mobile"]}
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
