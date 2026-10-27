"""All credentials/cookies are synthetic; no real account or Tuất DB is read."""
import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from live.api import create_app
from live.archives import ArchiveQueue
from live.storage import Store
from live.studio import StudioIssue
from live.tuat_session import list_channels, load_channel_session, normalize_cookies
from tests.test_archives import FakeStudio, VIDEO

CHANNEL = "UC" + "a" * 22
PASSWORD = "synthetic-password-not-a-real-account"


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name))
        self.addCleanup(self.store.close)
        self.browser = FakeStudio()
        self.browser.login = Mock()
        self.browser.import_session = Mock()
        self.queue = ArchiveQueue(self.store, lambda: self.browser)
        self.addCleanup(self.queue.shutdown)

    def test_credentials_pause_queue_then_are_discarded_before_session_confirmation(self):
        self.queue.enqueue(VIDEO)
        self.queue.start_login("fixture@example.test", PASSWORD)
        with self.assertRaises(ValueError):
            self.queue.start_login("fixture@example.test", PASSWORD)
        self.queue._session_commands()
        self.browser.login.assert_called_once()
        self.assertEqual(self.browser.login.call_args.args, ("fixture@example.test", PASSWORD))
        self.assertIsNone(self.queue.auth_command)
        self.queue.tick()
        self.assertTrue(self.queue.login_pending)
        self.assertEqual(self.store.archives()[0]["status"], "queued")
        self.queue.finish_login()
        self.queue._session_commands()
        self.assertTrue(self.queue.snapshot()["session"]["connected"])
        self.assertNotIn(PASSWORD, json.dumps(self.queue.snapshot()))
        self.assertNotIn(PASSWORD, json.dumps(self.store.setting("studio_session")))

    def test_cancel_or_shutdown_discards_queued_password_without_using_it(self):
        self.queue.start_login("fixture@example.test", PASSWORD)
        self.queue.abort_login()
        self.queue._session_commands()
        self.assertIsNone(self.queue.auth_command)
        self.assertFalse(self.queue.login_pending)
        self.browser.login.assert_not_called()
        self.queue.start_login("fixture@example.test", PASSWORD)
        self.queue.shutdown()
        self.assertIsNone(self.queue.auth_command)

    def test_browser_failure_does_not_expose_password_or_mark_session_connected(self):
        for error in (RuntimeError(PASSWORD), StudioIssue("Rejected " + PASSWORD, "needs_login")):
            with self.subTest(error_type=type(error).__name__):
                self.queue.open_login()
                self.queue.abort_login()
                self.queue._session_commands()
                self.browser.login.side_effect = error
                self.queue.start_login("fixture@example.test", PASSWORD)
                self.queue._session_commands()
                self.assertNotIn(PASSWORD, json.dumps(self.queue.snapshot()))
                self.assertFalse(self.queue.session["connected"])
                self.assertTrue(self.queue.login_pending)
                self.assertIsNone(self.queue.auth_command)

    def test_selected_tuat_channel_must_match_both_browser_verifications(self):
        for actual in (CHANNEL, "UC" + "b" * 22):
            with self.subTest(actual=actual):
                self.queue.session["connected"] = False
                self.queue.abort_login()
                self.queue._session_commands()
                self.queue.import_tuat_channel(CHANNEL)
                with patch("live.archives.load_channel_session", return_value=[{"name": "SAPISID", "value": PASSWORD}]), \
                     patch.object(self.browser, "verify_login", return_value={"channel_id": actual}):
                    self.queue._session_commands()
                self.assertEqual(self.queue.session["connected"], actual == CHANNEL)
                if actual != CHANNEL:
                    self.queue.finish_login()
                    with patch.object(self.browser, "verify_login", return_value={"channel_id": actual}):
                        self.queue._session_commands()
                    self.assertFalse(self.queue.session["connected"])
                    self.assertEqual(self.queue.expected_login_channel, CHANNEL)
                self.assertNotIn(PASSWORD, json.dumps(self.queue.snapshot()))
                self.assertNotIn(PASSWORD, json.dumps(self.store.setting("studio_session")))


class TuatDatabaseTests(unittest.TestCase):
    def test_read_only_import_returns_metadata_only_and_normalizes_selected_cookies(self):
        with tempfile.TemporaryDirectory(prefix="tuat-auth-fixture-") as folder:
            database = Path(folder) / "channels.db"
            raw = [{"domain": ".youtube.com", "name": "SAPISID", "value": PASSWORD,
                    "sameSite": "unspecified", "secure": True, "expirationDate": time.time() + 3600},
                   {"domain": "youtube.com.attacker.invalid", "name": "other", "value": PASSWORD}]
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE channels(id TEXT, name TEXT, cookies_json TEXT)")
                connection.execute("INSERT INTO channels VALUES(?,?,?)", (CHANNEL, "Fixture channel", json.dumps(raw)))
            connection.close()
            before = database.read_bytes()
            with patch.dict(os.environ, {"LIVE_TUAT_CHANNELS_DB": str(database)}):
                channels = list_channels()
                self.assertEqual(channels, [{"id": CHANNEL, "name": "Fixture channel"}])
                self.assertNotIn(PASSWORD, json.dumps(channels))
                cookies = load_channel_session(CHANNEL)
                self.assertEqual(len(cookies), 1)
                self.assertEqual(cookies[0]["sameSite"], "None")
                self.assertEqual(cookies[0]["domain"], ".youtube.com")
                with self.assertRaises(StudioIssue):
                    load_channel_session("UC" + "b" * 22)
            self.assertEqual(database.read_bytes(), before)

    def test_missing_database_is_not_created_and_expired_cookie_cannot_connect(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / "missing.db"
            with patch.dict(os.environ, {"LIVE_TUAT_CHANNELS_DB": str(database)}):
                self.assertEqual(list_channels(), [])
                with self.assertRaises(StudioIssue):
                    load_channel_session(CHANNEL)
                self.assertFalse(database.exists())
        with self.assertRaises(StudioIssue):
            normalize_cookies([{"name": "SAPISID", "value": PASSWORD, "domain": ".youtube.com", "expiry": 100}], now=200)


class AuthApiTests(unittest.TestCase):
    def test_auth_routes_require_local_token_and_never_return_password(self):
        with tempfile.TemporaryDirectory() as folder:
            app = create_app(Path(folder), start_scheduler=False, studio_factory=FakeStudio)
            with TestClient(app) as client:
                credentials = {"email": "fixture@example.test", "password": PASSWORD}
                for path, body in (("login", credentials), ("import-tuat", {"channel_id": CHANNEL})):
                    self.assertEqual(client.post("/api/studio/" + path, json=body).status_code, 403)
                self.assertEqual(client.get("/api/studio/tuat-channels").status_code, 403)
                headers = {"X-Local-Token": client.get("/api/system").json()["token"]}
                with patch.object(app.state.archives, "start_login") as login:
                    response = client.post("/api/studio/login", json=credentials, headers=headers)
                    self.assertEqual(response.status_code, 200)
                    login.assert_called_once_with("fixture@example.test", PASSWORD)
                    self.assertNotIn(PASSWORD, response.text)
                with patch("live.api.list_tuat_channels", return_value=[{"id": CHANNEL, "name": "Fixture"}]):
                    self.assertEqual(client.get("/api/studio/tuat-channels", headers=headers).json()[0]["id"], CHANNEL)
                response = client.post("/api/studio/login", json={**credentials, "email": "x"}, headers=headers)
                self.assertEqual(response.status_code, 422)
                self.assertNotIn(PASSWORD, response.text)
                self.assertEqual(app.state.archives.store.setting("studio_session"), {})


if __name__ == "__main__":
    unittest.main()
