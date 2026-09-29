"""Draft altered stories: change exactly one physical detail in one sentence.

With a Token Factory key a Nemotron model writes the alteration; offline (or if
the model's answer is unusable) simple word-swap rules are used. Every draft
still needs a human read before the Liar Test counts it."""

from __future__ import annotations

import re

from ..claims import split_sentences
from ..clients.token_factory import TokenFactoryClient
from ..jsonutil import extract_json

SWAPS = {
    "direction": [("left", "right"), ("north", "south"), ("east", "west"), ("forward", "backward"),
                  ("forwards", "backwards"), ("ahead", "behind"), ("towards", "away from"), ("uphill", "downhill"),
                  ("reversed", "drove forward"), ("reversing", "driving forward")],
    "time_of_day": [("day", "night"), ("daytime", "night-time"), ("morning", "evening"), ("afternoon", "night"),
                    ("daylight", "darkness"), ("dark", "bright"), ("noon", "midnight"), ("sunny", "dark")],
    "weather": [("raining", "dry"), ("rain", "sunshine"), ("wet", "dry"), ("snow", "clear weather"),
                ("foggy", "clear"), ("fog", "clear skies"), ("sunny", "raining"), ("clear", "foggy")],
    "contact": [("rear", "front"), ("front", "rear"), ("bumper", "door"), ("door", "bumper"),
                ("driver's side", "passenger side"), ("passenger side", "driver's side"), ("hit", "missed")],
    "presence": [("a pedestrian", "no pedestrian"), ("no one", "a cyclist"), ("empty", "busy"),
                 ("another car", "no other car")],
}

ACTION = re.compile(r"\b(hit|hits|braked|brakes|braking|stopped|stops|turned|turns|pulled|reversed|reversing|crashed|"
                    r"drove|moved|rolled|cut|swerved|collided|struck|overtook|merged|accelerated|slowed|"
                    r"changed lanes|ran|crossed|entered)\b", re.I)

LLM_PROMPT = """You help build a test set for a tool that checks insurance claim stories against dashcam video.

Rewrite the story below so that exactly ONE sentence changes exactly ONE physical detail of type "{kind}":
- direction: left/right, towards/away, forwards/reversing
- order: which event happened first
- time_of_day: day/night, dawn/dusk
- weather: rain/dry, fog/clear
- contact: where the vehicles touched
- presence: someone or something that was or wasn't there
The change must be something a camera could show. Keep every other sentence word for word.

Return only JSON: {{"altered_story": "...", "altered_sentence": "<the changed sentence, exactly as it appears>", "what_changed": "..."}}

Story:
\"\"\"{story}\"\"\""""


def _swap_word(sentence: str, a: str, b: str) -> str | None:
    pattern = re.compile(rf"\b{re.escape(a)}\b", re.I)
    if not pattern.search(sentence):
        return None

    def repl(m: re.Match) -> str:
        return b.capitalize() if m.group(0)[0].isupper() else b
    return pattern.sub(repl, sentence, count=1)


def rule_alter(story: str, kind: str) -> tuple[str, str] | None:
    """(altered_story, altered_sentence), or None if no rule applies."""
    sentences = split_sentences(story)
    if kind == "order":
        # Swap the first two adjacent sentences that both describe an action;
        # swapping an event with scene-setting ("it was sunny") is not an order lie.
        for i in range(len(sentences) - 1):
            if ACTION.search(sentences[i]) and ACTION.search(sentences[i + 1]):
                swapped = sentences[:i] + [sentences[i + 1], sentences[i]] + sentences[i + 2:]
                return " ".join(swapped), sentences[i + 1]
        return None
    for idx, sentence in enumerate(sentences):
        for a, b in SWAPS.get(kind, []):
            for x, y in ((a, b), (b, a)):
                new = _swap_word(sentence, x, y)
                if new and new != sentence:
                    out = sentences[:idx] + [new] + sentences[idx + 1:]
                    return " ".join(out), new
    return None


def llm_alter(client: TokenFactoryClient, model: str, story: str, kind: str) -> tuple[str, str] | None:
    result = client.chat(model, [{"role": "user", "content": LLM_PROMPT.format(kind=kind, story=story)}],
                         task="alter", offline_reply=lambda: "{}", temperature=0.4, max_tokens=1024)
    try:
        data = extract_json(result.text)
        altered, sentence = str(data["altered_story"]).strip(), str(data["altered_sentence"]).strip()
    except (ValueError, KeyError, TypeError, AttributeError):
        return None
    norm = lambda s: re.sub(r"\s+", " ", s.strip().lower())
    changed = [s for s in split_sentences(altered) if norm(s) not in {norm(t) for t in split_sentences(story)}]
    # Accept only a genuine one-sentence change that the model named correctly.
    if not altered or norm(altered) == norm(story) or len(changed) != 1 or norm(changed[0]) != norm(sentence):
        return None
    return altered, changed[0]


def alter(story: str, kind: str, client: TokenFactoryClient | None = None, model: str = "") -> tuple[str, str, str] | None:
    """(altered_story, altered_sentence, method) where method is "model" or "rules"."""
    if client is not None and not client.offline and model and kind != "order":
        got = llm_alter(client, model, story, kind)
        if got:
            return (*got, "model")
    got = rule_alter(story, kind)
    return (*got, "rules") if got else None

