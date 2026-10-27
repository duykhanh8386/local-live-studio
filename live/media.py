from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import quote, urlsplit

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".ts"}
HIDDEN_PROCESS = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def executable(name: str) -> str:
    value = os.environ.get(f"LIVE_{name.upper()}") or shutil.which(name)
    if not value or not Path(value).is_file():
        raise ValueError(f"Chưa tìm thấy {name}. Cài FFmpeg hoặc đặt LIVE_{name.upper()}.")
    return str(value)


def local_path(value: str) -> Path:
    value = value.strip()
    if not value.strip() or value.startswith(("\\\\", "//")):
        raise ValueError("Hãy chọn đường dẫn trên máy này.")
    return Path(value).expanduser().resolve()


def probe_video(value: str) -> dict:
    path = local_path(value)
    if not path.is_file() or path.suffix.lower() not in VIDEO_EXTENSIONS:
        raise ValueError("File video không tồn tại hoặc định dạng chưa được hỗ trợ.")
    try:
        result = subprocess.run([
            executable("ffprobe"), "-v", "error", "-show_streams", "-show_format",
            "-of", "json", str(path),
        ], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
            **HIDDEN_PROCESS)
    except subprocess.TimeoutExpired:
        raise ValueError("Quá thời gian đọc video. Kiểm tra ổ đĩa và thử lại.") from None
    except OSError:
        raise ValueError("Không chạy được FFprobe. Kiểm tra đường dẫn FFprobe.") from None
    if result.returncode:
        raise ValueError("FFprobe không đọc được file video; hãy kiểm tra file nguồn.")
    try:
        data = json.loads(result.stdout)
        seconds = float(data.get("format", {}).get("duration", 0))
    except (ValueError, TypeError):
        raise ValueError("Không đọc được thông tin thời lượng video.") from None
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    audio = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    if video is None or seconds <= 0:
        raise ValueError("File cần có hình ảnh và thời lượng hợp lệ.")
    return {
        "path": str(path), "name": path.name, "size": path.stat().st_size,
        "duration": seconds,
        "width": video.get("width", 0), "height": video.get("height", 0),
        "video_codec": video["codec_name"], "has_audio": audio is not None,
        "audio_codec": audio.get("codec_name", "") if audio else "",
    }


def destination(server: str, key: str) -> str:
    parsed = urlsplit(server)
    if parsed.scheme not in {"rtmp", "rtmps"} or not parsed.hostname:
        raise ValueError("Địa chỉ máy chủ phải bắt đầu bằng rtmp:// hoặc rtmps://.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Dùng URL máy chủ không chứa tài khoản, key hoặc tham số.")
    if not key.strip() or any(c.isspace() for c in key):
        raise ValueError("Stream key không được trống hoặc chứa khoảng trắng.")
    return server.rstrip("/") + "/" + quote(key.strip(), safe="-_.~")


def redact(message: str, key: str = "") -> str:
    if key:
        message = message.replace(key, "[stream key đã ẩn]").replace(
            quote(key, safe="-_.~"), "[stream key đã ẩn]"
        )
    return re.sub(r"rtmps?://[^\s\]\"']+", "[đích phát đã ẩn]", message)


def build_command(job: dict, media: dict, output: str, remaining: float | None) -> list[str]:
    # Keep decoder/filter pools bounded when several streams share the machine.
    # The old fixed two-thread encoder could not use an otherwise idle CPU.
    cores = os.cpu_count() or 2
    encode_threads = max(1, min(8, cores // 2))
    decode_threads = max(1, min(4, cores // 4))
    command = [executable("ffmpeg"), "-hide_banner", "-loglevel", "warning",
               "-nostats", "-progress", "pipe:1", "-stats_period", "1",
               "-filter_threads", str(min(2, encode_threads)),
               "-re", "-stream_loop", "-1", "-threads:v", str(decode_threads),
               "-i", media["path"]]
    if not media["has_audio"]:
        command += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
    command += ["-map", "0:v:0", "-map", "0:a:0" if media["has_audio"] else "1:a:0"]
    if job["encoder"] == "copy":
        if media["video_codec"] != "h264" or media["audio_codec"] not in {"", "aac", "mp3"}:
            raise ValueError("Chế độ Copy cần video H.264 và audio AAC/MP3. Chọn CPU để mã hóa file này.")
        command += ["-c:v", "copy"]
        command += ["-c:a", "copy"] if media["has_audio"] else ["-c:a", "aac", "-b:a", "128k"]
    else:
        height = {"720p": 720, "1080p": 1080}.get(job["resolution"])
        scale = f"scale=-2:{height}" if height else "scale=trunc(iw/2)*2:trunc(ih/2)*2"
        pixel_format = "nv12" if job["encoder"] == "h264_qsv" else "yuv420p"
        command += ["-vf", scale, "-r", "30", "-c:v", job["encoder"],
                    "-pix_fmt", pixel_format, "-g", "60", "-b:v", f'{job["bitrate"]}k',
                    "-maxrate", f'{job["bitrate"]}k', "-bufsize", f'{job["bitrate"] * 2}k']
        if job["encoder"] == "libx264":
            command += ["-preset", "veryfast", "-tune", "zerolatency", "-threads:v", str(encode_threads),
                        "-sc_threshold", "0", "-x264-params", "nal-hrd=cbr:force-cfr=1"]
        elif job["encoder"] == "h264_qsv":
            # Software decoding accepts all supported source codecs; encoding is
            # offloaded to Intel Quick Sync (including Arc) at the selected size/rate.
            command += ["-preset", "veryfast", "-look_ahead", "0", "-async_depth", "4",
                        "-scenario", "livestreaming", "-forced_idr", "1", "-bf", "0"]
        else:
            command += ["-preset", "p4", "-rc", "cbr"]
        command += ["-c:a", "aac", "-threads:a", "1", "-b:a", "128k", "-ar", "48000", "-ac", "2"]
    if remaining is not None:
        command += ["-t", f"{max(0.1, remaining):.3f}"]
    return command + ["-f", "flv", "-flvflags", "no_duration_filesize", "-rw_timeout", "15000000", output]
