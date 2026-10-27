"""Owner-session Studio automation, inspired by Youtube-remove-copyright.

Edits use the signed-in browser UI. Unknown UI states stop the job; a missing
selector is never evidence that a claim has disappeared.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import urlencode, urlparse


class StudioIssue(ValueError):
    def __init__(self, message, status="needs_review"):
        super().__init__(message)
        self.status = status


def chrome_path() -> str:
    configured = os.environ.get("LIVE_STUDIO_BROWSER")
    if configured:
        if Path(configured).is_file():
            return configured
        raise StudioIssue("LIVE_STUDIO_BROWSER chưa trỏ tới file Chrome/Edge hợp lệ.")
    for root in (os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)"), os.environ.get("LOCALAPPDATA")):
        if root:
            for suffix in ("Google/Chrome/Application/chrome.exe", "Microsoft/Edge/Application/msedge.exe"):
                candidate = Path(root) / suffix
                if candidate.is_file():
                    return str(candidate)
    for name in ("google-chrome", "chromium", "chromium-browser", "msedge"):
        if found := shutil.which(name):
            return found
    raise StudioIssue("Cần cài Google Chrome hoặc Microsoft Edge để xử lý bản lưu trong Studio.")


CLEAN = re.compile(r"no copyright (?:claims|issues)|no copyright.protected content (?:was )?found|"
                   r"no issues found|no claims found|"
                   r"không có (?:khiếu nại|vấn đề|thông tin khiếu nại) (?:nào )?(?:về )?bản quyền|"
                   r"chưa (?:phát hiện|tìm thấy) (?:nội dung|khiếu nại) (?:nào )?(?:có |về )?bản quyền|"
                   r"không tìm thấy nội dung (?:nào )?có bản quyền|không tìm thấy vấn đề|"
                   r"không có khiếu nại", re.I)
PROCESSING = re.compile(r"(?:video|edits|changes) (?:is|are) being process|processing (?:video|edits|changes)|"
                        r"(?:video|edits|changes) (?:is|are) (?:still )?processing|"
                        r"video is being edited|checks (?:are )?(?:running|in progress)|"
                        r"đang (?:xử lý (?:video|nội dung chỉnh sửa|thay đổi)|kiểm tra)", re.I)
TRIM = re.compile(r"^(?:trim out segment|trim segment|cắt bỏ phân đoạn|cắt phân đoạn|cắt bỏ đoạn|cắt đoạn)$", re.I)
CONTINUE = re.compile(r"^(?:continue|tiếp tục)$", re.I)
CONFIRM = re.compile(r"^(?:trim|confirm|save|cắt|cắt bỏ|xác nhận|lưu)$", re.I)
ACTIONS = re.compile(r"^(?:select action|select an action|take action|actions|chọn hành động|chọn thao tác|thực hiện hành động|thao tác)$", re.I)
CLAIM_ROWS = "ytcr-video-claim, ytcr-video-claim-row, ytcr-claim-row, ytcr-claim-list-item, [data-claim-id]"
CONTROL = "button, ytcp-button, ytcp-icon-button, tp-yt-paper-item, [role=button], [role=menuitem]"


class StudioBrowser:
    def __init__(self, profile: Path, *, background=False, channel_id=""):
        self.profile = profile
        self.background = background
        self.expected_channel_id = channel_id
        self.playwright = None
        self.browser = None
        self.page = None
        self.process = None
        self.current_claim = None

    def connect(self):
        if self.browser and self.browser.is_connected() and self.page and not self.page.is_closed():
            return
        self.close()
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise StudioIssue("Thiếu Playwright. Chạy start.bat để cài thư viện theo requirements.txt.")
        executable = chrome_path()
        self.profile.mkdir(parents=True, exist_ok=True)
        # This profile belongs exclusively to this feature. Never attach to the
        # user's normal Chrome profile or an arbitrary debugging port.
        port_file = self.profile / "DevToolsActivePort"
        port_file.unlink(missing_ok=True)
        arguments = [executable, f"--user-data-dir={self.profile.resolve()}",
            "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=0", "--no-first-run",
            "--no-default-browser-check", "--disable-blink-features=AutomationControlled",
            "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"]
        if self.background:
            arguments.append("--headless=new")
        self.process = subprocess.Popen(arguments + ["about:blank"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        end = time.monotonic() + 20
        endpoint = None
        while time.monotonic() < end:
            try:
                port = int(port_file.read_text(encoding="utf-8").splitlines()[0])
                if 1 <= port <= 65535:
                    endpoint = f"http://127.0.0.1:{port}"
                    break
            except (OSError, ValueError, IndexError):
                pass
            if self.process.poll() is not None:
                break
            time.sleep(.15)
        if not endpoint:
            raise StudioIssue("Không mở được cửa sổ Studio riêng. Đóng cửa sổ Studio do tool mở trước đó rồi thử lại.")
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.connect_over_cdp(endpoint, timeout=15000)
        context = self.browser.contexts[0]
        self.page = context.pages[0] if context.pages else context.new_page()
        self.page.set_default_timeout(8000)
        self.page.set_default_navigation_timeout(20000)

    def open_login(self):
        self.expected_channel_id = ""
        self.set_background(False)
        self.connect()
        self.page.goto("https://studio.youtube.com", wait_until="domcontentloaded")
        self.page.bring_to_front()

    def login(self, email, password, *, cancelled=lambda: False):
        """Submit only to Google's own sign-in page; the user handles 2FA."""
        self.open_login()
        self.page.goto("https://accounts.google.com/v3/signin/identifier?" + urlencode({
            "service": "youtube", "continue": "https://studio.youtube.com/", "hl": "vi",
        }), wait_until="domcontentloaded")

        def field(selector):
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if cancelled():
                    raise StudioIssue("Đã hủy đăng nhập YouTube.", "needs_login")
                parsed = urlparse(self.page.url)
                if parsed.scheme != "https" or parsed.hostname != "accounts.google.com":
                    # Already authenticated or at a channel chooser: never type
                    # credentials into a redirected page, even a lookalike URL.
                    return None
                text = self.page.locator("body").inner_text(timeout=2000)
                if "/signin/rejected" in parsed.path or re.search(
                    r"browser or app may not be secure|trình duyệt hoặc ứng dụng này có thể không an toàn", text, re.I
                ):
                    raise StudioIssue("Google từ chối trình duyệt đăng nhập. Dùng phiên kênh đã đăng nhập trong tool Tuất, hoặc mở YouTube thủ công để đăng nhập lại.", "needs_login")
                visible = self._visible(self.page.locator(selector))
                if len(visible) == 1 and visible[0].is_enabled():
                    return visible[0]
                # A challenge must be completed by the owner, without filling
                # a password into a recovery/verification-code field.
                if "/challenge/" in parsed.path and not parsed.path.endswith("/pwd"):
                    return None
                self.page.wait_for_timeout(200)
            raise StudioIssue("Chưa thấy ô đăng nhập. Kiểm tra cửa sổ YouTube, hoàn tất xác minh rồi chọn kênh.", "needs_login")

        email_field = field('input[type="email"]')
        if email_field is None:
            return
        email_field.fill(email)
        self.page.locator("#identifierNext").click()
        password_field = field('input[type="password"]')
        if password_field is None:
            return
        password_field.fill(password)
        self.page.locator("#passwordNext").click()

    def import_session(self, channel_id, cookies, *, background=True):
        self.close()
        self.expected_channel_id = channel_id
        self.background = background
        self.connect()
        context = self.page.context
        context.clear_cookies()
        context.add_cookies(cookies)
        if not background:
            self.page.bring_to_front()

    def set_background(self, background):
        if self.background != background:
            self.close()
            self.background = background

    def use_saved_session(self, channel_id):
        self.expected_channel_id = channel_id
        self.set_background(True)

    def verify_login(self, *, timeout=30):
        """Prove Studio reached a channel dashboard; never export its cookies."""
        self.connect()
        observed_channels = set()

        def observe(response):
            # Some Studio versions put delegation in their own successful
            # requests rather than ytcfg. Read the channel identifier only.
            parsed = urlparse(response.url)
            if not 200 <= response.status < 300 or parsed.hostname != "studio.youtube.com" or not parsed.path.startswith("/youtubei/v1/"):
                return
            try:
                body = response.request.post_data_json or {}
                channel = body.get("context", {}).get("user", {}).get("delegationContext", {}).get("externalChannelId")
                if isinstance(channel, str):
                    observed_channels.add(channel)
            except (ValueError, AttributeError, TypeError):
                pass

        self.page.on("response", observe)
        start_time = time.monotonic()
        try:
            current_url = urlparse(self.page.url)
            needs_nav = True
            if self.expected_channel_id and current_url.hostname == "studio.youtube.com":
                current_match = re.search(r"/channel/(UC[A-Za-z0-9_-]{22})", current_url.path)
                if current_match and current_match.group(1) == self.expected_channel_id:
                    needs_nav = False

            if needs_nav:
                target = (f"https://studio.youtube.com/channel/{self.expected_channel_id}/dashboard"
                          if re.fullmatch(r"UC[A-Za-z0-9_-]{22}", self.expected_channel_id) else "https://studio.youtube.com")
                self.page.goto(target, wait_until="domcontentloaded")
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                # If YouTube presents an interstitial banner, auto-dismiss/continue to Studio
                try:
                    for elem in self.page.locator("button, a").all():
                        if elem.is_visible():
                            txt = (elem.inner_text() or "").strip().upper()
                            if "YOUTUBE STUDIO" in txt and any(w in txt for w in ["DIREKT", "CONTINUE", "TIẾP TỤC", "ZUM", "TO"]):
                                elem.click()
                                break
                except Exception:
                    pass

                parsed = urlparse(self.page.url)
                elapsed = time.monotonic() - start_time
                if elapsed > 3.0 and (
                    parsed.hostname in {"accounts.google.com", "consent.youtube.com", "consent.google.com"} or (
                        parsed.hostname in {"youtube.com", "www.youtube.com"} and parsed.path.startswith(("/signin", "/account", "/channel_switcher"))
                    )
                ):
                    raise StudioIssue("YouTube đang yêu cầu đăng nhập, xác minh hoặc chọn kênh. Hoàn tất trong cửa sổ Studio rồi bấm Xác minh lại phiên. Nếu cookie Tuất đã hết hiệu lực, Hủy rồi load lại kênh trong Tuất.", "needs_login")

                match = re.match(r"^/channel/(UC[A-Za-z0-9_-]{22})(?:/|$)", parsed.path)
                configured = self.page.evaluate("""() => {
                    const cfg = window.ytcfg;
                    const get = key => (cfg && typeof cfg.get === 'function' ? cfg.get(key) : undefined) ?? (cfg && (cfg.data_ || {})[key]) ?? (cfg && cfg[key]);
                    const context = (cfg && get('INNERTUBE_CONTEXT')) || {};
                    const delegation = ((context.user || {}).delegationContext || {}).externalChannelId;
                    if (delegation) return delegation;

                    const navLinks = Array.from(document.querySelectorAll('ytcp-navigation-item a, #menu a, ytcp-animatable a, a[href*="/channel/UC"]'));
                    for (const a of navLinks) {
                        const m = (a.getAttribute('href') || '').match(/\\/channel\\/(UC[A-Za-z0-9_-]{22})/);
                        if (m) return m[1];
                    }

                    return (cfg && get('CHANNEL_ID')) || '';
                }""")
                app = self.page.locator("ytcp-app")
                app_loaded = bool(self._visible(app) or self._visible(app.locator(":scope > *")))
                if app_loaded and match:
                    detected_channel = match[1]
                    if self.expected_channel_id:
                        if detected_channel == self.expected_channel_id:
                            return {"channel_id": detected_channel}
                    else:
                        if configured == detected_channel or detected_channel in observed_channels or not configured:
                            return {"channel_id": detected_channel}
                self.page.wait_for_timeout(250)
        finally:
            self.page.remove_listener("response", observe)
        parsed = urlparse(self.page.url)
        match = re.match(r"^/channel/(UC[A-Za-z0-9_-]{22})(?:/|$)", parsed.path)
        configured_final = self.page.evaluate("""() => {
            const cfg = window.ytcfg;
            const get = key => (cfg && typeof cfg.get === 'function' ? cfg.get(key) : undefined) ?? (cfg && (cfg.data_ || {})[key]) ?? (cfg && cfg[key]);
            const context = (cfg && get('INNERTUBE_CONTEXT')) || {};
            const delegation = ((context.user || {}).delegationContext || {}).externalChannelId;
            if (delegation) return delegation;

            const navLinks = Array.from(document.querySelectorAll('ytcp-navigation-item a, #menu a, ytcp-animatable a, a[href*="/channel/UC"]'));
            for (const a of navLinks) {
                const m = (a.getAttribute('href') || '').match(/\\/channel\\/(UC[A-Za-z0-9_-]{22})/);
                if (m) return m[1];
            }

            return (cfg && get('CHANNEL_ID')) || '';
        }""")
        detected_final = match[1] if (parsed.hostname == "studio.youtube.com" and match) else configured_final
        if parsed.hostname == "studio.youtube.com" and detected_final and self.expected_channel_id and detected_final != self.expected_channel_id:
            message = f"Studio đang mở kênh {detected_final}, khác kênh đã chọn ({self.expected_channel_id}). Chuyển đúng kênh trong cửa sổ Studio rồi bấm Xác minh lại phiên."
        elif not self._visible(self.page.locator("ytcp-app")) and not self._visible(self.page.locator("ytcp-app > *")):
            message = "Studio chưa tải được giao diện kênh. Kiểm tra mạng và cửa sổ Studio; tải lại trang rồi bấm Xác minh lại phiên hoặc Hủy để thử lại."
        else:
            message = "Studio đã mở nhưng chưa xác nhận được ngữ cảnh Channel ID. Chọn đúng kênh trong cửa sổ Studio rồi bấm Xác minh lại phiên; tool chưa lưu kết nối."
        raise StudioIssue(message, "needs_login")

    def close(self):
        if self.browser:
            if self.process:
                try:
                    # Closing a Playwright CDP connection only disconnects it.
                    # Ask our dedicated Chrome process to exit gracefully so
                    # its cookie/profile database is actually flushed.
                    self.browser.new_browser_cdp_session().send("Browser.close")
                except Exception:
                    pass
            try:
                self.browser.close()
            except Exception:
                pass
        if self.playwright:
            try:
                self.playwright.stop()
            except Exception:
                pass
        self.browser = self.page = self.playwright = None
        if self.process:
            if self.process.poll() is None:
                try:
                    # Let Chrome flush its own login profile before switching
                    # between visible login and background automation.
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                        self.process.wait(timeout=5)
            self.process = None

    def _goto(self, identifier, section):
        self.page.goto(f"https://studio.youtube.com/video/{identifier}/{section}?hl=en",
                       wait_until="domcontentloaded")
        self._check_url(identifier, section)

    def _check_url(self, identifier, section):
        parsed = urlparse(self.page.url)
        if parsed.hostname in {"accounts.google.com", "consent.youtube.com"}:
            raise StudioIssue("Hãy đăng nhập và chọn đúng kênh trong cửa sổ Studio rồi bấm Tiếp tục.", "needs_login")
        path = parsed.path.rstrip("/")
        valid = (path == f"/video/{identifier}/{section}" or
                 bool(re.fullmatch(rf"/channel/UC[A-Za-z0-9_-]{{22}}/video/{re.escape(identifier)}/{re.escape(section)}", path)))
        if parsed.hostname != "studio.youtube.com" or not valid:
            raise StudioIssue("Studio chưa mở đúng Video ID. Kiểm tra tài khoản, kênh và quyền chỉnh sửa video.")

    @staticmethod
    def _visible(locator):
        return [item for item in locator.all() if item.is_visible()]

    def _button(self, scope, pattern, *, required=True):
        # Prefer accessible native controls; fall back to Studio custom elements.
        for locator in (scope.get_by_role("button", name=pattern),
                        scope.get_by_role("menuitem", name=pattern),
                        scope.locator(CONTROL).filter(has_text=pattern)):
            matches = self._visible(locator)
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                raise StudioIssue("Studio hiện nhiều nút trùng tên. Cần kiểm tra thủ công để tránh chọn nhầm.")
        if required:
            raise StudioIssue("Không tìm thấy nút phù hợp trong Studio. Giao diện có thể đã thay đổi.")
        return None

    def _wait_button(self, scope, pattern):
        end = time.monotonic() + 8
        while time.monotonic() < end:
            button = self._button(scope, pattern, required=False)
            if button is not None:
                return button
            self.page.wait_for_timeout(200)
        raise StudioIssue("Studio chưa hiện nút xác nhận của thao tác đang chọn.")

    def _scope(self):
        for selector in ("ytcr-video-home-dialog", "ytcp-video-copyright", "main", "#main"):
            matches = self._visible(self.page.locator(selector))
            if len(matches) == 1:
                return matches[0]
        return self.page.locator("body")

    def _claim(self, scope):
        for row in self._visible(scope.locator(CLAIM_ROWS)):
            actions = self._visible(row.locator("#action-button"))
            button = actions[0] if len(actions) == 1 else self._button(row, ACTIONS, required=False)
            if button is not None:
                return row, button
        # Older Studio renders no stable row tag. Only accept an action control
        # whose ancestor contains a time range and no second claim's control.
        for button in self._visible(scope.locator("#action-button")):
            row = button
            for _ in range(6):
                row = row.locator("xpath=..")
                text = row.inner_text()
                if len(self._visible(row.locator("#action-button"))) > 1:
                    break
                if re.search(r"\d+:\d{2}(?::\d{2})?\s*[-–—]\s*\d+:\d{2}", text):
                    return row, button
        return None

    def _ended(self, identifier):
        response = self.page.context.request.get(f"https://www.youtube.com/watch?v={identifier}&hl=en", timeout=20000)
        if response.status != 200:
            raise StudioIssue("Chưa đọc được bản lưu YouTube để xác nhận live đã kết thúc.")
        html = response.text()
        match = re.search(r"(?:var\s+ytInitialPlayerResponse\s*=|window\[\"ytInitialPlayerResponse\"\]\s*=)\s*", html)
        try:
            player = json.JSONDecoder().raw_decode(html[match.end():])[0] if match else {}
            details = player["videoDetails"]
            live = player.get("microformat", {}).get("playerMicroformatRenderer", {}).get("liveBroadcastDetails", {})
            if details["videoId"] != identifier:
                raise KeyError("videoId")
            if self.expected_channel_id and details.get("channelId") != self.expected_channel_id:
                raise StudioIssue("Video không thuộc kênh đã kết nối. Kiểm tra Video ID hoặc đăng nhập lại đúng kênh.")
            if live.get("isLiveNow") or (details.get("isLiveContent") and not live.get("endTimestamp")):
                return False
            seconds = int(details.get("lengthSeconds", 0))
            if seconds <= 0:
                return False
            if seconds >= 21600:
                raise StudioIssue("Bản lưu từ 6 giờ trở lên cần kiểm tra khả năng cắt trong YouTube Studio thủ công.")
        except StudioIssue:
            raise
        except (KeyError, ValueError, TypeError):
            raise StudioIssue("Chưa xác nhận được Video ID, thời lượng hoặc thời điểm kết thúc live. Kiểm tra Studio và thử lại.")
        return True

    def inspect(self, identifier):
        self.current_claim = None
        self.connect()
        self._goto(identifier, "copyright")
        if not self._ended(identifier):
            return {"kind": "live"}
        end = time.monotonic() + 15
        while time.monotonic() < end:
            self._check_url(identifier, "copyright")
            scope = self._scope()
            text = scope.inner_text()
            if re.search(r"sign in|đăng nhập", text, re.I) and "accounts.google.com" in str(self.page.content()):
                raise StudioIssue("Cần đăng nhập Studio và chọn đúng kênh.", "needs_login")
            if PROCESSING.search(text):
                return {"kind": "processing"}
            claim = self._claim(scope)
            if claim:
                row, button = claim
                description = " ".join(row.inner_text().split())
                fingerprint = hashlib.sha256(description.encode()).hexdigest()
                self.current_claim = (identifier, fingerprint, row, button)
                if button.is_disabled() or button.get_attribute("disabled") is not None or button.get_attribute("aria-disabled") == "true":
                    return {"kind": "processing"}
                return {"kind": "claim", "fingerprint": fingerprint, "description": description}
            # Generic "No issues found" outside the copyright panel is not a
            # sufficient clean signal (e.g. another Studio check may say it).
            clean = CLEAN.search(text)
            copyright_panel = scope.evaluate("el => el.tagName.toLowerCase()") in {"ytcr-video-home-dialog", "ytcp-video-copyright"}
            if clean and (copyright_panel or re.search(r"copyright|bản quyền", clean.group(0), re.I)):
                return {"kind": "clean"}
            self.page.wait_for_timeout(300)
        raise StudioIssue("Studio chưa xác nhận không có claim và chưa đọc được danh sách claim. Cần kiểm tra giao diện; không công khai video.")

    def trim(self, identifier, fingerprint, *, before_confirm=None):
        self._check_url(identifier, "copyright")
        if not self.current_claim or self.current_claim[:2] != (identifier, fingerprint):
            raise StudioIssue("Claim đã thay đổi. Cần đọc lại trước khi cắt.")
        row, button = self.current_claim[2:]
        current = hashlib.sha256(" ".join(row.inner_text().split()).encode()).hexdigest()
        if current != fingerprint:
            raise StudioIssue("Danh sách claim vừa thay đổi. Chưa chọn thao tác cắt; hãy đọc lại video.")
        button.click()
        # Select by its meaning, never by menu position (e.g. text-item-1).
        self._wait_button(self.page, TRIM).click()
        self._wait_button(self.page, CONTINUE).click()
        # Require the final, dedicated confirmation dialog, not the page's Save.
        confirmation = self.page.locator("#confirm-button")
        visible = self._visible(confirmation)
        end = time.monotonic() + 8
        while not visible and time.monotonic() < end:
            self.page.wait_for_timeout(200)
            visible = self._visible(confirmation)
        self._check_url(identifier, "copyright")
        if len(visible) != 1 or not CONFIRM.fullmatch(visible[0].inner_text().strip()):
            raise StudioIssue("Chưa xác định được nút xác nhận cắt đoạn. Hãy kiểm tra Studio.")
        if before_confirm:
            before_confirm()
        visible[0].click()
        self.current_claim = None

    def _visibility(self):
        end = time.monotonic() + 12
        while time.monotonic() < end:
            # These selectors can identify a parent and its child. Evaluate
            # them in priority order instead of treating both as two videos.
            for selector in ("#visibility ytcp-dropdown-trigger", "ytcp-video-metadata-visibility #select-button", "#visibility #select-button"):
                matches = self._visible(self.page.locator(selector))
                if len(matches) == 1:
                    return matches[0]
                if len(matches) > 1:
                    raise StudioIssue("Không xác định duy nhất phần hiển thị của video.")
            self.page.wait_for_timeout(200)
        raise StudioIssue("Không đọc được chế độ hiển thị của video. Chưa xác nhận Công khai.")

    def publish(self, identifier):
        self._goto(identifier, "edit")
        self._visibility().click()
        public = self.page.locator('tp-yt-paper-radio-button[name="PUBLIC"], ytcp-radio-button[name="PUBLIC"], [role=radio][name="PUBLIC"]')
        public.first.wait_for(state="visible")
        if len(self._visible(public)) != 1:
            raise StudioIssue("Không xác định duy nhất tùy chọn Công khai.")
        public.first.click()
        self._wait_button(self.page, re.compile(r"^(?:done|xong)$", re.I)).click()
        save = self._wait_button(self.page, re.compile(r"^(?:save|lưu)$", re.I))
        self._check_url(identifier, "edit")
        if not save.is_disabled() and save.get_attribute("disabled") is None:
            save.click()
            end = time.monotonic() + 15
            while time.monotonic() < end:
                if save.is_disabled() or save.get_attribute("disabled") is not None or save.get_attribute("aria-disabled") == "true":
                    break
                self.page.wait_for_timeout(200)
            else:
                raise StudioIssue("Studio chưa xác nhận lưu chế độ hiển thị. Chưa coi là Công khai.")
        # Reload from the server; a selected radio alone is not a saved change.
        self._goto(identifier, "edit")
        if not re.fullmatch(r"\s*(?:public|công khai)\s*", self._visibility().inner_text(), re.I):
            raise StudioIssue("Đã yêu cầu công khai nhưng chưa đọc lại được kết quả lưu. Kiểm tra Studio.")
