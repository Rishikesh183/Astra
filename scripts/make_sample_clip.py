"""Generate a synthetic 10 s test clip (moving test pattern) with no external assets.

Usage: python scripts/make_sample_clip.py [out.mp4] [seconds]
"""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from physics_witness.frames import ffmpeg_exe  # noqa: E402


def make_clip(out: Path, seconds: float = 10.0) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"testsrc2=duration={seconds}:size=640x360:rate=25",
        "-pix_fmt", "yuv420p", str(out),
    ], check=True)
    return out


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("samples/synthetic.mp4")
    secs = float(sys.argv[2]) if len(sys.argv) > 2 else 10.0
    print(make_clip(target, secs))
