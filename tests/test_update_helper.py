"""Exercise the real Windows helper against fixtures, never the running tool."""
import hashlib
import http.server
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == "nt", "Windows update helper")
class UpdateHelperTests(unittest.TestCase):
    def setUp(self):
        qa = ROOT / "qa"
        qa.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="update-helper-", dir=qa)
        self.folder = Path(self.temp.name).resolve()
        self.assertTrue(self.folder.is_relative_to(qa.resolve()))
        self.stage = self.folder / "staging có dấu"
        self.install = self.folder / "tool's folder"
        self.stage.mkdir()
        self.install.mkdir()
        self.target = self.install / "LocalLiveStudio.exe"
        self.target.write_bytes(b"keep-old-executable")
        self.data = self.folder / "data"
        self.data.mkdir()
        (self.data / "keep.txt").write_text("keep-private-data", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def run_helper(self, source, expected_hash, port):
        config = self.stage / "install.json"
        config.write_text(json.dumps(dict(pid=2147483000, parent_pid=2147483001, source=str(source),
            target=str(self.target), sha256=expected_hash, commit="b" * 40, port=port,
            max_streams=4, data_dir=str(self.data))), encoding="utf-8")
        return subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", str(ROOT / "packaging" / "install-update.ps1"), "-Config", str(config)],
            capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)

    def test_bad_checksum_never_replaces_exe_or_private_data(self):
        source = self.stage / "new.exe"
        source.write_bytes(b"different-file")
        result = self.run_helper(source, "0" * 64, 8765)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.target.read_bytes(), b"keep-old-executable")
        self.assertEqual((self.data / "keep.txt").read_text(), "keep-private-data")

    def test_real_helper_replaces_exe_preserves_backup_and_restarts(self):
        source = self.stage / "new.exe"
        fixture_code = '''using System; using System.IO; using System.Diagnostics; using System.Threading;
        public class Fixture { public static void Main(string[] args) {
          File.WriteAllText("fixture.pid", Process.GetCurrentProcess().Id.ToString()); Thread.Sleep(4000);
        }}'''
        code = self.stage / "fixture.cs"
        code.write_text(fixture_code, encoding="utf-8")
        compile_script = self.stage / "compile.ps1"
        compile_script.write_text("param($Source,$Output)\nAdd-Type -TypeDefinition (Get-Content -LiteralPath $Source -Raw) -OutputType ConsoleApplication -OutputAssembly $Output\n", encoding="utf-8")
        subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
            str(compile_script), "-Source", str(code), "-Output", str(source)],
            check=True, capture_output=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)

        class Health(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"service": "local-live-studio", "commit": "b" * 40}).encode())

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Health)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            expected = hashlib.sha256(source.read_bytes()).hexdigest()
            result = self.run_helper(source, expected, server.server_address[1])
            log = self.stage / "install.log"
            diagnostic = (result.stdout + result.stderr).decode(errors="replace")
            if log.is_file():
                diagnostic += "\n" + log.read_text(encoding="utf-8", errors="replace")
            self.assertEqual(result.returncode, 0, diagnostic)
            self.assertEqual(hashlib.sha256(self.target.read_bytes()).hexdigest(), expected)
            self.assertEqual(Path(str(self.target) + ".previous").read_bytes(), b"keep-old-executable")
            self.assertEqual((self.data / "keep.txt").read_text(), "keep-private-data")
            deadline = time.monotonic() + 5
            marker = self.install / "fixture.pid"
            while not marker.is_file() and time.monotonic() < deadline:
                time.sleep(.05)
            self.assertTrue(marker.is_file())
            pid = int(marker.read_text())
            subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                "Wait-Process -Id " + str(pid) + " -Timeout 10 -ErrorAction SilentlyContinue"],
                capture_output=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
