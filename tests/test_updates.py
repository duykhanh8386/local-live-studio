"""No GitHub writes or actual application replacement in updater unit tests."""
import hashlib
import io
import json
import os
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from live.api import create_app
from live.runtime import data_directory
from live.updates import ASSET, SafeRedirect, UpdateService

REPO = "owner/local-live-studio"
COMMIT = "b" * 40
OLD = "a" * 40
PAYLOAD = b"MZ" + b"synthetic-executable" * 20


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.manifest = dict(schema=1, repository=REPO, commit=COMMIT, version="1.0.0", target="windows-x64",
                             file=ASSET, sha256=hashlib.sha256(PAYLOAD).hexdigest(), size=len(PAYLOAD))
        self.head = {"sha": COMMIT, "commit": {"message": "New version\nDetails"}}
        self.assets = [{"name": name, "size": len(PAYLOAD) if name == ASSET else 100,
                        "browser_download_url": f"https://github.com/{REPO}/releases/download/build-{COMMIT}/{name}"}
                       for name in (ASSET, "update.json")]
        self.release = {"tag_name": "build-" + COMMIT, "draft": False, "assets": self.assets}
        self.requests = []
        self.payload = PAYLOAD
        self.no_release = False
        self.installed = self.folder / "LocalLiveStudio.exe"
        self.installed.write_bytes(b"MZ-old-app")
        self.service = UpdateService(self.folder, info={"repository": REPO, "branch": "main", "version": "1.0.0",
            "commit": OLD, "packaged": True}, opener=self.open, installed_exe=self.installed)

    def tearDown(self):
        self.service.shutdown()
        self.temp.cleanup()

    def open(self, request, **kwargs):
        url = request.full_url
        self.requests.append(request)
        if "/commits/" in url:
            return io.BytesIO(json.dumps(self.head).encode())
        if "/releases/tags/" in url:
            if self.no_release:
                raise urllib.error.HTTPError(url, 404, "Not found", {}, None)
            return io.BytesIO(json.dumps(self.release).encode())
        if url.endswith("/update.json"):
            return io.BytesIO(json.dumps(self.manifest).encode())
        if url.endswith("/" + ASSET):
            return io.BytesIO(self.payload)
        raise AssertionError("Unexpected network request")

    def run_action(self, action):
        self.service.start(action)
        self.service.worker.join(timeout=5)
        self.assertFalse(self.service.worker.is_alive())
        return self.service.snapshot()

    def test_exact_main_commit_and_complete_release_required(self):
        state = self.run_action("check")
        self.assertEqual(state["status"], "available")
        self.assertEqual(state["latest_commit"], COMMIT)
        self.no_release = True
        self.assertEqual(self.run_action("check")["status"], "building")
        self.no_release = False
        self.release["assets"] = []
        self.assertEqual(self.run_action("check")["status"], "building")

    def test_current_build_does_not_download_or_request_release(self):
        self.service.info["commit"] = COMMIT
        self.assertEqual(self.run_action("check")["status"], "current")
        self.assertEqual(len(self.requests), 1)

    def test_download_verifies_size_hash_and_exe_header_before_installable(self):
        state = self.run_action("download")
        self.assertEqual(state["status"], "ready")
        self.assertTrue(state["can_install"])
        self.assertEqual(Path(self.service.prepared["path"]).read_bytes(), PAYLOAD)
        self.assertEqual(self.installed.read_bytes(), b"MZ-old-app")

    def test_corrupt_partial_or_non_exe_never_replaces_current_app(self):
        for content in (PAYLOAD[:-2], b"MZ" + b"z" * (len(PAYLOAD) - 2), b"XX" + PAYLOAD[2:]):
            self.payload = content
            state = self.run_action("download")
            self.assertEqual(state["status"], "failed")
            self.assertFalse(state["can_install"])
            self.assertIsNone(self.service.prepared)
            self.assertEqual(self.installed.read_bytes(), b"MZ-old-app")
            self.assertFalse(list(self.folder.rglob("*.part")))

    def test_foreign_repo_asset_and_manifest_are_rejected(self):
        self.assets[0]["browser_download_url"] = "https://evil.invalid/update.exe"
        self.assertEqual(self.run_action("check")["status"], "failed")
        self.assets[0]["browser_download_url"] = f"https://github.com/{REPO}/releases/download/build-{COMMIT}/{ASSET}"
        self.manifest["commit"] = OLD
        self.assertEqual(self.run_action("check")["status"], "failed")

    def test_source_version_can_check_but_never_replace_python(self):
        self.service.info["packaged"] = False
        self.assertEqual(self.run_action("check")["status"], "available")
        self.assertEqual(self.run_action("download")["status"], "failed")
        self.assertIsNone(self.service.prepared)

    def test_installer_rechecks_latest_commit_and_staged_bytes(self):
        self.run_action("download")
        self.head["sha"] = "c" * 40
        with self.assertRaisesRegex(ValueError, "commit mới hơn"):
            self.service.launch_installer(port=8765, max_streams=4)
        self.head["sha"] = COMMIT
        Path(self.service.prepared["path"]).write_bytes(b"MZ-tampered")
        with self.assertRaisesRegex(ValueError, "đã thay đổi"):
            self.service.launch_installer(port=8765, max_streams=4)

    def test_installer_launches_hidden_helper_with_only_our_paths(self):
        self.run_action("download")
        with patch("live.updates.subprocess.Popen", return_value=Mock(pid=123)) as launch:
            self.assertEqual(self.service.launch_installer(port=8765, max_streams=4), 123)
        arguments = launch.call_args.args[0]
        self.assertEqual(arguments[0], "powershell.exe")
        self.assertEqual(launch.call_args.kwargs["creationflags"], 0x08000000)
        settings = json.loads(Path(arguments[-1]).read_text())
        self.assertEqual(settings["target"], str(self.installed.resolve()))
        self.assertEqual(settings["data_dir"], str(self.folder.resolve()))
        self.assertEqual(self.installed.read_bytes(), b"MZ-old-app")

    def test_tokens_never_appear_in_status_and_are_removed_on_cross_host_redirect(self):
        with patch.dict(os.environ, {"LIVE_GITHUB_TOKEN": "private-token-value"}):
            self.service._json(self.service.api + "/commits/main")
            self.assertEqual(self.requests[0].get_header("Authorization"), "Bearer private-token-value")
            self.assertNotIn("private-token-value", json.dumps(self.service.snapshot()))
            redirected = SafeRedirect().redirect_request(self.requests[0], None, 302, "Found", {}, "https://release-assets.githubusercontent.com/file")
            self.assertIsNone(redirected.get_header("Authorization"))

    def test_update_routes_require_local_auth_and_block_install_during_live(self):
        app = create_app(self.folder / "api", start_scheduler=False)
        with TestClient(app) as client:
            self.assertEqual(client.post("/api/update/check").status_code, 403)
            client.headers["X-Local-Token"] = app.state.token
            self.assertEqual(client.get("/api/update").status_code, 200)
            app.state.request_exit = Mock()
            app.state.engine.runs["running"] = Mock()
            with patch.object(app.state.updater, "launch_installer") as installer:
                response = client.post("/api/update/install")
                self.assertEqual(response.status_code, 400)
                self.assertIn("Dừng các luồng", response.json()["detail"])
                installer.assert_not_called()
            app.state.engine.runs.clear()

    def test_frozen_data_is_outside_bundle(self):
        with patch.dict(os.environ, {"LOCALAPPDATA": str(self.folder)}):
            with patch("live.runtime.sys.frozen", True, create=True):
                saved = os.environ.pop("LIVE_DATA_DIR", None)
                try:
                    self.assertEqual(data_directory(), self.folder / "LocalLiveStudio" / "data")
                finally:
                    if saved is not None:
                        os.environ["LIVE_DATA_DIR"] = saved


if __name__ == "__main__":
    unittest.main()
