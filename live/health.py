"""Measure recent output, rather than FFmpeg's average since the stream began."""
from collections import deque
import math


class OutputHealth:
    def __init__(self):
        self.samples = deque()
        self.started = None

    def update(self, now: float, elapsed: float) -> dict:
        if not math.isfinite(now) or not math.isfinite(elapsed) or elapsed < 0:
            return {}
        if self.samples and (now < self.samples[-1][0] or elapsed < self.samples[-1][1]):
            self.samples.clear()
            self.started = None
        if self.started is None:
            self.started = now
        self.samples.append((now, elapsed))
        # Retain at least ten seconds of output for a stable recent rate.
        while len(self.samples) > 2 and self.samples[1][0] <= now - 10:
            self.samples.popleft()
        span = now - self.samples[0][0]
        speed = (elapsed - self.samples[0][1]) / span if span > 0 else None
        warning = ""
        if now - self.started >= 15 and span >= 10 and speed is not None and speed < 0.9:
            warning = (f"Phát chậm ({speed:.2f}x trong khoảng 10 giây gần đây), có thể gây giật. "
                       "Kiểm tra CPU/GPU và tốc độ upload; dùng bộ mã hóa GPU phù hợp hoặc giảm bitrate/độ phân giải.")
        return {"realtime_speed": speed, "warning": warning}
