"""Exercise real startup timing, port ownership and failures before HTTP readiness."""
import asyncio
import os
import socket
import subprocess
import sys
import threading
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

import uvicorn
from fastapi import FastAPI

from app import LocalServer
from live.diagnostics import probe_http

ROOT = Path(__file__).resolve().parents[1]


class StartupTests(unittest.TestCase):
    def run_launcher(self, port, **environment):
        env = os.environ.copy()
        env.update(environment)
        return subprocess.run([sys.executable, "-X", "utf8", str(ROOT / "app.py"),
            "--port", str(port), "--no-browser"], cwd=ROOT, env=env,
            capture_output=True, text=True, encoding="utf-8", timeout=15)

    def test_failed_data_directory_never_announces_ready(self):
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        result = self.run_launcher(port, LIVE_DATA_DIR=str(Path(__file__).resolve()))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("FileExistsError", result.stderr)
        self.assertNotIn("Local Live Studio: http://", result.stdout)
        self.assertNotIn("HTTP 200", result.stdout)

    def test_occupied_port_reports_failure_without_announcing_ready(self):
        with socket.socket() as reservation:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                reservation.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            reservation.bind(("127.0.0.1", 0))
            reservation.listen(1)
            result = self.run_launcher(reservation.getsockname()[1])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("đang được chương trình khác sử dụng", result.stderr)
        self.assertNotIn("HTTP 200", result.stdout)

    def test_browser_waits_for_slow_startup_and_real_http_despite_proxy(self):
        entered, gate, browser_opened = (threading.Event() for _ in range(3))
        observations, failures = [], []

        @asynccontextmanager
        async def lifespan(application):
            entered.set()
            await asyncio.to_thread(gate.wait)
            yield

        application = FastAPI(lifespan=lifespan)

        @application.get("/api/health")
        def health():
            return {"status": "ok", "service": "local-live-studio"}

        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
            server = LocalServer(uvicorn.Config(application, log_config=None, access_log=False), port=port)

            def run():
                try:
                    server.run(sockets=[listener])
                except BaseException as exc:
                    failures.append(exc)

            def open_browser(url):
                observations.append((url, probe_http(port)))
                browser_opened.set()
                return True

            with patch("app.webbrowser.open", side_effect=open_browser) as browser, patch.dict(os.environ,
                {"HTTP_PROXY": "http://127.0.0.1:1", "HTTPS_PROXY": "http://127.0.0.1:1", "NO_PROXY": ""}):
                worker = threading.Thread(target=run, daemon=True)
                worker.start()
                try:
                    self.assertTrue(entered.wait(5))
                    self.assertFalse(server.ready.is_set())
                    browser.assert_not_called()
                    gate.set()
                    self.assertTrue(browser_opened.wait(5))
                    self.assertEqual(observations, [(f"http://127.0.0.1:{port}/",
                        {"status": 200, "service": "local-live-studio"})])
                    browser.assert_called_once()
                    self.assertFalse(server.readiness_error)
                finally:
                    gate.set()
                    server.should_exit = True
                    server.finished.set()
                    worker.join(timeout=5)
                self.assertFalse(worker.is_alive())
                self.assertEqual(failures, [])


if __name__ == "__main__":
    unittest.main()
