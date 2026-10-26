"""Publish only CI-built files for the exact current main commit."""
import json
import hashlib
import os
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path


def main():
    repo, commit = os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_SHA"]
    token = os.environ["GITHUB_TOKEN"]
    directory = Path(os.environ.get("RELEASE_DIRECTORY", "dist/windows"))
    manifest = json.loads((directory / "update.json").read_text(encoding="utf-8"))
    if manifest["repository"] != repo or manifest["commit"] != commit:
        raise RuntimeError("CI artifact does not belong to this repository/commit")
    binary = directory / manifest["file"]
    if not binary.is_file():
        with zipfile.ZipFile(directory / "LocalLiveStudio-Windows-x64.zip") as package:
            with package.open("LocalLiveStudio/LocalLiveStudio.exe") as source, binary.open("wb") as destination:
                import shutil
                shutil.copyfileobj(source, destination)
    with binary.open("rb") as handle:
        if binary.stat().st_size != manifest["size"] or hashlib.file_digest(handle, "sha256").hexdigest() != manifest["sha256"]:
            raise RuntimeError("CI executable failed size/SHA-256 verification")
    base = "https://api.github.com/repos/" + repo

    def request(url, method="GET", payload=None, binary=False):
        data = payload if binary else json.dumps(payload).encode() if payload is not None else None
        headers = {"Authorization": "Bearer " + token, "User-Agent": "LocalLiveStudio-release",
                   "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if data is not None:
            headers["Content-Type"] = "application/octet-stream" if binary else "application/json"
        with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers, method=method), timeout=180) as response:
            return json.load(response) if response.status != 204 else None

    if request(base + "/commits/main")["sha"] != commit:
        print("A newer main commit exists; skipping publication of the older run.")
        return
    tag = "build-" + commit
    files = [manifest["file"], manifest["file"] + ".sha256", "update.json",
             "LocalLiveStudio-Windows-x64.zip", "LocalLiveStudio-Windows-x64.zip.sha256"]
    try:
        release = request(base + "/releases/tags/" + tag)
        if not release["draft"] and set(files).issubset({asset["name"] for asset in release["assets"]}):
            print("This commit already has a complete published release.")
            return
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        release = request(base + "/releases", "POST", {"tag_name": tag, "target_commitish": commit,
            "name": "Local Live Studio · " + commit[:8], "draft": True,
            "body": (directory / "RELEASE_NOTES.md").read_text(encoding="utf-8")})
    existing = {asset["name"]: asset["id"] for asset in release["assets"]}
    for name in files:
        if name in existing:
            request(base + "/releases/assets/" + str(existing[name]), "DELETE")
        upload = release["upload_url"].split("{")[0] + "?name=" + urllib.parse.quote(name)
        request(upload, "POST", (directory / name).read_bytes(), binary=True)
        print("Uploaded " + name, flush=True)
    request(base + "/releases/" + str(release["id"]), "PATCH", {"draft": False, "make_latest": "true"})
    print(release["html_url"], flush=True)


if __name__ == "__main__":
    main()
