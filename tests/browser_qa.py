"""Optional browser smoke test. All media goes to a local file, never YouTube."""
import argparse
import base64
import json
import socket
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import uvicorn
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

from live.api import create_app
from live.media import HIDDEN_PROCESS, executable

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--driver", required=True)
    parser.add_argument("--chrome")
    args = parser.parse_args()
    qa = ROOT / "qa" / "browser"
    qa.mkdir(parents=True, exist_ok=True)
    video = qa / "video mẫu.mp4"
    subprocess.run([executable("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30", "-f", "lavfi", "-i",
        "sine=frequency=440:sample_rate=48000", "-t", "1", "-c:v", "libx264", "-pix_fmt",
        "yuv420p", "-c:a", "aac", str(video)], check=True, capture_output=True, timeout=15,
        **HIDDEN_PROCESS)
    data = qa / f"data-{time.time_ns()}"
    app = create_app(data, output_factory=lambda job, key: str(data / (job["id"] + ".flv")))
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, access_log=False, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    driver = None
    try:
        end = time.monotonic() + 10
        while not server.started:
            if time.monotonic() > end:
                raise RuntimeError("Preview server did not start")
            time.sleep(.1)
        with httpx.Client(base_url=url, headers={"X-Local-Token": app.state.token}) as api:
            assert api.get("/").status_code == 200
            options = webdriver.ChromeOptions()
            for arg in ("--headless=new", "--disable-gpu", "--disable-background-networking",
                        "--no-first-run", "--no-default-browser-check", "--window-size=1440,1100"):
                options.add_argument(arg)
            options.set_capability("goog:loggingPrefs", {"browser": "ALL"})
            if args.chrome:
                options.binary_location = args.chrome
            driver = webdriver.Chrome(service=Service(args.driver), options=options)
            wait = WebDriverWait(driver, 12)
            driver.get(url)
            wait.until(lambda d: "sẵn sàng" in d.find_element(By.ID, "tool-status").text)
            def click(element_id):
                driver.find_element(By.ID, element_id).click()
            def fill(element_id, value):
                field = driver.find_element(By.ID, element_id)
                field.clear()
                field.send_keys(value)
            def set_value(element_id, value):
                driver.execute_script("let e=document.getElementById(arguments[0]);e.value=arguments[1];e.dispatchEvent(new Event('change',{bubbles:true}));", element_id, value)
            def row_count(count):
                wait.until(lambda d: len(d.find_elements(By.CSS_SELECTOR, "#streams-body tr")) == count)
            def action(name):
                driver.find_element(By.CSS_SELECTOR, f"#streams-body [data-action='{name}']").click()
            def snapshot(name):
                driver.execute_async_script("const done=arguments[0];refresh().then(()=>done()).catch(e=>done(e.message));")
                wait.until(lambda d: not d.find_elements(By.CSS_SELECTOR, "#toast-region .toast"))
                driver.execute_script("window.scrollTo(0,0)")
                layout = driver.execute_cdp_cmd("Page.getLayoutMetrics", {})["cssContentSize"]
                png = driver.execute_cdp_cmd("Page.captureScreenshot", {"format":"png", "captureBeyondViewport":True,
                    "clip":{"x":0, "y":0, "width":layout["width"], "height":layout["height"], "scale":1}})
                (ROOT / "qa" / name).write_bytes(base64.b64decode(png["data"]))

            # Choose a local file using the real folder browser and add a schedule.
            fill("stream-name", "Nhạc buổi tối · Kênh 01")
            fill("stream-key", "synthetic-ui-key-1234")
            click("browse")
            fill("folder-path", str(qa))
            click("folder-open")
            wait.until(lambda d: d.find_elements(By.CSS_SELECTOR, ".file-item strong") and
                       any(e.text == video.name for e in d.find_elements(By.CSS_SELECTOR, ".file-item strong")))
            next(e for e in driver.find_elements(By.CSS_SELECTOR, ".file-item strong") if e.text == video.name).click()
            wait.until(lambda d: not d.find_element(By.ID, "file-dialog").get_property("open"))
            click("save-stream")
            row_count(1)
            assert api.get("/api/streams").json()[0]["source_path"] == str(video.resolve())

            # Edit while keeping the stored key; confirm log and search work.
            action("edit")
            fill("stream-name", "Nhạc buổi tối · Kênh 01 (đã sửa)")
            assert driver.find_element(By.ID, "stream-key").get_attribute("value") == ""
            click("save-stream")
            wait.until(lambda d: "đã sửa" in d.find_element(By.ID, "streams-body").text)
            action("logs")
            wait.until(lambda d: "Đã lưu" in d.find_element(By.ID, "logs-content").text)
            driver.find_element(By.CSS_SELECTOR, "#logs-dialog [data-close]").click()
            fill("search", "no-matching-job")
            row_count(0)
            fill("search", "")
            row_count(1)

            # Start via UI, with an injected file sink and a short duration.
            fill("stream-name", "Phiên thử máy phát · hoàn tất")
            fill("stream-key", "synthetic-now-key")
            fill("source-path", str(video))
            fill("duration", "00:00:03")
            driver.find_element(By.CSS_SELECTOR, ".advanced summary").click()
            set_value("resolution", "original")
            set_value("bitrate", "3000")
            click("live-now")
            click("confirm-action")
            row_count(2)
            wait.until(lambda d: "Hoàn tất" in d.find_element(By.ID, "streams-body").text)
            jobs = api.get("/api/streams").json()
            assert any(j["status"] == "completed" and j["metrics"]["elapsed"] > 2 for j in jobs)

            # Add and cancel a 24/7 schedule; a stopped record remains visible.
            click("mode-continuous")
            assert driver.find_element(By.ID, "duration").get_attribute("disabled")
            fill("stream-name", "Không gian cafe · 24/7")
            fill("stream-key", "synthetic-cafe-key")
            fill("source-path", str(video))
            click("save-stream")
            row_count(3)
            continuous = next(j for j in api.get("/api/streams").json() if j["mode"] == "continuous")
            api.post(f"/api/streams/{continuous['id']}/stop").raise_for_status()
            click("refresh")
            wait.until(lambda d: "Đã dừng" in d.find_element(By.ID, "streams-body").text)
            snapshot("desktop.png")
            driver.execute_cdp_cmd("Browser.setDownloadBehavior", {"behavior":"allow", "downloadPath":str(data.resolve())})
            click("export")
            wait.until(lambda d: (data / "lich-livestream.csv").exists())
            assert "synthetic" not in (data / "lich-livestream.csv").read_text(encoding="utf-8-sig")
            driver.set_window_size(390, 844)
            driver.execute_cdp_cmd("Emulation.setDeviceMetricsOverride", {
                "width":390, "height":844, "deviceScaleFactor":1, "mobile":False})
            driver.execute_script("window.scrollTo(0,0)")
            assert driver.execute_script("return document.documentElement.scrollWidth <= innerWidth")
            snapshot("mobile.png")
            driver.execute_cdp_cmd("Emulation.clearDeviceMetricsOverride", {})
            driver.set_window_size(1440, 1100)

            # Filters, cancel delete, confirm delete, clear completed.
            set_value("filter-mode", "continuous")
            row_count(1)
            action("delete")
            driver.find_element(By.CSS_SELECTOR, "#confirm-dialog .button-outline").click()
            row_count(1)
            action("delete")
            click("confirm-action")
            row_count(0)
            set_value("filter-mode", "")
            row_count(2)
            click("clear-done")
            click("confirm-action")
            row_count(1)
            action("delete")
            click("confirm-action")
            row_count(0)
            assert video.exists()
            errors = [e for e in driver.get_log("browser") if e["level"] == "SEVERE"]
            assert not errors, errors
            report = {"result":"passed", "browser":driver.capabilities["browserVersion"],
                      "javascript_errors":errors, "screenshots":["qa/desktop.png", "qa/mobile.png"],
                      "checks":["folder picker", "schedule", "edit", "log", "search", "live now to local file",
                                "24/7 mode", "cancel schedule", "CSV export", "filters", "delete", "clear completed", "mobile layout"]}
            (ROOT / "qa" / "browser-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps(report, ensure_ascii=False))
    finally:
        if driver:
            driver.quit()
        server.should_exit = True
        thread.join(timeout=40)
        if thread.is_alive():
            raise RuntimeError("Preview server did not stop")
        print("Owned preview server stopped.")


if __name__ == "__main__":
    main()
