"""Real dedicated Chrome profile, synthetic cookie, no YouTube navigation."""
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from live.studio import StudioBrowser

ROOT = Path(__file__).resolve().parents[1]


class StudioProfileTests(unittest.TestCase):
    def test_switching_to_background_preserves_session_and_closes_owned_chrome(self):
        qa = ROOT / "qa"
        qa.mkdir(exist_ok=True)
        original_popen = subprocess.Popen
        modes = []

        def isolated_launch(arguments, *args, **kwargs):
            if isinstance(arguments, list) and any(str(arg).startswith("--remote-debugging-port=") for arg in arguments):
                modes.append("--headless=new" in arguments)
                # Force both modes invisible in QA; replace the startup URL so
                # the real profile test never opens YouTube/Google.
                arguments = arguments[:-1] + ["--headless=new", "--disable-background-networking", "about:blank"]
            return original_popen(arguments, *args, **kwargs)

        with tempfile.TemporaryDirectory(prefix="studio-profile-test-", dir=qa) as folder:
            self.assertTrue(Path(folder).resolve().is_relative_to(qa.resolve()))
            driver = StudioBrowser(Path(folder) / "profile")
            try:
                with patch("live.studio.subprocess.Popen", side_effect=isolated_launch):
                    driver.connect()
                    first_process = driver.process
                    driver.page.context.add_cookies([dict(name="session_fixture", value="synthetic-local-session",
                        url="https://studio.youtube.com", expires=time.time() + 3600)])
                    driver.use_saved_session("UC" + "a" * 22)
                    self.assertIsNotNone(first_process.poll())
                    driver.connect()
                    cookies = driver.page.context.cookies("https://studio.youtube.com")
                    self.assertTrue(any(c["name"] == "session_fixture" and c["value"] == "synthetic-local-session" for c in cookies))
                    self.assertEqual(driver.expected_channel_id, "UC" + "a" * 22)
                    second_process = driver.process
                    driver.close()
                    self.assertIsNotNone(second_process.poll())
                    self.assertEqual(modes, [False, True])
            finally:
                driver.close()


if __name__ == "__main__":
    unittest.main()
