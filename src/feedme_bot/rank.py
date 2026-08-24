"""Tournament ranking: candidates -> Safe Pick / Mood Pick.

The model is only ever allowed to choose IDs from the candidate list we
hand it — it must not regenerate item names/prices itself. Real money is
on the other end of this, so the result is validated against the actual
candidate set before anything downstream trusts it (see plan notes on
ranking hallucination risk).
"""

import json
from typing import Any

from groq import Groq
from pydantic import BaseModel

from feedme_bot.config import settings
from feedme_bot.intent import MODEL, OrderIntent

RANK_PROMPT = """You are ranking food delivery candidates for a WhatsApp bot.
Given the user's intent and a list of candidate items (each with an "id" field),
pick exactly two DIFFERENT ids from the candidate list:
- safe_pick_id: closest match to the user's stated constraints, lowest relative ETA
- mood_pick_id: best match to the specific cuisine/protein/mood constraint, even if
  slightly more expensive or slower

You MUST use ids that appear in the candidate list. Do not invent an id.
Return ONLY JSON: {"safe_pick_id": string, "mood_pick_id": string}"""


class RankResult(BaseModel):
    safe_pick_id: str
    mood_pick_id: str


class NoValidCandidatesError(Exception):
    pass


def _candidate_id(item: dict[str, Any]) -> str:
    # search_menu / get_restaurant_menu disagree on the id field name — see plan notes.
    return str(item.get("menu_item_id") or item.get("id"))


def rank_candidates(
    intent: OrderIntent, candidates: list[dict[str, Any]]
) -> tuple[dict[str, Any], dict[str, Any]]:
    if len(candidates) < 2:
        raise NoValidCandidatesError(f"need at least 2 candidates, got {len(candidates)}")

    by_id = {_candidate_id(item): item for item in candidates}
    slim_candidates = [
        {
            "id": _candidate_id(item),
            "name": item.get("name"),
            "price": item.get("price"),
            "isVeg": item.get("isVeg"),
            "rating": item.get("rating"),
        }
        for item in candidates
    ]

    client = Groq(api_key=settings.groq_api_key)
    response = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": RANK_PROMPT},
            {
                "role": "user",
                "content": json.dumps({"intent": intent.model_dump(), "candidates": slim_candidates}),
            },
        ],
        temperature=0,
        response_format={"type": "json_object"},
    )
    result = RankResult.model_validate_json(response.choices[0].message.content or "{}")

    safe_pick = by_id.get(result.safe_pick_id)
    mood_pick = by_id.get(result.mood_pick_id)
    if safe_pick is None or mood_pick is None:
        # The model picked an id we never offered it — refuse rather than trust it.
        raise NoValidCandidatesError(
            f"model returned ids not in candidate set: {result.safe_pick_id}, {result.mood_pick_id}"
        )
    return safe_pick, mood_pick
