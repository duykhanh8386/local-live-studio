"""Archive tests never sign in to, edit, or publish an actual YouTube video."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from live.api import create_app
from live.archives import ArchiveQueue, video_id
from live.storage import Store
from live.studio import StudioBrowser, StudioIssue

VIDEO = "Abc123_-XYZ"


class FakeStudio:
    def __init__(self, states=None):
        self.states = list(states or [{"kind": "live"}])
        self.trims = []
        self.publications = []
        self.fail_trim = False

    def inspect(self, identifier):
        return self.states.pop(0) if len(self.states) > 1 else self.states[0]

    def trim(self, identifier, fingerprint, *, before_confirm=None):
        if before_confirm:
            before_confirm()
        self.trims.append((identifier, fingerprint))
        if self.fail_trim:
            raise RuntimeError("Connection lost after Confirm")

    def publish(self, identifier):
        self.publications.append(identifier)

    def open_login(self):
        pass

    def verify_login(self):
        return {"channel_id": "UC" + "a" * 22}

    def use_saved_session(self, channel_id):
        self.channel_id = channel_id
        self.background = True

    def close(self):
        pass


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name))
        self.browser = FakeStudio()
        self.queue = ArchiveQueue(self.store, lambda: self.browser, interval=0)

    def tearDown(self):
        self.queue.shutdown()
        self.store.close()
        self.temp.cleanup()

    def task(self):
        return self.store.archives()[0]

    def test_video_id_accepts_only_exact_youtube_hosts_and_ids(self):
        for value in (VIDEO, f"https://youtu.be/{VIDEO}", f"https://studio.youtube.com/video/{VIDEO}/copyright",
                      f"https://www.youtube.com/live/{VIDEO}", f"https://www.youtube.com/watch?v={VIDEO}&t=2"):
            self.assertEqual(video_id(value), VIDEO)
        for value in ("abc", f"https://youtube.com.evil.invalid/watch?v={VIDEO}",
                      f"https://evil.invalid/video/{VIDEO}", "javascript:alert(1)"):
            with self.assertRaises(ValueError):
                video_id(value)

    def test_login_pauses_jobs_then_saves_and_restores_background_session(self):
        self.browser.states = [dict(kind="claim", fingerprint="first")]
        self.queue.enqueue(VIDEO)
        self.queue.open_login()
        self.queue._session_commands()
        self.queue.tick()
        self.assertEqual(self.task()["cuts"], 0)
        self.queue.finish_login()
        self.queue._session_commands()
        state = self.queue.snapshot()
        self.assertTrue(state["session"]["connected"])
        self.assertTrue(state["session"]["background"])
        self.assertFalse(state["login_pending"])
        self.queue.tick()
        self.assertEqual(self.browser.trims, [(VIDEO, "first")])
        restored = ArchiveQueue(self.store)
        browser = restored.factory()
        self.assertTrue(browser.background)
        self.assertEqual(browser.expected_channel_id, "UC" + "a" * 22)

    def test_failed_login_does_not_resume_jobs_or_expose_session_secrets(self):
        self.queue.enqueue(VIDEO)
        self.queue.open_login()
        self.queue._session_commands()
        self.queue.finish_login()
        with patch.object(self.browser, "verify_login", side_effect=StudioIssue("Not signed in", "needs_login")):
            self.queue._session_commands()
        self.queue.tick()
        self.assertTrue(self.queue.login_pending)
        self.assertFalse(self.queue.session["connected"])
        self.assertEqual(self.task()["status"], "queued")
        self.queue.finish_login()
        with patch.object(self.browser, "verify_login", return_value={
                "channel_id": "UC" + "a" * 22, "cookies": "synthetic-private-value", "token": "synthetic-private-value"}):
            self.queue._session_commands()
        self.assertNotIn("synthetic-private-value", json.dumps(self.queue.snapshot()))
        self.assertNotIn("synthetic-private-value", json.dumps(self.store.setting("studio_session")))

    def test_changed_channel_in_background_is_not_connected(self):
        self.queue.open_login()
        self.queue._session_commands()
        self.queue.finish_login()
        with patch.object(self.browser, "verify_login", side_effect=[
                {"channel_id": "UC" + "a" * 22}, {"channel_id": "UC" + "b" * 22}]):
            self.queue._session_commands()
        self.assertFalse(self.queue.session["connected"])
        self.assertTrue(self.queue.login_pending)

    def test_cancel_during_session_verification_prevents_saving_and_resuming(self):
        self.queue.enqueue(VIDEO)
        self.queue.open_login()
        self.queue._session_commands()
        self.queue.finish_login()

        def verify_then_cancel():
            self.queue.abort_login()
            return {"channel_id": "UC" + "a" * 22}

        with patch.object(self.browser, "verify_login", side_effect=verify_then_cancel):
            self.queue._session_commands()
        self.queue.tick()
        self.assertFalse(self.queue.session["connected"])
        self.assertEqual(self.store.setting("studio_session"), {})
        self.assertEqual(self.task()["status"], "queued")
        self.assertTrue(self.queue.login_pending)
        self.queue._session_commands()
        self.assertFalse(self.queue.login_pending)

    def test_login_expiry_and_cancel_then_reconnect_preserve_pending_cut(self):
        self.queue.enqueue(VIDEO)
        task = self.task()
        task.update(pending_claim="submitted-claim", cuts=1)
        self.store.save_archive(task)
        self.queue.session.update(connected=True, background=True)
        with patch.object(self.browser, "inspect", side_effect=StudioIssue("Expired", "needs_login")):
            self.queue.tick()
        self.assertEqual(self.task()["status"], "needs_login")
        self.assertFalse(self.store.setting("studio_session")["connected"])
        self.queue.open_login()
        self.queue._session_commands()
        self.queue.abort_login()
        self.queue._session_commands()
        self.assertFalse(self.queue.login_pending)
        self.assertEqual(self.task()["status"], "needs_login")
        with self.assertRaises(ValueError) as err:
            self.queue.retry(self.task()["id"])
        self.assertIn("Chưa kết nối phiên YouTube Studio", str(err.exception))
        self.queue.open_login()
        self.queue._session_commands()
        self.queue.finish_login()
        self.queue._session_commands()
        self.assertEqual(self.task()["status"], "queued")
        self.assertEqual(self.task()["pending_claim"], "submitted-claim")
        self.assertEqual(self.task()["cuts"], 1)

    def test_opening_login_during_trim_prevents_confirmation(self):
        self.browser.states = [dict(kind="claim", fingerprint="first")]
        self.queue.enqueue(VIDEO)

        def login_before_confirmation(*args, before_confirm):
            self.queue.open_login()
            before_confirm()

        with patch.object(self.browser, "trim", side_effect=login_before_confirmation):
            self.queue.tick()
        self.assertEqual(self.task()["cuts"], 0)
        self.assertIsNone(self.task()["pending_claim"])
        self.assertEqual(self.task()["status"], "queued")

    def test_trim_wait_verify_then_publish(self):
        self.browser.states = [dict(kind="claim", fingerprint="first", description="Song 0:10–0:30"),
                               dict(kind="processing"), dict(kind="clean"), dict(kind="clean"), dict(kind="clean")]
        self.queue.enqueue(VIDEO)
        self.queue.tick()
        self.assertEqual(self.task()["status"], "processing")
        self.assertFalse(self.browser.publications)
        self.queue.tick()
        self.queue.tick()
        self.assertEqual(self.task()["status"], "verifying")
        self.assertFalse(self.browser.publications)
        self.queue.tick()
        self.assertEqual(self.task()["status"], "completed")
        self.assertEqual(self.browser.trims, [(VIDEO, "first")])
        self.assertEqual(self.browser.publications, [VIDEO])

    def test_live_processing_and_unknown_are_never_success(self):
        self.browser.states = [dict(kind="live"), dict(kind="processing"), dict(kind="unknown")]
        self.queue.enqueue(VIDEO)
        for expected in ("waiting_live", "processing", "needs_review"):
            self.queue.tick()
            self.assertEqual(self.task()["status"], expected)
        self.assertFalse(self.browser.trims)
        self.assertFalse(self.browser.publications)

    def test_pending_confirmation_survives_restart_and_is_not_sent_twice(self):
        self.browser.states = [dict(kind="claim", fingerprint="same")]
        self.browser.fail_trim = True
        self.queue.enqueue(VIDEO)
        self.queue.tick()
        self.assertEqual(self.task()["pending_claim"], "same")
        self.assertEqual(self.task()["status"], "needs_review")
        self.store.close()
        self.store = Store(Path(self.temp.name))
        self.queue = ArchiveQueue(self.store, lambda: self.browser, interval=0)
        self.queue.retry(self.task()["id"])
        self.browser.fail_trim = False
        self.queue.tick()
        self.assertEqual(len(self.browser.trims), 1)
        self.assertEqual(self.task()["status"], "needs_review")

    def test_multiple_claims_and_unchanged_claim_after_processing(self):
        self.browser.states = [dict(kind="claim", fingerprint="a"), dict(kind="processing"),
                               dict(kind="claim", fingerprint="b"), dict(kind="processing"),
                               dict(kind="claim", fingerprint="b")]
        self.queue.enqueue(VIDEO)
        for _ in range(5):
            self.queue.tick()
        self.assertEqual(self.browser.trims, [(VIDEO, "a"), (VIDEO, "b")])
        self.assertEqual(self.task()["status"], "needs_review")
        self.assertFalse(self.browser.publications)

    def test_new_claim_before_publish_cancels_publication(self):
        self.browser.states = [dict(kind="clean"), dict(kind="clean"), dict(kind="claim", fingerprint="new")]
        self.queue.enqueue(VIDEO)
        self.queue.tick()
        self.queue.tick()
        self.assertEqual(self.task()["status"], "queued")
        self.assertFalse(self.browser.publications)

    def test_failure_before_confirmation_can_be_retried_without_false_pending_cut(self):
        self.browser.states = [dict(kind="claim", fingerprint="same")]
        with patch.object(self.browser, "trim", side_effect=StudioIssue("Trim menu unavailable")):
            self.queue.enqueue(VIDEO)
            self.queue.tick()
        self.assertEqual(self.task()["cuts"], 0)
        self.assertIsNone(self.task()["pending_claim"])
        self.queue.retry(self.task()["id"])
        self.queue.tick()
        self.assertEqual(self.task()["cuts"], 1)
        self.assertEqual(self.task()["status"], "processing")

    def test_keep_visibility_duplicate_cancel_and_timeout(self):
        self.browser.states = [dict(kind="clean")]
        task = self.queue.enqueue(VIDEO, publish_after=False)
        self.assertEqual(self.queue.enqueue(VIDEO)["id"], task["id"])
        self.queue.tick()
        self.queue.tick()
        self.assertEqual(self.task()["status"], "completed")
        self.assertFalse(self.browser.publications)
        second = self.queue.enqueue("Def123_-XYZ")
        self.queue.cancel(second["id"])
        self.queue.tick()
        self.assertEqual(self.store.archive(second["id"])["status"], "cancelled")
        self.queue.retry(second["id"])
        self.queue.tick(now=second["deadline"] + 90000)
        self.assertEqual(self.store.archive(second["id"])["status"], "needs_review")

    def test_auto_task_created_only_on_normal_finish_or_manual_stop(self):
        job = dict(id="local", trim_after_live=True, youtube_video_id=VIDEO, publish_after_trim=True)
        for status in ("streaming", "failed", "interrupted", "missed"):
            self.queue.after_stream(job, status)
        self.assertFalse(self.store.archives())
        self.queue.after_stream(job, "stopped")
        self.assertEqual(self.task()["stream_id"], "local")
        self.queue.after_stream(job, "completed")
        self.assertEqual(len(self.store.archives()), 1)

    def test_api_consent_auth_duplicate_and_logs(self):
        with tempfile.TemporaryDirectory() as folder:
            app = create_app(Path(folder), start_scheduler=False, studio_factory=lambda: FakeStudio())
            with TestClient(app) as client:
                body = dict(video=VIDEO, publish_after=True, accept_permanent_cut=True)
                self.assertEqual(client.post("/api/archives", json=body).status_code, 403)
                client.headers["X-Local-Token"] = app.state.token
                self.assertEqual(client.post("/api/archives", json={**body, "accept_permanent_cut": False}).status_code, 400)
                result = client.post("/api/archives", json=body)
                self.assertEqual(result.status_code, 201)
                task = result.json()
                self.assertEqual(client.post("/api/archives", json=body).json()["id"], task["id"])
                self.assertEqual(client.post(f"/api/archives/{task['id']}/cancel").status_code, 200)
                self.assertTrue(client.get(f"/api/archives/{task['id']}/logs").json())
                self.assertEqual(client.get("/api/archives").json()["tasks"][0]["status"], "cancelled")

    def test_session_api_requires_local_auth_and_explicit_login_completion(self):
        with tempfile.TemporaryDirectory() as folder:
            browser = FakeStudio()
            app = create_app(Path(folder), start_scheduler=False, studio_factory=lambda: browser)
            with TestClient(app) as client:
                for path in ("/api/studio/open", "/api/studio/connect", "/api/studio/cancel-login"):
                    self.assertEqual(client.post(path).status_code, 403)
                client.headers["X-Local-Token"] = app.state.token
                self.assertEqual(client.post("/api/studio/connect").status_code, 400)
                self.assertEqual(client.post("/api/studio/open").status_code, 200)
                self.assertEqual(client.post("/api/studio/connect").status_code, 200)
                import time
                end = time.monotonic() + 3
                while time.monotonic() < end:
                    state = client.get("/api/archives").json()
                    if state["session"]["connected"]:
                        break
                    time.sleep(.02)
                self.assertTrue(state["session"]["connected"])
                self.assertTrue(state["session"]["background"])
                self.assertEqual(state["session"]["channel_id"], "UC" + "a" * 22)
                self.assertNotIn("cookies", state["session"])
                self.assertNotIn("token", state["session"])


class MetadataTests(unittest.TestCase):
    def inspect_metadata(self, details, live, channel=""):
        browser = StudioBrowser(Path("unused"))
        browser.expected_channel_id = channel
        player = dict(videoDetails=details, microformat={"playerMicroformatRenderer": {"liveBroadcastDetails": live}})
        html = "var ytInitialPlayerResponse = " + json.dumps(player) + ";"
        browser.page = SimpleNamespace(context=SimpleNamespace(request=SimpleNamespace(
            get=lambda *a, **kw: SimpleNamespace(status=200, text=lambda: html))))
        return browser._ended(VIDEO)

    def test_ended_live_and_missing_metadata(self):
        details = dict(videoId=VIDEO, isLiveContent=True, lengthSeconds="3600")
        self.assertTrue(self.inspect_metadata(details, {"endTimestamp": "2026-10-26T12:00:00Z"}))
        self.assertFalse(self.inspect_metadata(details, {"isLiveNow": True}))
        self.assertFalse(self.inspect_metadata(details, {}))
        with self.assertRaises(StudioIssue):
            self.inspect_metadata({}, {})
        with self.assertRaisesRegex(StudioIssue, "6 giờ"):
            self.inspect_metadata({**details, "lengthSeconds": "21600"}, {"endTimestamp": "end"})
        with self.assertRaises(StudioIssue):
            self.inspect_metadata({**details, "videoId": "other"}, {"endTimestamp": "end"})

    def test_video_must_belong_to_connected_channel(self):
        channel = "UC" + "a" * 22
        details = dict(videoId=VIDEO, channelId=channel, isLiveContent=True, lengthSeconds="60")
        self.assertTrue(self.inspect_metadata(details, {"endTimestamp": "end"}, channel))
        with self.assertRaisesRegex(StudioIssue, "không thuộc kênh"):
            self.inspect_metadata(details, {"endTimestamp": "end"}, "UC" + "b" * 22)


if __name__ == "__main__":
    unittest.main()
