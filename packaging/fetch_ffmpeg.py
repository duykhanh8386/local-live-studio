"""Fetch a pinned vendor build; extract only tools and their notices."""
import hashlib
import json
import shutil
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    config = json.loads((ROOT / "packaging" / "ffmpeg.json").read_text(encoding="utf-8"))
    folder = ROOT / "_build" / "ffmpeg"
    folder.mkdir(parents=True, exist_ok=True)
    archive = folder / "vendor.zip"
    valid = False
    if archive.is_file():
        with archive.open("rb") as handle:
            valid = hashlib.file_digest(handle, "sha256").hexdigest() == config["sha256"]
    if not valid:
        print("Downloading pinned FFmpeg " + config["version"], flush=True)
        with urllib.request.urlopen(config["url"], timeout=60) as response, archive.open("wb") as output:
            shutil.copyfileobj(response, output)
    with archive.open("rb") as handle:
        if hashlib.file_digest(handle, "sha256").hexdigest() != config["sha256"]:
            raise RuntimeError("FFmpeg archive failed SHA-256 verification")
    with zipfile.ZipFile(archive) as package:
        for suffix, name in (("/bin/ffmpeg.exe", "ffmpeg.exe"), ("/bin/ffprobe.exe", "ffprobe.exe"),
                             ("/LICENSE", "FFmpeg-LICENSE.txt"), ("/README.txt", "FFmpeg-vendor-README.txt")):
            members = [member for member in package.namelist() if member.endswith(suffix)]
            if len(members) != 1:
                raise RuntimeError("Missing or ambiguous FFmpeg member: " + suffix)
            with package.open(members[0]) as source, (folder / name).open("wb") as destination:
                shutil.copyfileobj(source, destination)
    print("Verified FFmpeg tools: " + str(folder), flush=True)


if __name__ == "__main__":
    main()
