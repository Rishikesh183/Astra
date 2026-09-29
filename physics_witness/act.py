"""The act step: for claims the footage can't settle, ask the claimant for one
specific extra piece of evidence. The re-judge itself lives in pipeline.rejudge."""

from __future__ import annotations

import json
from typing import Any

from .clients.token_factory import TokenFactoryClient
from .jsonutil import extract_json

DEFAULT_ASK = "Footage from another camera angle, or from a few seconds earlier, covering this moment."

ASK_PROMPT = """You help an insurance adjuster ask a claimant for more evidence. The video could not settle the claims below.

For each claim write one short, specific, polite request for a single piece of evidence that would settle it
(for example "footage from 10 seconds before the collision", "the rear camera angle", "a photo of the rear bumper").
Use the investigator's suggestion when it is specific. Never accuse the claimant or hint at fraud.
Then write a short message to the claimant that lists the requests.

Return only JSON: {{"message": "...", "items": [{{"claim_id": "c1", "ask": "..."}}]}}

Claims and the investigator's suggestions:
{items}"""


def _fallback(pending: list[dict[str, Any]]) -> dict[str, Any]:
    items = [{"claim_id": v["claim_id"], "ask": v.get("needed_evidence") or DEFAULT_ASK} for v in pending]
    lines = "\n".join(f"- {i['ask']} (about: \"{v['claim']}\")" for i, v in zip(items, pending))
    message = ("Thank you for your claim. The video you sent does not show enough to confirm some of what you "
               f"described. If you have them, please send:\n{lines}")
    return {"message": message, "items": items}


def compose_request(client: TokenFactoryClient, model: str, verdicts: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Returns {"message", "items": [{"claim_id", "ask"}]} or None when nothing is pending."""
    pending = [v for v in verdicts if v["verdict"] == "cant_tell"]
    if not pending:
        return None
    fallback = _fallback(pending)
    payload = json.dumps([{"claim_id": v["claim_id"], "claim": v["claim"],
                           "why_unsettled": v.get("reasoning", ""), "suggestion": v.get("needed_evidence", "")}
                          for v in pending], indent=1)
    result = client.chat(model, [{"role": "user", "content": ASK_PROMPT.format(items=payload)}], task="ask",
                         offline_reply=lambda: json.dumps(fallback), temperature=0.2, max_tokens=1024)
    try:
        data = extract_json(result.text)
        asks = {str(i.get("claim_id")): str(i.get("ask", "")).strip() for i in data.get("items", [])}
        items = [{"claim_id": v["claim_id"], "ask": asks.get(v["claim_id"]) or f["ask"]}
                 for v, f in zip(pending, fallback["items"])]
        message = str(data.get("message") or "").strip() or fallback["message"]
        return {"message": message, "items": items}
    except (ValueError, AttributeError, TypeError):
        return fallback
