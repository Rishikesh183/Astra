"""Model routing: which model does which job, plus a cheap frame filter so
Cosmos only reads frames where something changed.

Tasks:
  split  story -> atomic claims        (small text model)
  see    frames -> timed events        (Cosmos, the only vision model)
  judge  claims vs events -> verdicts  (Ultra; Super under the budget policy)
  ask    Can't tell -> evidence request (small text model)
"""

from __future__ import annotations

from PIL import ImageChops, ImageStat

from .frames import Frame

TASKS = {
    "split": "Split story into claims",
    "see": "Read frames",
    "judge": "Cross-examine",
    "ask": "Ask for evidence",
}

# Each task names the model roles to try, in order.
POLICIES = {
    "quality": {"split": ("splitter", "super", "ultra"), "see": ("cosmos",),
                "judge": ("ultra", "super"), "ask": ("splitter", "super", "ultra")},
    "budget": {"split": ("splitter", "super", "ultra"), "see": ("cosmos",),
               "judge": ("super", "ultra"), "ask": ("splitter", "super", "ultra")},
}


def route(names: dict[str, str], policy: str = "quality") -> dict[str, str]:
    table = POLICIES.get(policy, POLICIES["quality"])
    return {task: next((names.get(role) for role in roles if names.get(role)), "") for task, roles in table.items()}


def _small(frame: Frame):
    w, h = frame.image.size
    # Crop off the timestamp band so the changing label doesn't count as motion.
    return frame.image.crop((0, 0, w, int(h * 0.9))).convert("L").resize((96, 54))


def drop_near_duplicates(frames: list[Frame], min_change: float = 1.5) -> tuple[list[Frame], list[Frame]]:
    """Keep the first and last frame, and any frame that differs from the last
    kept one by at least `min_change` (mean absolute pixel difference, 0-255).
    Returns (kept, dropped)."""
    if len(frames) <= 2:
        return list(frames), []
    kept, dropped = [frames[0]], []
    last = _small(frames[0])
    for f in frames[1:-1]:
        cur = _small(f)
        if ImageStat.Stat(ImageChops.difference(last, cur)).mean[0] >= min_change:
            kept.append(f)
            last = cur
        else:
            dropped.append(f)
    kept.append(frames[-1])
    return kept, dropped
