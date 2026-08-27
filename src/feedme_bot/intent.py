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

# Verified live against this Groq account's actual model list (2026-08-25) —
# llama-3.3-70b-versatile is gone from the lineup entirely, don't assume it.
MODEL = "openai/gpt-oss-120b"

MessageKind = Literal[
    "new_order", "selection", "more_options", "modification", "cancellation", "not_food"
]


class OrderIntent(BaseModel):
    # None means "no real signal about what food is wanted" — the caller
    # asks the user directly rather than searching on a guess. Search
    # requires a real term either way, so this is never silently defaulted.
    query: str | None
    query_alternatives: list[str] = []
    # Only set when the user actually named a specific restaurant/brand —
    # "biryani from Paradise", "get me a pizza from Domino's". Never a
    # guess; leave null rather than invent one, since a wrong guess here
    # scopes the whole search to the wrong place.
    restaurant: str | None = None
    max_price: int | None = None
    max_eta_mins: int | None = None
    dietary_preference: Literal["veg", "non-veg", "any"] = "any"
    high_protein: bool = False
    # Free-text aversions the user actually stated ("not too oily", "no
    # onion"), separate from "query" — query is a search term to find
    # candidates with, this is a constraint the ranking step must still
    # respect once those candidates come back, since a search term match
    # doesn't guarantee the specific dish avoids what was ruled out.
    dislikes: list[str] = []


def _client() -> Groq:
    return Groq(api_key=settings.groq_api_key)


CLASSIFY_PROMPT = """You classify one WhatsApp message for a food-ordering bot.
Categories:
- new_order: describes what food they want (even vaguely)
- selection: picking between previously offered options ("1", "the second one", "yeah get that")
- more_options: wants to see other choices beyond what's currently offered, without
  rejecting them outright ("give me more", "what else you got", "show me other options",
  "anything else?") — only valid when options are currently pending
- modification: adjusting a pending order/search ("actually make it veg", "under 300 instead")
- cancellation: backing out ("nah", "cancel that", "none of these")
- not_food: anything unrelated to ordering food

Reply with only the category name, nothing else."""

_MESSAGE_KINDS = (
    "new_order", "selection", "more_options", "modification", "cancellation", "not_food",
)


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
    if raw not in _MESSAGE_KINDS:
        return "not_food"
    return raw  # type: ignore[return-value]


EXTRACT_PROMPT = """You are a strict JSON extraction engine for a food delivery assistant.
Parse the input into this JSON object, nothing else:
{"query": string or null, "query_alternatives": array of 0-3 strings, "restaurant": string or null,
 "max_price": integer or null, "max_eta_mins": integer or null,
 "dietary_preference": "veg" | "non-veg" | "any", "high_protein": boolean}

"restaurant" is only set when the user actually names a specific restaurant
or brand ("from Paradise", "get it from Domino's", "order from KFC") — this
scopes the whole search to just that one place, so never guess one from a
dish/cuisine alone ("biryani" alone does NOT imply a restaurant named
"Biryani House" — leave it null unless a real name was stated).

"query" and every entry in "query_alternatives" go straight into a keyword search
against real menu item names, so each must be a concrete, searchable food/dish/
cuisine term — never price, time, or mood/context phrasing on its own.

Most real messages do NOT name a dish directly — they describe a mood, craving,
or context instead ("something warm and comforting", "craving something spicy",
"feeling like junk food today", "light and healthy, not too heavy"). This is a
guess, not a fact the user stated — search near them can easily come up empty
for your first guess even when plenty of dishes would've matched their actual
mood. That's what query_alternatives is for: when "query" is a guessed
translation (not a dish the user named directly), give 2-3 OTHER concrete,
genuinely different dishes/cuisines that would satisfy the same mood, ordered
by likelihood — the caller tries them in order if the first has too few results
nearby. Give real alternatives, not near-duplicates of the primary guess (e.g.
"chicken biryani" and "veg biryani" are not alternatives to each other).
If the message names a specific dish directly, query_alternatives can be empty
— there's nothing left to translate or hedge against.

Examples:
"Tired after workout, chicken dish under 350, fast ETA" ->
{"query": "chicken", "query_alternatives": [], "restaurant": null, "max_price": 350,
 "max_eta_mins": null, "dietary_preference": "any", "high_protein": false}

"I'm just in the mood for something warm and comforting, keep it under 250" ->
{"query": "khichdi", "query_alternatives": ["dal rice", "soup", "maggi"], "restaurant": null,
 "max_price": 250, "max_eta_mins": null, "dietary_preference": "any", "high_protein": false}

"Craving something spicy and filling, keep it under 300, not too oily though" ->
{"query": "chilli chicken", "query_alternatives": ["peri peri wings", "schezwan noodles", "biryani"],
 "restaurant": null, "max_price": 300, "max_eta_mins": null, "dietary_preference": "any",
 "high_protein": false}

"honestly just craving something greasy and unhealthy rn" ->
{"query": "burger", "query_alternatives": ["fries", "pizza"], "restaurant": null, "max_price": null,
 "max_eta_mins": null, "dietary_preference": "any", "high_protein": false}

"Get me a biryani from Paradise, under 400" ->
{"query": "biryani", "query_alternatives": [], "restaurant": "Paradise", "max_price": 400,
 "max_eta_mins": null, "dietary_preference": "any", "high_protein": false}

"I'm hungry, get me something" (no order history given) ->
{"query": null, "query_alternatives": [], "restaurant": null, "max_price": null,
 "max_eta_mins": null, "dietary_preference": "any", "high_protein": false}

If the message explicitly references a past order ("the usual", "same as
last time", "what I got last week") — that reference IS real signal — and a
list of past orders is given below, use it to fill in "query".

If the message gives NO real signal at all about what food is wanted
("I'm hungry", "get me something", "surprise me", "whatever's good", or
anything not actually about food) — not even a mood, not even a reference
to a past order — set "query" to null and "query_alternatives" to []. Never
guess a dish out of nowhere just to have an answer, and never lean on order
history as a silent default either — the caller asks the user directly
when query is null, and a real answer from them beats any guess."""


def extract_intent(text: str, order_history: list[str] | None = None) -> OrderIntent:
    system_prompt = EXTRACT_PROMPT
    if order_history:
        system_prompt += "\n\nUser's recent past orders: " + ", ".join(order_history)

    response = _client().chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
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


def resolve_numbered_choice(text: str, count: int) -> int | None:
    """Which numbered option (1..count) did a free-form reply pick? Used
    for the one-time address picker. Returns None if unclear."""
    response = _client().chat.completions.create(
        model=MODEL,
        messages=[
            {
                "role": "system",
                "content": (
                    f"The user was shown a numbered list of {count} options and asked to "
                    f"pick one. Given their reply, answer with just the number (1-{count}), "
                    "or the word 'unclear' if it doesn't clearly pick one."
                ),
            },
            {"role": "user", "content": text},
        ],
        temperature=0,
    )
    raw = (response.choices[0].message.content or "").strip()
    if raw.isdigit() and 1 <= int(raw) <= count:
        return int(raw)
    return None


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
