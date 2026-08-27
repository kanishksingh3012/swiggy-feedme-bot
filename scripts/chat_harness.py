"""Talk to the bot's logic directly from the terminal — no WhatsApp
involved. Exercises the exact same handle_message() the real webhook
calls, so the whole pipeline (intent parsing, Swiggy search, ranking,
confirm-before-checkout) is testable while WhatsApp Cloud API is blocked.

handle_message() sends replies itself via whatsapp.py (interactive
buttons/lists, not plain strings) — this stubs those senders to print to
the terminal instead of hitting the real Graph API, so this still works
with no Meta credentials at all. Tap-based flows (address/pick/confirm)
are simulated by typing the row/button id shown in brackets, e.g. "1" or
"addr:<id>" as printed.

Requires GROQ_API_KEY set and a working Swiggy login (first call will
open a browser for the PKCE flow if no cached token exists).

Usage: python scripts/chat_harness.py
"""

import asyncio

from feedme_bot import whatsapp

FAKE_JID = "test-user@local"


async def _print_text(to: str, body: str) -> None:
    print(f"bot> {body}\n")


async def _print_buttons(to: str, body: str, buttons: list[tuple[str, str]], footer: str = "") -> None:
    print(f"bot> {body}")
    for bid, title in buttons:
        print(f"     [{bid}] {title}")
    if footer:
        print(f"     ({footer})")
    print()


async def _print_list(
    to: str, body: str, button_text: str, rows: list[tuple[str, str, str]], section_title: str = "Options"
) -> None:
    print(f"bot> {body}")
    print(f"     [{button_text}]")
    for rid, title, description in rows:
        print(f"     [{rid}] {title} — {description}")
    print()


whatsapp.send_text = _print_text
whatsapp.send_reply_buttons = _print_buttons
whatsapp.send_list = _print_list

from feedme_bot.handlers import handle_message  # noqa: E402 (must patch whatsapp first)


async def main() -> None:
    print("FeedMe Bot local harness — type a message or an id shown in [brackets], Ctrl+C to quit.\n")
    while True:
        try:
            text = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not text:
            continue
        interactive_id = text if ":" in text else None
        await handle_message(FAKE_JID, text, interactive_id=interactive_id)


if __name__ == "__main__":
    asyncio.run(main())
