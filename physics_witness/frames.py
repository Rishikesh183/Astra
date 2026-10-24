"""Frame sampling: extract candidate frames with ffmpeg, pick the ones with the
most motion, and burn a visible timestamp into the bottom of each frame
(Cosmos reads timestamps from the bottom of frames)."""

from __future__ import annotations

import base64
import io
import re
import shutil
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import imageio_ffmpeg
from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageStat


def ffmpeg_exe() -> str:
    return shutil.which("ffmpeg") or imageio_ffmpeg.get_ffmpeg_exe()


@dataclass
class Frame:
    index: int          # position among candidate frames
    t: float            # seconds from start of clip
    image: Image.Image  # RGB, timestamp already burned in
    motion: float = 0.0
    prefix: str = ""    # "" for the original video, "e1_" etc. for extra evidence

    def to_b64_jpeg(self, quality: int = 85) -> str:
        buf = io.BytesIO()
        self.image.save(buf, format="JPEG", quality=quality)
        return base64.b64encode(buf.getvalue()).decode()

    def label(self) -> str:
        return f"t={self.t:.2f}s"

    @property
    def filename(self) -> str:
        return f"{self.prefix}frame_{self.t:07.2f}s.jpg"


class Cancelled(Exception):
    """Raised when a run is cancelled (the cancel event was set)."""


def check_cancel(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise Cancelled()


_DURATION = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")


def video_duration(path: Path) -> float:
    """Clip length from the container header (instant). Falls back to a full
    decode only when the header has no duration."""
    proc = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", str(path)], capture_output=True, text=True)
    m = _DURATION.search(proc.stderr)
    if m:
        h, mnt, sec = m.groups()
        return int(h) * 3600 + int(mnt) * 60 + float(sec)
    _, secs = imageio_ffmpeg.count_frames_and_secs(str(path))
    return float(secs)


_PTS = re.compile(r"\bn:\s*(\d+)\b.*?\bpts_time:\s*([-\d.]+)")

MAX_CANDIDATES = 120      # candidate frames scored for motion, whatever the clip length
SEEK_ABOVE_S = 90.0       # longer clips: jump to each sample time instead of decoding every frame


def _run_cancellable(cmd: list[str], cancel: threading.Event | None, log_path: Path) -> str:
    """Run ffmpeg, killing it promptly if `cancel` is set. Returns its stderr."""
    with open(log_path, "w", encoding="utf-8") as log:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=log)
        try:
            while proc.poll() is None:
                if cancel is not None and cancel.is_set():
                    proc.kill()
                    proc.wait()
                    raise Cancelled()
                time.sleep(0.05)
        finally:
            if proc.poll() is None:
                proc.kill()
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, cmd, stderr=log_path.read_text(encoding="utf-8", errors="replace")[-2000:])
    return log_path.read_text(encoding="utf-8", errors="replace")


def _decode_all(video: Path, gap: float, width: int, tmp: str, cancel) -> list[tuple[float, Image.Image]]:
    # `select` rather than the `fps` filter, so each frame keeps its true
    # presentation time (read back from `showinfo`), which is what gets burned in.
    vf = f"select='isnan(prev_selected_t)+gte(t-prev_selected_t\\,{gap:.4f})',showinfo,scale={width}:-2"
    cmd = [ffmpeg_exe(), "-hide_banner", "-i", str(video), "-vf", vf, "-fps_mode", "vfr", "-q:v", "3", f"{tmp}/%06d.jpg"]
    stderr = _run_cancellable(cmd, cancel, Path(tmp) / "ffmpeg.log")
    times = {int(n): float(t) for n, t in _PTS.findall(stderr)}
    start = min(times.values(), default=0.0)
    frames = []
    for i, f in enumerate(sorted(Path(tmp).glob("*.jpg"))):
        with Image.open(f) as im:
            frames.append((max(0.0, times.get(i, i * gap) - start), im.convert("RGB")))
    return frames


def _seek_each(video: Path, times: list[float], width: int, tmp: str, cancel) -> list[tuple[float, Image.Image]]:
    """One quick ffmpeg per sample time: seeks to the nearest keyframe and
    decodes forward to `t`, so a 5-minute clip costs ~MAX_CANDIDATES short decodes."""
    def grab(i_t):
        i, t = i_t
        check_cancel(cancel)
        out = Path(tmp) / f"{i:06d}.jpg"
        cmd = [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-ss", f"{t:.3f}", "-i", str(video),
               "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "3", "-y", str(out)]
        _run_cancellable(cmd, cancel, Path(tmp) / f"{i:06d}.log")
        if not out.is_file():
            return None
        with Image.open(out) as im:
            return t, im.convert("RGB")

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(grab, enumerate(times)))
    return [r for r in results if r is not None]


def extract_candidates(video: Path, fps: float = 4.0, width: int = 768, cancel: threading.Event | None = None,
                       max_candidates: int = MAX_CANDIDATES) -> list[tuple[float, Image.Image]]:
    """Candidate frames about 1/`fps` s apart (fewer for long clips, so there are
    never more than `max_candidates`), scaled to `width` px."""
    video = Path(video)
    if not video.is_file():
        raise FileNotFoundError(video)
    duration = video_duration(video)
    eff_fps = min(fps, max_candidates / max(duration, 0.1))
    with tempfile.TemporaryDirectory() as tmp:
        if duration > SEEK_ABOVE_S:
            n = max(2, int(duration * eff_fps))
            last = max(0.0, duration - 0.5)
            times = [round(last * i / (n - 1), 3) for i in range(n)]
            return _seek_each(video, times, width, tmp, cancel)
        return _decode_all(video, 1.0 / eff_fps, width, tmp, cancel)


def motion_scores(images: list[Image.Image]) -> list[float]:
    """Mean absolute pixel change vs. the previous frame (first frame scores 0)."""
    small = [im.convert("L").resize((96, 54)) for im in images]
    scores = [0.0]
    for prev, cur in zip(small, small[1:]):
        scores.append(ImageStat.Stat(ImageChops.difference(prev, cur)).mean[0])
    return scores


SPIKE_RATIO = 2.5   # a frame change this many times the clip's median counts as a jolt or a cut
LEAD_IN = 2         # candidates kept just before each spike: the moments leading up to an impact


def select_indices(scores: list[float], max_frames: int, min_gap: int = 1) -> list[int]:
    """Choose which candidate frames the vision model sees, in time order.

    Picking purely by motion fails on dashcam footage: driving is always
    "high motion", and a camera knocked by an impact or a cut to other
    footage scores highest of all, so the half-second before an impact could
    be skipped. So, in priority order:
      1. first and last frame;
      2. each big spike (jolt or cut) plus the LEAD_IN frames just before it;
      3. an even spread over the whole clip (about half the budget);
      4. the remaining budget by motion, at least `min_gap` candidates apart.
    """
    n = len(scores)
    if n <= max_frames:
        return list(range(n))
    chosen = {0, n - 1}

    def add(i: int) -> None:
        if 0 <= i < n and len(chosen) < max_frames:
            chosen.add(i)

    ordered = sorted(range(1, n), key=lambda i: scores[i], reverse=True)
    rest = sorted(scores[1:])
    median = rest[len(rest) // 2] if rest else 0.0
    if median > 0:
        spikes = [i for i in ordered if scores[i] >= SPIKE_RATIO * median][: max(1, max_frames // 5)]
        for i in spikes:
            for j in range(i, i - LEAD_IN - 1, -1):
                add(j)

    spread = max(2, max_frames // 2)
    for k in range(spread):
        add(round(k * (n - 1) / (spread - 1)))

    for i in ordered:
        if all(abs(i - c) > min_gap for c in chosen):
            add(i)
    for i in ordered:                     # low-motion clips may not fill the budget under the gap rule
        add(i)
    return sorted(chosen)


def burn_timestamp(image: Image.Image, t: float) -> Image.Image:
    """Add a black band under the frame with the timestamp in white."""
    w, h = image.size
    band = max(24, h // 12)
    out = Image.new("RGB", (w, h + band), "black")
    out.paste(image, (0, 0))
    draw = ImageDraw.Draw(out)
    try:
        font = ImageFont.load_default(size=int(band * 0.7))
    except TypeError:  # Pillow < 10.1
        font = ImageFont.load_default()
    draw.text((10, h + band * 0.12), f"t={t:.2f}s", fill="white", font=font)
    return out


def sample_frames(video: Path, max_frames: int = 16, fps: float = 4.0, width: int = 768,
                  prefix: str = "", cancel: threading.Event | None = None) -> list[Frame]:
    candidates = extract_candidates(video, fps=fps, width=width, cancel=cancel)
    if not candidates:
        raise ValueError(f"No frames decoded from {video}")
    scores = motion_scores([im for _, im in candidates])
    keep = select_indices(scores, max_frames=max_frames)
    return [
        Frame(index=i, t=candidates[i][0], image=burn_timestamp(candidates[i][1], candidates[i][0]), motion=scores[i],
              prefix=prefix)
        for i in keep
    ]


def windows(frames: list[Frame], size: int = 6, overlap: int = 1) -> list[list[Frame]]:
    """Sliding windows that respect the per-request image limit (4-8 per Nebius doc)."""
    if size < 1 or overlap >= size:
        raise ValueError("need size >= 1 and overlap < size")
    if len(frames) <= size:
        return [frames]
    out, start, step = [], 0, size - overlap
    while start < len(frames):
        out.append(frames[start:start + size])
        if start + size >= len(frames):
            break
        start += step
    return out


def save_frames(frames: list[Frame], out_dir: Path) -> list[Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for f in frames:
        p = out_dir / f.filename
        f.image.save(p, quality=90)
        paths.append(p)
    return paths
