"""Groq-backed message understanding: what kind of message is this, and
(if it's a new order) what does the user actually want.

Two separate calls on purpose — classification has to run on *every*
inbound message (a reply to pending options, a correction, a random text
that isn't about food at all), not just the ones that look like a fresh
order. See plan notes: "reply 1 or 2" is not a safe assumption.
"""

import json
from typing import Literal

from groq import Groq
from pydantic import BaseModel

from feedme_bot.config import settings

MODEL = "llama-3.3-70b-versatile"

MessageKind = Literal["new_order", "selection", "modification", "cancellation", "not_food"]


class OrderIntent(BaseModel):
    query: str
    max_price: int | None = None
    max_eta_mins: int | None = None
    dietary_preference: Literal["veg", "non-veg", "any"] = "any"
    high_protein: bool = False


def _client() -> Groq:
    return Groq(api_key=settings.groq_api_key)


CLASSIFY_PROMPT = """You classify one WhatsApp message for a food-ordering bot.
Categories:
- new_order: describes what food they want (even vaguely)
- selection: picking between previously offered options ("1", "the second one", "yeah get that")
- modification: adjusting a pending order/search ("actually make it veg", "under 300 instead")
- cancellation: backing out ("nah", "cancel that", "none of these")
- not_food: anything unrelated to ordering food

Reply with only the category name, nothing else."""


def classify_message(text: str, has_pending_options: bool) -> MessageKind:
    context = (
        "There are options currently pending the user's reply."
        if has_pending_options
        else "There are no options currently pending."
    )
    response = _client().chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": CLASSIFY_PROMPT},
            {"role": "user", "content": f"{context}\nMessage: {text}"},
        ],
        temperature=0,
    )
    raw = (response.choices[0].message.content or "").strip().lower()
    if raw not in ("new_order", "selection", "modification", "cancellation", "not_food"):
        return "not_food"
    return raw  # type: ignore[return-value]


EXTRACT_PROMPT = """You are a strict JSON extraction engine for a food delivery assistant.
Parse the input into this JSON object, nothing else:
{"query": string, "max_price": integer or null, "max_eta_mins": integer or null,
 "dietary_preference": "veg" | "non-veg" | "any", "high_protein": boolean}
Return ONLY valid JSON, no markdown fences."""


def extract_intent(text: str) -> OrderIntent:
    response = _client().chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": EXTRACT_PROMPT},
            {"role": "user", "content": text},
        ],
        temperature=0,
        response_format={"type": "json_object"},
    )
    data = json.loads(response.choices[0].message.content or "{}")
    return OrderIntent.model_validate(data)


def resolve_selection(text: str) -> Literal["safe", "mood", "unclear"]:
    """Which of the two pending options ("Safe Pick" #1 / "Mood Pick" #2)
    did a free-form reply mean? Handles "1", "the second one", "yeah get
    that", etc. — not just a literal digit (see plan notes)."""
    response = _client().chat.completions.create(
        model=MODEL,
        messages=[
            {
                "role": "system",
                "content": (
                    "The user was offered two options: 1) Safe Pick, 2) Mood Pick. "
                    "Given their reply, answer with exactly one word: safe, mood, or unclear."
                ),
            },
            {"role": "user", "content": text},
        ],
        temperature=0,
    )
    raw = (response.choices[0].message.content or "").strip().lower()
    return raw if raw in ("safe", "mood") else "unclear"  # type: ignore[return-value]


def resolve_confirmation(text: str) -> Literal["yes", "no", "unclear"]:
    """Is this reply confirming a pending checkout, declining it, or
    neither? Used only when a PendingConfirmation is awaiting a reply —
    this is the explicit final gate before place_food_order, never skip
    straight from a selection to checkout (see plan notes)."""
    response = _client().chat.completions.create(
        model=MODEL,
        messages=[
            {
                "role": "system",
                "content": (
                    "The user is being asked to confirm a food order before payment. "
                    "Given their reply, answer with exactly one word: yes, no, or unclear."
                ),
            },
            {"role": "user", "content": text},
        ],
        temperature=0,
    )
    raw = (response.choices[0].message.content or "").strip().lower()
    return raw if raw in ("yes", "no") else "unclear"  # type: ignore[return-value]
