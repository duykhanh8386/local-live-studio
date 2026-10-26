"""Make a self-contained Windows EXE and a commit-bound update manifest."""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSET = "LocalLiveStudio-Windows-x64.exe"


def sha256(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", default=os.environ.get("GITHUB_SHA", ""))
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("Build the Windows EXE on Windows x64.")
    commit = args.commit or subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        parser.error("A full commit SHA is required.")
    project = json.loads((ROOT / "project.json").read_text(encoding="utf-8"))
    vendor = ROOT / "_build" / "ffmpeg"
    if not all((vendor / name).is_file() for name in ("ffmpeg.exe", "ffprobe.exe", "FFmpeg-LICENSE.txt")):
        raise RuntimeError("Run packaging/fetch_ffmpeg.py first.")
    work = ROOT / "_build" / "exe"
    work.mkdir(parents=True, exist_ok=True)
    metadata = work / "BUILD_INFO.json"
    metadata.write_text(json.dumps({"commit": commit, "built_at": datetime.now(timezone.utc).isoformat()}), encoding="utf-8")
    notices = work / "licenses"
    notices.mkdir(exist_ok=True)
    for name in ("FFmpeg-LICENSE.txt", "FFmpeg-vendor-README.txt"):
        shutil.copy2(vendor / name, notices / name)
    (notices / "THIRD_PARTY_NOTICES.txt").write_text(
        "FFmpeg: unmodified GPL v3 vendor build. See included license and vendor README for revision/build information.\n"
        "FFmpeg source: https://github.com/FFmpeg/FFmpeg\nVendor builds: https://www.gyan.dev/ffmpeg/builds/\n"
        "Python license: https://docs.python.org/3/license.html\n"
        "PyInstaller: GPL with bootloader exception; https://pyinstaller.org/en/stable/license.html\n"
        "Application dependencies: requirements.txt and portable/requirements-lock.txt in the source repository.\n"
        "Playwright: Apache-2.0; https://github.com/microsoft/playwright-python\n", encoding="utf-8")
    output = ROOT / "dist" / "windows"
    command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--console",
        "--name", "LocalLiveStudio", "--distpath", str(output), "--workpath", str(work / "pyinstaller"),
        "--specpath", str(work), "--collect-all", "playwright", "--collect-submodules", "uvicorn"]
    for source, target in ((ROOT / "static", "static"), (ROOT / "project.json", "."), (metadata, "."),
                           (ROOT / "packaging" / "install-update.ps1", "packaging"), (notices, "licenses")):
        command += ["--add-data", str(source) + os.pathsep + target]
    for name in ("ffmpeg.exe", "ffprobe.exe"):
        command += ["--add-binary", str(vendor / name) + os.pathsep + "tools"]
    subprocess.run(command + [str(ROOT / "app.py")], check=True, cwd=ROOT)
    binary = output / ASSET
    shutil.copy2(output / "LocalLiveStudio.exe", binary)
    manifest = {"schema": 1, "repository": project["repository"], "commit": commit,
                "version": project["version"], "target": "windows-x64", "file": ASSET,
                "sha256": sha256(binary), "size": binary.stat().st_size}
    (output / "update.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (output / (ASSET + ".sha256")).write_text(manifest["sha256"] + "  " + ASSET + "\n", encoding="ascii")
    archive = output / "LocalLiveStudio-Windows-x64.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as package:
        package.write(binary, "LocalLiveStudio/LocalLiveStudio.exe")
        package.write(ROOT / "README.md", "LocalLiveStudio/README.md")
        for notice in notices.iterdir():
            package.write(notice, "LocalLiveStudio/licenses/" + notice.name)
    (output / (archive.name + ".sha256")).write_text(sha256(archive) + "  " + archive.name + "\n", encoding="ascii")
    notes = (f"Windows 10/11 x64 · Python, thư viện và FFmpeg đã kèm trong EXE.\n\n"
             f"Commit: `{commit}`\n\nTải ZIP để lấy EXE cùng hướng dẫn và giấy phép thành phần. "
             "YouTube Studio cần Chrome hoặc Edge trên máy. Dữ liệu riêng lưu trong `%LOCALAPPDATA%/LocalLiveStudio/data`.\n\n"
             "Mỗi commit mới trên main tự tạo một bản phát hành. Trong tool: Kiểm tra cập nhật → Tải bản mới → Cài & khởi động lại.\n")
    (output / "RELEASE_NOTES.md").write_text(notes, encoding="utf-8")
    print(json.dumps(manifest), flush=True)


if __name__ == "__main__":
    main()
