"""Real Chromium against intercepted Studio pages; all external requests blocked."""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from playwright.sync_api import sync_playwright

from live.studio import StudioBrowser, StudioIssue, chrome_path

VIDEO = "Abc123_-XYZ"
CHANNEL = "UC" + "a" * 22
CLAIM = '''<!doctype html><html><body><main>
<ytcr-video-claim data-claim-id="first"><span>Song One · 0:10–0:30</span>
<button id="action-button" onclick="document.querySelector('#menu').hidden=false">Select action</button>
</ytcr-video-claim>
<div id="menu" hidden><button onclick="document.querySelector('#continue-button').hidden=false">Trim out segment</button></div>
<button id="continue-button" hidden onclick="document.querySelector('#confirm-button').hidden=false">Continue</button>
<button id="confirm-button" hidden onclick="document.body.dataset.confirmed='yes'; document.querySelector('main').innerHTML='Video is being processed'">Confirm</button>
</main></body></html>'''


class StudioBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(executable_path=chrome_path(), headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.context = self.browser.new_context()
        self.page = self.context.new_page()
        self.driver = StudioBrowser(Path("unused-mocked-profile"))
        self.driver.page = self.page
        self.driver.browser = self.browser
        self.driver.connect = lambda: None
        self.driver._ended = lambda identifier: True
        self.html = CLAIM
        self.saved_public = False
        self.login_channel = CHANNEL
        self.dashboard_context = CHANNEL
        self.request_context = False

        def intercept(route):
            url = route.request.url
            if url.rstrip("/") == "https://studio.youtube.com":
                route.fulfill(content_type="text/html", body=f'''<ytcp-app style="display:block">Studio dashboard</ytcp-app>
                    <script>history.replaceState(null,'','/channel/{self.login_channel}/dashboard');
                    window.ytcfg={{get:key=>key==='CHANNEL_ID'?'{self.dashboard_context}':null}};
                    if ({str(self.request_context).lower()}) fetch('/youtubei/v1/fixture',{{method:'POST',headers:{{'Content-Type':'application/json'}},
                    body:JSON.stringify({{context:{{user:{{delegationContext:{{externalChannelId:'{CHANNEL}'}}}}}}}})}});
                    </script>''')
            elif "/youtubei/v1/fixture" in url:
                route.fulfill(content_type="application/json", body='{"ok":true}')
            elif url.startswith(f"https://studio.youtube.com/channel/{self.login_channel}/"):
                route.fulfill(content_type="text/html", body=f'''<ytcp-app style="display:block">Studio dashboard</ytcp-app>
                    <script>window.ytcfg={{get:key=>key==='CHANNEL_ID'?'{self.dashboard_context}':null}};</script>''')
            elif url.startswith("https://accounts.google.com/"):
                route.fulfill(content_type="text/html", body="<main>Sign in</main>")
            elif "/persist-visibility" in url:
                self.saved_public = True
                route.fulfill(status=200, body="ok")
            elif f"/video/{VIDEO}/edit" in url:
                selected = "Public" if self.saved_public else "Private"
                route.fulfill(content_type="text/html", body=f'''<main>
                <div id="visibility"><ytcp-dropdown-trigger onclick="document.querySelector('#options').hidden=false"><span id="select-button">{selected}</span></ytcp-dropdown-trigger></div>
                <div id="options" hidden><ytcp-radio-button name="PUBLIC" role="radio" onclick="document.body.dataset.chosen='PUBLIC'">Public</ytcp-radio-button><button onclick="document.querySelector('#options').hidden=true">Done</button></div>
                <button onclick="if(document.body.dataset.chosen==='PUBLIC') fetch('/persist-visibility').then(()=>this.disabled=true)">Save</button>
                </main>''')
            elif url.startswith(f"https://studio.youtube.com/video/{VIDEO}/copyright"):
                route.fulfill(content_type="text/html", body=self.html)
            else:
                route.abort()

        self.context.route("**/*", intercept)

    def tearDown(self):
        self.context.close()

    def test_claim_trim_processing_and_explicit_clean(self):
        state = self.driver.inspect(VIDEO)
        self.assertEqual(state["kind"], "claim")
        self.assertIn("0:10", state["description"])
        self.driver.trim(VIDEO, state["fingerprint"])
        self.assertEqual(self.page.locator("body").get_attribute("data-confirmed"), "yes")
        self.html = "<main>Video is being processed</main>"
        self.assertEqual(self.driver.inspect(VIDEO)["kind"], "processing")
        self.html = "<main>No copyright claims</main>"
        self.assertEqual(self.driver.inspect(VIDEO)["kind"], "clean")

    def test_never_select_an_unrelated_action_or_confirm(self):
        self.html = CLAIM.replace("Trim out segment", "Dispute")
        state = self.driver.inspect(VIDEO)
        with self.assertRaises(StudioIssue):
            self.driver.trim(VIDEO, state["fingerprint"])
        self.assertIsNone(self.page.locator("body").get_attribute("data-confirmed"))

    def test_wrong_video_and_disabled_actions_are_rejected(self):
        self.html = CLAIM.replace('id="action-button"', 'id="action-button" disabled')
        self.assertEqual(self.driver.inspect(VIDEO)["kind"], "processing")
        with self.assertRaises(StudioIssue):
            self.driver.trim("Other12_-XY", "unknown")

    def test_publish_requires_a_saved_public_state_after_reload(self):
        self.driver.publish(VIDEO)
        self.assertTrue(self.saved_public)
        self.assertEqual(self.page.locator("ytcp-dropdown-trigger").inner_text(), "Public")

    def test_claim_wins_over_clean_label_elsewhere_on_page(self):
        self.html = CLAIM.replace("<main>", "<main><p>No issues found</p>")
        self.assertEqual(self.driver.inspect(VIDEO)["kind"], "claim")

    def test_changed_claim_is_not_trimmed_using_stale_row(self):
        state = self.driver.inspect(VIDEO)
        self.page.locator('ytcr-video-claim span').evaluate("el => el.textContent='A different song · 1:00–2:00'")
        with self.assertRaises(StudioIssue):
            self.driver.trim(VIDEO, state["fingerprint"])
        self.assertIsNone(self.page.locator("body").get_attribute("data-confirmed"))

    def test_verify_login_requires_matching_dashboard_and_channel_context(self):
        self.assertEqual(self.driver.verify_login(timeout=1), {"channel_id": CHANNEL})
        self.dashboard_context = "UC" + "b" * 22
        with self.assertRaises(StudioIssue):
            self.driver.verify_login(timeout=.3)

    def test_expired_login_is_reported_without_account_actions(self):
        goto = self.page.goto
        with patch.object(self.page, "goto", side_effect=lambda *args, **kwargs: goto("https://accounts.google.com/login", **kwargs)):
            with self.assertRaises(StudioIssue) as error:
                self.driver.verify_login(timeout=1)
        self.assertEqual(error.exception.status, "needs_login")

    def test_verify_login_can_read_channel_from_successful_studio_request(self):
        self.dashboard_context = ""
        self.request_context = True
        self.assertEqual(self.driver.verify_login(timeout=2), {"channel_id": CHANNEL})

    def test_visible_login_and_background_modes_are_distinct(self):
        with patch.object(self.driver, "close") as close:
            self.driver.use_saved_session(CHANNEL)
            self.assertTrue(self.driver.background)
            self.assertEqual(self.driver.expected_channel_id, CHANNEL)
            close.assert_called_once()
            self.driver.open_login()
            self.assertFalse(self.driver.background)
            self.assertEqual(close.call_count, 2)


if __name__ == "__main__":
    unittest.main()
