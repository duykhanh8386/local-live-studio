"""Build a clean offline Windows x64 ZIP. No personal data is copied."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON_VERSION = "3.13.16"
PYTHON_URL = f"https://www.python.org/ftp/python/{PYTHON_VERSION}/python-{PYTHON_VERSION}-embed-amd64.zip"
PYTHON_SHA = "97dae5274cc54867065e8d5a3226e48c35017ed332a0fdb0e27d5b5821961297"


def digest(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def fetch(url, path):
    if path.exists():
        return
    print(f"Downloading {path.name}", flush=True)
    with urllib.request.urlopen(url, timeout=60) as response, path.open("xb") as output:
        shutil.copyfileobj(response, output)


def archive(bundle, output):
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as package:
        for path in sorted(bundle.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                relative = path.relative_to(bundle)
                if relative.parts[0] in {"data", "qa", "tests", "_build", "dist"}:
                    continue
                if relative.as_posix().startswith("runtime/python/Lib/site-packages/bin/"):
                    continue
                package.write(path, Path(bundle.name, relative).as_posix())
    with zipfile.ZipFile(output) as package:
        bad = package.testzip()
        if bad:
            raise RuntimeError(f"Corrupt ZIP member: {bad}")
        if any(Path(name).parts[1] in {"data", "qa"} for name in package.namelist()):
            raise RuntimeError("Unexpected personal/test data in ZIP")
    output.with_suffix(".zip.sha256").write_text(digest(output) + "  " + output.name + "\n", encoding="ascii")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ffmpeg-root", type=Path)
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("Run the packager on Windows x64.")
    ffmpeg_root = args.ffmpeg_root or Path(shutil.which("ffmpeg") or "").resolve().parent.parent
    if not (ffmpeg_root / "bin" / "ffmpeg.exe").is_file() or not (ffmpeg_root / "LICENSE").is_file():
        parser.error("Provide --ffmpeg-root with bin/ffmpeg.exe, bin/ffprobe.exe, LICENSE and README.txt.")
    stamp = datetime.now(timezone(timedelta(hours=7))).strftime("%Y%m%d_%H%M%S")
    cache = ROOT / "_build" / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    bundle = ROOT / "_build" / stamp / "LocalLiveStudio"
    bundle.mkdir(parents=True, exist_ok=False)
    python_root = bundle / "runtime" / "python"
    python_root.mkdir(parents=True)
    python_zip = cache / f"python-{PYTHON_VERSION}-embed-amd64.zip"
    fetch(PYTHON_URL, python_zip)
    if digest(python_zip) != PYTHON_SHA:
        raise RuntimeError("Python archive SHA-256 does not match python.org.")
    with zipfile.ZipFile(python_zip) as package:
        package.extractall(python_root)
    (python_root / "python313._pth").write_text(
        "python313.zip\n.\nLib\\site-packages\n..\\..\n", encoding="ascii")
    print("Vendoring pinned Windows CPython 3.13 dependencies", flush=True)
    subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--no-compile",
        "--only-binary=:all:", "--platform", "win_amd64", "--implementation", "cp", "--python-version", "3.13",
        "--abi", "cp313", "--target", str(python_root / "Lib" / "site-packages"),
        "-r", str(ROOT / "portable" / "requirements-lock.txt")], check=True)
    # Copy an explicit list rather than archiving the project directory.
    for name in ("app.py", "README.md", "requirements.txt", "project.json"):
        shutil.copy2(ROOT / name, bundle / name)
    for folder in ("live", "static"):
        shutil.copytree(ROOT / folder, bundle / folder, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copytree(ROOT / "packaging", bundle / "packaging", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for name in ("portable_entry.py", "START_LIVE.bat", "CHECK_SETUP.bat", "DIAGNOSE_LIVE.bat", "HUONG_DAN.txt", "requirements-lock.txt"):
        source = ROOT / "portable" / name
        if source.suffix == ".bat":
            (bundle / name).write_bytes(source.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
        else:
            shutil.copy2(source, bundle / name)
    shutil.copy2(bundle / "START_LIVE.bat", bundle / "start.bat")
    ffmpeg_bin = bundle / "runtime" / "ffmpeg" / "bin"
    ffmpeg_bin.mkdir(parents=True)
    for name in ("ffmpeg.exe", "ffprobe.exe"):
        shutil.copy2(ffmpeg_root / "bin" / name, ffmpeg_bin / name)
    notices = bundle / "licenses"
    notices.mkdir()
    shutil.copy2(ffmpeg_root / "LICENSE", notices / "FFmpeg-GPL-v3.txt")
    shutil.copy2(ffmpeg_root / "README.txt", notices / "FFmpeg-vendor-README.txt")
    shutil.copy2(python_root / "LICENSE.txt", notices / "Python-LICENSE.txt")
    version = subprocess.run([str(ffmpeg_bin / "ffmpeg.exe"), "-version"], capture_output=True,
        text=True, encoding="utf-8", check=True).stdout
    (notices / "FFmpeg-build-configuration.txt").write_text(version, encoding="utf-8")
    notice = """Bundled third-party components (unmodified binaries/packages)

Python: https://www.python.org/downloads/release/python-31316/
Python documentation/license: https://docs.python.org/3.13/license.html
FFmpeg: GPL v3, Windows static build supplied by https://www.gyan.dev/ffmpeg/builds/
FFmpeg upstream corresponding revision and build details: FFmpeg-vendor-README.txt
FFmpeg source repository: https://github.com/FFmpeg/FFmpeg
FFmpeg legal/source information: https://ffmpeg.org/legal.html

Python dependency licenses and copyright notices are retained in
runtime/python/Lib/site-packages/*dist-info/LICENSE* or *dist-info/licenses/.
Pinned package versions: requirements-lock.txt. Package sources are available
from https://pypi.org/project/<package-name>/<version>/.
FFmpeg runs as a separate process; no FFmpeg binary or library was modified.
"""
    (notices / "THIRD_PARTY_NOTICES.txt").write_text(notice, encoding="utf-8")
    info = {"name":"Local Live Studio Portable", "target":"Windows 10/11 x64",
        "built_at":datetime.now(timezone(timedelta(hours=7))).isoformat(), "python":PYTHON_VERSION,
        "python_url":PYTHON_URL, "python_archive_sha256":PYTHON_SHA,
        "ffmpeg":version.splitlines()[0], "dependencies":(ROOT / "portable" / "requirements-lock.txt").read_text(),
        "personal_data_included":False, "offline_startup":True}
    (bundle / "BUILD_INFO.json").write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    files = {path.relative_to(bundle).as_posix():digest(path) for path in sorted(bundle.rglob("*"))
             if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
             and not path.relative_to(bundle).as_posix().startswith("runtime/python/Lib/site-packages/bin/")}
    (bundle / "BUNDLE_MANIFEST.json").write_text(json.dumps({"algorithm":"sha256", "files":files}, indent=2), encoding="utf-8")
    subprocess.run([str(python_root / "python.exe"), "-B", "-X", "utf8", str(bundle / "portable_entry.py"),
                    "--check", "--verify"], check=True)
    output = ROOT / "dist" / f"LocalLiveStudio_Portable_Windows_x64_{stamp}.zip"
    output.parent.mkdir(exist_ok=True)
    print("Creating and verifying ZIP", flush=True)
    archive(bundle, output)
    report = {"zip":str(output), "bundle":str(bundle), "sha256":digest(output), "bytes":output.stat().st_size}
    (ROOT / "_build" / "last-build.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    main()
