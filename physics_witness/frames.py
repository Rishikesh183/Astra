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

    def to_b64_jpeg(self, quality: int = 85) -> str:
        buf = io.BytesIO()
        self.image.save(buf, format="JPEG", quality=quality)
        return base64.b64encode(buf.getvalue()).decode()

    def label(self) -> str:
        return f"t={self.t:.2f}s"


def video_duration(path: Path) -> float:
    _, secs = imageio_ffmpeg.count_frames_and_secs(str(path))
    return float(secs)


_PTS = re.compile(r"\bn:\s*(\d+)\b.*?\bpts_time:\s*([-\d.]+)")


def extract_candidates(video: Path, fps: float = 4.0, width: int = 768) -> list[tuple[float, Image.Image]]:
    """Decode real source frames spaced at least 1/`fps` s apart, scaled to `width` px.

    Uses `select` rather than the `fps` filter so each frame keeps its true
    presentation time (read back from `showinfo`), which is what gets burned in.
    """
    video = Path(video)
    if not video.is_file():
        raise FileNotFoundError(video)
    gap = 1.0 / fps
    vf = f"select='isnan(prev_selected_t)+gte(t-prev_selected_t\\,{gap:.4f})',showinfo,scale={width}:-2"
    with tempfile.TemporaryDirectory() as tmp:
        cmd = [
            ffmpeg_exe(), "-hide_banner", "-i", str(video), "-vf", vf,
            "-fps_mode", "vfr", "-q:v", "3", f"{tmp}/%06d.jpg",
        ]
        proc = subprocess.run(cmd, check=True, capture_output=True, text=True)
        times = {int(n): float(t) for n, t in _PTS.findall(proc.stderr)}
        files = sorted(Path(tmp).glob("*.jpg"))
        start = min(times.values(), default=0.0)
        frames = []
        for i, f in enumerate(files):
            with Image.open(f) as im:
                frames.append((max(0.0, times.get(i, i * gap) - start), im.convert("RGB")))
    return frames


def motion_scores(images: list[Image.Image]) -> list[float]:
    """Mean absolute pixel change vs. the previous frame (first frame scores 0)."""
    small = [im.convert("L").resize((96, 54)) for im in images]
    scores = [0.0]
    for prev, cur in zip(small, small[1:]):
        scores.append(ImageStat.Stat(ImageChops.difference(prev, cur)).mean[0])
    return scores


def select_indices(scores: list[float], max_frames: int, min_gap: int = 1) -> list[int]:
    """Always keep first and last frame, then fill with highest-motion frames
    at least `min_gap` candidates apart. Returned in time order."""
    n = len(scores)
    if n <= max_frames:
        return list(range(n))
    chosen = {0, n - 1}
    by_motion = sorted(range(n), key=lambda i: scores[i], reverse=True)
    for i in by_motion:
        if len(chosen) >= max_frames:
            break
        if all(abs(i - c) > min_gap for c in chosen):
            chosen.add(i)
    # Low-motion clips may not fill the budget under the gap rule.
    for i in by_motion:
        if len(chosen) >= max_frames:
            break
        chosen.add(i)
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


def sample_frames(video: Path, max_frames: int = 16, fps: float = 4.0, width: int = 768) -> list[Frame]:
    candidates = extract_candidates(video, fps=fps, width=width)
    if not candidates:
        raise ValueError(f"No frames decoded from {video}")
    scores = motion_scores([im for _, im in candidates])
    keep = select_indices(scores, max_frames=max_frames)
    return [
        Frame(index=i, t=candidates[i][0], image=burn_timestamp(candidates[i][1], candidates[i][0]), motion=scores[i])
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
        p = out_dir / f"frame_{f.t:07.2f}s.jpg"
        f.image.save(p, quality=90)
        paths.append(p)
    return paths
