"""Pull a JSON value out of a model reply that may include reasoning text,
<think> blocks, or markdown fences."""

from __future__ import annotations

import json
import re
from typing import Any

_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def extract_json(text: str) -> Any:
    text = _THINK.sub("", text or "")
    answer = re.search(r"<answer>(.*?)</answer>", text, re.S | re.I)
    if answer:
        text = answer.group(1)
    candidates = [m.group(1) for m in _FENCE.finditer(text)] + [text]
    decoder = json.JSONDecoder()
    for chunk in candidates:
        for i, ch in enumerate(chunk):
            if ch in "{[":
                try:
                    value, _ = decoder.raw_decode(chunk[i:])
                    return value
                except json.JSONDecodeError:
                    continue
    raise ValueError(f"No JSON found in model reply: {text[:200]!r}")
