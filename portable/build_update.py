"""Create a small update for an existing, known portable ZIP without touching data."""
import argparse
import hashlib
import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", type=Path, required=True)
    args = parser.parse_args()
    build = json.loads((ROOT / "_build" / "last-build.json").read_text(encoding="utf-8"))
    bundle = Path(build["bundle"])
    with zipfile.ZipFile(args.base) as base:
        previous = json.loads(base.read("LocalLiveStudio/BUNDLE_MANIFEST.json"))
    current = json.loads((bundle / "BUNDLE_MANIFEST.json").read_text(encoding="utf-8"))
    changed = [name for name, sha in current["files"].items() if previous["files"].get(name) != sha]
    if set(previous["files"]) - set(current["files"]):
        raise RuntimeError("This update format cannot remove existing files.")
    names = changed + ["BUNDLE_MANIFEST.json"]
    stamp = bundle.parent.name
    output = ROOT / "dist" / f"LocalLiveStudio_CapNhatKhoiDong_{stamp}.zip"
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as package:
        for name in names:
            source = (bundle / name).resolve()
            if not source.is_relative_to(bundle) or Path(name).parts[0] == "data":
                raise RuntimeError(f"Invalid update member: {name}")
            package.write(source, name)
        package.writestr("HUONG_DAN_CAP_NHAT.txt", """CẬP NHẬT KHỞI ĐỘNG LOCAL LIVE STUDIO

Gói này dùng với bản LocalLiveStudio_Portable_Windows_x64_20261010_161456.zip.
Không dùng riêng; cần có thư mục runtime của bản portable đã giải nén.

1. Dừng live và đóng cửa sổ START_LIVE.bat của bản cũ.
2. Giải nén NỘI DUNG ZIP cập nhật vào đúng thư mục có START_LIVE.bat
   và thư mục runtime. Chọn Replace / Ghi đè các file chương trình.
3. Chạy START_LIVE.bat. Chờ [OK] Máy chủ đã sẵn sàng (HTTP 200).
4. Nếu web vẫn không mở, chạy DIAGNOSE_LIVE.bat trong cùng thư mục.
   Kết quả nằm trong data\\diagnostic.txt; lỗi khởi động ở data\\startup.log.

Gói cập nhật không chứa và không ghi đè data, video, lịch hoặc stream key.
Nếu chưa có bản portable đầy đủ, hãy dùng ZIP đầy đủ được gửi kèm.
""".encode("utf-8"))
    with zipfile.ZipFile(output) as package:
        assert package.testzip() is None
        assert all(Path(name).parts[0] != "data" for name in package.namelist())
    sha = digest(output)
    output.with_suffix(".zip.sha256").write_text(f"{sha}  {output.name}\n", encoding="ascii")
    report = {"zip":str(output), "base_zip":str(args.base), "full_zip":build["zip"],
              "bytes":output.stat().st_size, "sha256":sha, "changed_files":names}
    (ROOT / "_build" / "last-update.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
